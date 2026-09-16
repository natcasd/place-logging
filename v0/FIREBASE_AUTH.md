# Firebase authentication and existing-library ownership

Jot uses standard Firebase Authentication with Apple and Google. Firebase holds
login identities; private library data stays in SQLite on Fly. The selected
project is `jot-app-20260915` (Jot App), linked to the existing billing account
(Blaze). The verified authentication subtype is `FIREBASE_AUTH`.
Do not enable the optional Identity Platform upgrade, email, phone, anonymous
sign-in, or a separate app-data database as part of this setup.

## Request flow

1. The native SDK signs in with Apple or Google and obtains a Firebase ID token.
2. The client sends that token in `Authorization: Bearer ...` over HTTPS.
3. The official Python Admin SDK verifies the signature, issuer, audience,
   expiry, and revocation/disabled status. Jot requires its explicit project,
   an Apple/Google sign-in provider, and a valid authentication time. Emulator,
   anonymous, custom, email/phone, and tenant tokens are rejected.
4. The verified `(Firebase project, UID)` resolves to an internal account ID.
   First sign-in creates an empty account atomically. Existing account names,
   emails, first-registration order, and client-supplied owner IDs never determine
   ownership. A disabled internal account cannot be recreated by signing in.
5. Every library operation still checks the internal account in its transaction.

`GET /api/v1/account` returns the current internal ID and display name. The same
Firebase UID linked to both providers has one library. A different UID receives
a different account even if an email matches; merging libraries is not implicit.

The SDK's normal token renewal preserves long-lived mobile sessions. Routine
requests do not require a recent sign-in. Revoked/invalid credentials return 401;
verification outages return a sanitized 503, so the client can keep its session
and retry without presenting an unnecessary login screen.

## Preserve Nathan's library

Schema 4 added nullable `firebase_project_id` and `firebase_uid` columns to
`users`, with a unique pair and all-or-neither constraint. Schema 5 adds the
`deleting` user status without new tables or columns. The offline migration
upgrades schema 1/2/3/4 copies, preserving existing fields and IDs. New captures,
recommendations, or ownership tables are unnecessary.

The existing internal owner stays unchanged. Before release, Nathan signs in on
his device and the operator verifies the intended Firebase UID. This command
requires that exact UID **and** a matching freshly authenticated token:

```sh
python bind_legacy_account.py \
  --source /absolute/path/to/fresh-snapshot.sqlite \
  --output /absolute/path/to/new-bound.sqlite \
  --owner-id nathan --owner-name Nathan \
  --firebase-project jot-app-20260915 \
  --expected-uid THE_VERIFIED_FIREBASE_UID \
  --credential-file /private/path/to/admin-credential.json
```

Enter the fresh ID token at the hidden prompt, never in command arguments,
version control, or a chat message. The command verifies it with revocation
checking and requires authentication within five minutes. It then migrates and
reconciles a new private copy, binds only the explicitly selected existing owner,
and atomically publishes the verified output. The source stays read-only.

Existing bindings cannot be replaced, and a UID already attached to another
internal account causes a conflict requiring review. It never silently moves or
deletes records to resolve that conflict. Repeating the same binding is safe;
output files are never overwritten. There is no public "claim old library" API.

The account service refuses startup with unreconciled captures, an unbound
existing library, or bindings from another Firebase project. Real binding has
not been performed: Nathan's actual Firebase sign-in is still required.

## Explicit service entrypoint

Production still starts the old `app:app`; merging this change does not deploy.
After complete client integration and a separately rehearsed cutover, use:

```sh
uvicorn firebase_service:create_app --factory --host 0.0.0.0 --port 8000
```

Required environment: `JOT_ACCOUNT_DB_PATH`, `FIREBASE_PROJECT_ID`, and
`JOT_PROCESSING_VERSION`. Supply Admin SDK application credentials through
`GOOGLE_APPLICATION_CREDENTIALS` or an appropriate Application Default
Credentials deployment configuration. Credentials must allow user lookup for
revocation checks. Never put service-account credentials in the mobile app.
The service explicitly starts the durable processing and account-deletion workers
and drains in-flight work on shutdown. Deletion also requires permission to delete
Firebase users; do not expose this administrative credential to clients.

## Account deletion

`DELETE /api/v1/account` requires a verified Apple/Google Firebase session whose
`auth_time` is within five minutes. An otherwise valid older session receives
403 with `code: recent_sign_in_required`; it does not invalidate normal login.
The native client must obtain fresh provider authentication and, for linked Apple
accounts, revoke the Apple token before requesting deletion.

The response is 202 `deletion_requested`. In one transaction the account becomes
`deleting`, immediately blocking private reads, writes, and processing deliveries.
Repeating the request with a still-valid fresh token is idempotent. An identity
without a library gets a separate empty deletion record; it cannot claim an
unbound historical library.

The worker checks pending deletions every 30 seconds, deletes the Firebase user
first, and then cascades deletion of that account's private SQL records. A missing
Firebase user counts as success. Provider outages preserve the blocked account and
retry after restart. Other users and shared public processing/Places data remain.
New-account creation rechecks Firebase under the database transaction so a token
verified just before deletion cannot recreate the deleted account afterward.

These are deletions from live application storage. Retained backups need an
explicit expiry policy and a restore procedure that reapplies later deletions
before serving data. Operational backup/restore handling and the native deletion
screen remain required before release; do not claim backups are instantly erased.

## Verification and outstanding setup

Tests use the real Admin SDK with locally generated RSA-signed tokens and
injected certificate/user lookup responses; no paid APIs or real users are
created. Coverage includes incorrect signatures/projects/issuers, expiry,
revocation, disabled/deleted users, emulator refusal, concurrent first login,
provider linking, ownership conflicts, safe offline binding, and startup guards.

The replacement project is active, linked to the existing billing account, and
verified as standard Firebase Authentication after billing linkage. Google
sign-in is enabled. The registered iOS app is
`1:271626317276:ios:7d30fc01f17100b8403319`, bundle `com.natcasd.placelogger`.
Download its configuration locally; never commit backend administrative credentials.
Apple provider setup and physical-device signing remain pending Apple Developer
Program approval. Real-device login and secure deployment credentials still need
validation. No app-data database was created in Firebase.

Initialize standard Authentication using the Firebase console's Get started
flow and verify `subtype: FIREBASE_AUTH` via the project configuration API. Do not
use `identityPlatform:initializeAuth`, the Identity Platform upgrade, or
`firebase:provisionFirebaseApp` for authentication initialization. The provisioning
workflow enabled the unwanted Identity Platform tier in the former project.

The old `place-logging-7b73af` project remains intact for existing Places services.
It is not the selected Firebase identity project. No production service or phone
build has switched to the replacement. Prepare and test new Places credentials
before coordinated cutover, then verify no remaining dependencies before deleting
the old project. Do not alter existing library ownership to perform this change.

Official references:
- [Verify ID tokens](https://firebase.google.com/docs/auth/admin/verify-id-tokens)
- [Manage sessions and revocation](https://firebase.google.com/docs/auth/admin/manage-sessions)
- [Add Firebase to an existing project](https://firebase.google.com/docs/projects/api/workflow_set-up-and-manage-project)
- [Native Apple sign-in](https://firebase.google.com/docs/auth/ios/apple)
- [Google sign-in on iOS](https://firebase.google.com/docs/auth/ios/google-signin)
