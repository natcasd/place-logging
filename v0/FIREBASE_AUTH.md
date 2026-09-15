# Firebase authentication and existing-library ownership

Jot uses standard Firebase Authentication with Apple and Google. Firebase holds
login identities; private library data stays in SQLite on Fly. The selected
project is `place-logging-7b73af`, using its existing billing account (Blaze).
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

Schema 4 adds only nullable `firebase_project_id` and `firebase_uid` columns to
`users`, with a unique pair and all-or-neither constraint. The offline migration
upgrades schema 1/2/3 copies, preserving existing fields and IDs. New captures,
recommendations, or ownership tables are unnecessary.

The existing internal owner stays unchanged. Before release, Nathan signs in on
his device and the operator verifies the intended Firebase UID. This command
requires that exact UID **and** a matching freshly authenticated token:

```sh
python bind_legacy_account.py \
  --source /absolute/path/to/fresh-snapshot.sqlite \
  --output /absolute/path/to/new-bound.sqlite \
  --owner-id nathan --owner-name Nathan \
  --firebase-project place-logging-7b73af \
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
The service explicitly starts the durable worker and drains it on shutdown.

## Verification and outstanding setup

Tests use the real Admin SDK with locally generated RSA-signed tokens and
injected certificate/user lookup responses; no paid APIs or real users are
created. Coverage includes incorrect signatures/projects/issuers, expiry,
revocation, disabled/deleted users, emulator refusal, concurrent first login,
provider linking, ownership conflicts, safe offline binding, and startup guards.

Cloud setup is not complete. The existing project and billing were verified, and
Firebase/project-management APIs were enabled. `addFirebase` still returned 403
although all four documented IAM permissions were granted. Console sign-in is
needed to diagnose/complete project activation. Apple/Google provider setup,
mobile SDK configuration, secure deployment credentials, and real-device login
remain outstanding. No Identity Platform upgrade or app-data database was created.

Official references:
- [Verify ID tokens](https://firebase.google.com/docs/auth/admin/verify-id-tokens)
- [Manage sessions and revocation](https://firebase.google.com/docs/auth/admin/manage-sessions)
- [Add Firebase to an existing project](https://firebase.google.com/docs/projects/api/workflow_set-up-and-manage-project)
- [Native Apple sign-in](https://firebase.google.com/docs/auth/ios/apple)
- [Google sign-in on iOS](https://firebase.google.com/docs/auth/ios/google-signin)
