"""Bind the existing library to a freshly verified login on a NEW offline copy."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import tempfile
from pathlib import Path

from firebase_identity import FirebaseIdentity, IdentityStore, create_token_verifier
from legacy_reconciliation import prepare_reconciled_copy


def prepare_bound_copy(source: Path, output: Path, *, owner_id: str, owner_name: str,
                       identity: FirebaseIdentity) -> dict:
    output = output.absolute()
    if output.exists() or output.is_symlink() or output.resolve() == source.resolve():
        raise ValueError('Output must be a new file, different from the source')
    with tempfile.TemporaryDirectory(prefix='.jot-bind-', dir=output.parent) as temp:
        staged = Path(temp) / 'accounts.sqlite'
        report = prepare_reconciled_copy(source, staged, owner_id=owner_id, owner_name=owner_name)
        IdentityStore(staged).bind_existing(owner_id, identity)
        with staged.open('rb') as handle:
            os.fsync(handle.fileno())
        os.link(staged, output)
        return {**report, 'output': str(output), 'bound_owner_id': owner_id,
                'firebase_project_id': identity.project_id, 'firebase_uid': identity.uid}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--owner-id', required=True)
    parser.add_argument('--owner-name', required=True)
    parser.add_argument('--firebase-project', required=True)
    parser.add_argument('--expected-uid', required=True)
    parser.add_argument('--credential-file', type=Path)
    args = parser.parse_args()
    verifier = create_token_verifier(args.firebase_project, credential_path=args.credential_file)
    # Never pass ID tokens through command arguments, logs, or stored reports.
    identity = verifier.verify(getpass.getpass('Fresh Firebase ID token: '), recent=True)
    if identity.uid != args.expected_uid:
        raise ValueError('Verified login does not match the explicitly selected Firebase UID')
    print(json.dumps(prepare_bound_copy(args.source, args.output, owner_id=args.owner_id,
        owner_name=args.owner_name, identity=identity), indent=2))
