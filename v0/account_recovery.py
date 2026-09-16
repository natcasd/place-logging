"""Private backups and a deletion journal that must survive database restoration.

The journal contains only project/UID deletion intent, never library content or
credentials. Keep its latest copy separately from historical application backups.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from multi_user_migration import APPLICATION_ID, SCHEMA_VERSION

JOURNAL_ID = 0x4A4F5444


def _check_database(con):
    if (con.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
            or con.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
            or con.execute('PRAGMA integrity_check').fetchall() != [('ok',)]
            or con.execute('PRAGMA foreign_key_check').fetchall()):
        raise ValueError('A valid current account database is required')


class DeletionJournal:
    def __init__(self, path: Path):
        self.path = path.resolve(strict=True)
        con = self._connect()
        try:
            if con.execute('PRAGMA application_id').fetchone()[0] != JOURNAL_ID:
                raise ValueError('Invalid deletion journal')
            con.execute('SELECT project_id, uid FROM deletions LIMIT 0')
        finally:
            con.close()

    @classmethod
    def initialize(cls, path: Path):
        # Explicit one-time operator action: service startup must not silently
        # create an empty journal when an existing journal is missing.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        con = sqlite3.connect(path)
        try:
            con.execute(f'PRAGMA application_id = {JOURNAL_ID}')
            con.execute('''CREATE TABLE deletions (project_id TEXT NOT NULL, uid TEXT NOT NULL,
                requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(project_id, uid))''')
            con.commit()
        finally:
            con.close()
        return cls(path)

    def _connect(self):
        return sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True)

    def record(self, project_id: str, uid: str):
        con = self._connect()
        try:
            con.execute('PRAGMA synchronous = FULL')
            con.execute('INSERT OR IGNORE INTO deletions(project_id,uid) VALUES (?,?)', (project_id, uid))
            con.commit()
        finally:
            con.close()

    def backup(self, output: Path):
        """Export a consistent independent journal; never overwrite a prior copy."""
        output = output.absolute()
        if output.exists() or output.is_symlink() or output.resolve() == self.path:
            raise ValueError('Journal backup must be a new file')
        fd, name = tempfile.mkstemp(prefix='.jot-journal-', suffix='.sqlite', dir=output.parent)
        os.close(fd)
        temporary = Path(name)
        source = self._connect()
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
            if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Invalid journal backup')
            target.close()
            with temporary.open('rb') as handle:
                os.fsync(handle.fileno())
            os.link(temporary, output)
        finally:
            target.close()
            source.close()
            temporary.unlink(missing_ok=True)

    def apply(self, con, project_id: str) -> int:
        journal = self._connect()
        try:
            rows = journal.execute('SELECT uid FROM deletions WHERE project_id=?', (project_id,)).fetchall()
        finally:
            journal.close()
        changed = 0
        for (uid,) in rows:
            changed += con.execute("UPDATE users SET status='deleting' WHERE firebase_project_id=? AND firebase_uid=? AND status!='deleting'",
                                   (project_id, uid)).rowcount
        return changed


def prepare_backup(source: Path, output: Path, *, journal: DeletionJournal | None = None,
                   project_id: str | None = None) -> dict:
    """Publish a validated independent copy, never overwrite a source or output.

    With a journal, prepare a restoration candidate with later deletions blocked.
    This does not switch a live database or roll back post-backup writes.
    """
    source = source.resolve(strict=True)
    output = output.absolute()
    if source == output.resolve() or output.exists() or output.is_symlink():
        raise ValueError('Output must be a new file')
    if (journal is None) != (project_id is None) or project_id == '':
        raise ValueError('Restoration requires both journal and Firebase project')
    fd, name = tempfile.mkstemp(prefix='.jot-recovery-', suffix='.sqlite', dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    original = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    target = sqlite3.connect(temporary)
    try:
        original.backup(target)
        target.execute('PRAGMA journal_mode = DELETE')
        target.execute('PRAGMA foreign_keys = ON')
        _check_database(target)
        blocked = 0
        if journal is not None:
            if target.execute('SELECT 1 FROM users WHERE firebase_project_id IS NOT NULL AND firebase_project_id != ? LIMIT 1', (project_id,)).fetchone():
                raise ValueError('Backup belongs to another Firebase project')
            blocked = journal.apply(target, project_id)
            target.commit()
        _check_database(target)
        counts = {table: target.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                  for table in ('users', 'captures', 'recommendations', 'recommendation_mentions', 'locations')}
        target.close()
        with temporary.open('rb') as handle:
            os.fsync(handle.fileno())
        os.link(temporary, output)
        return {'counts': counts, 'accounts_blocked_by_deletion_journal': blocked, 'output': str(output)}
    finally:
        target.close()
        original.close()
        temporary.unlink(missing_ok=True)
        for suffix in ('-journal', '-wal', '-shm'):
            Path(str(temporary)+suffix).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init-journal')
    init.add_argument('--journal', type=Path, required=True)
    journal_backup = commands.add_parser('backup-journal')
    journal_backup.add_argument('--journal', type=Path, required=True)
    journal_backup.add_argument('--output', type=Path, required=True)
    for name in ('backup', 'restore-copy'):
        command = commands.add_parser(name)
        command.add_argument('--source', type=Path, required=True)
        command.add_argument('--output', type=Path, required=True)
        if name == 'restore-copy':
            command.add_argument('--journal', type=Path, required=True)
            command.add_argument('--project', required=True)
    args = parser.parse_args()
    if args.command == 'init-journal':
        DeletionJournal.initialize(args.journal)
        print('Initialized deletion journal')
    elif args.command == 'backup-journal':
        DeletionJournal(args.journal).backup(args.output)
        print('Exported deletion journal')
    else:
        journal = DeletionJournal(args.journal) if args.command == 'restore-copy' else None
        print(json.dumps(prepare_backup(args.source, args.output, journal=journal,
                                       project_id=args.project if journal else None), indent=2))


if __name__ == '__main__':
    main()
