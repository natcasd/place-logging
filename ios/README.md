# Jot for iPhone

## Share auto-close experiment

This branch tests automatic dismissal before processing finishes. The extension
first posts with `wait_seconds=0`. Only after confirmed durable acceptance does it
show the outlined green circle/check with “Processing…” and “You will be notified
on completion.” It starts system dismissal after 750 ms, targeting roughly 1.0
seconds including the system animation. Network acceptance time is additional.
An unconfirmed save stays open with the existing error message.

The extension requests a half-height sheet through its preferred content size
and selects the medium detent when a native sheet controller is available. A
large detent remains available for expansion. The source app owns presentation,
so the actual height still needs physical-device verification in the source app.

An independent task repeats the POST with the **same request key** and
`wait_seconds=150` to retain the existing result/local-notification attempt without
creating a second save. It is not tied to SwiftUI view cancellation, but iOS can
still terminate the extension after `completeRequest`. This experiment does not
guarantee notification delivery. No polling, APNs, FCM, backend deployment, or
database change is included. ShareFlow logs contain lifecycle events, not URLs or
account identifiers.

Verify on a phone with a new post: let the sheet close automatically, stay in the
source app, and check for the final notification. A cached post alone does not
exercise a long processing interval. If notifications regress, reinstall the
previous signed build without deleting the app. The baseline flow below remains
the rollback behavior until this experiment is accepted.

The native client has two targets:

- `PlaceLogger`: Apple/Google sign-in, a private saved library, Apple Maps,
  Activity, and account settings with provider linking and sign-out.
- `PlaceLoggerShare`: submits a social URL using the shared Keychain session,
  waits for the actual result through an ordinary HTTP request, posts the local
  result notification, and completes the share request.

## Save-result notifications

This retains the original extension-owned request/local-notification flow. The
account API accepts the save durably before waiting, so server processing survives
an extension exit, phone disconnection, or response timeout. The native POST uses
`wait_seconds=150` and a 180-second request timeout. Other API clients still get
immediate 202 acceptance by default. A completed, partial, or failed
result supplies the existing detailed notification text and destinations. A pending
response is not presented as a completed save. Acceptance, scheduled retries and
transport uncertainty do not produce notifications; transport errors stay in the
share sheet.

The extension can continue while the user returns to the source app, as observed
with the original app, but iOS controls its lifetime. If iOS terminates it before
the result arrives, processing continues and Activity shows the result; this flow
does not guarantee a later notification. Automatic retries can also finish after
the original request ends. APNs will address that delivery gap after Apple approval.

There is no background URLSession polling, App Group entitlement, or main-app
background callback. The rejected polling prototype repeatedly launched downloads,
which Apple documents as subject to increasing scheduling delays. See
[Apple's background transfer documentation](https://developer.apple.com/documentation/foundation/downloading-files-in-the-background).

All library requests and notification taps are account-scoped. Logout clears
notifications, and in-flight responses check the original account generation.
There is no shared API token fallback. Deploy the waiting-result API before
installing this client. Phone verification of the restored flow remains required.

## Next step: APNs after Apple approval

The existing Fly worker can send result alerts directly to Apple Push Notification
service (APNs). Firebase Authentication is independent; Firebase Cloud Messaging
is optional and is not installed or required.

Keep the private saved result as the source of truth. Add account-owned device-token
registration, a small durable send record in SQLite, and the Fly-side APNs sender.
Handle logout, token changes, send retries and duplicate events. Switch each device
from local result notifications to remote ones so it does not receive both. Check
queued-alert privacy across account switches, and authenticate detail reads on tap.
No new app-data database, cloud functions, or external queue is needed.

This remains future work. Apple login and push capabilities still require the
membership/provider/provisioning setup. See
[Sending notification requests to APNs](https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns).

The map shows resolved places as selectable pins. Pins use the saved place
type's icon (for example, a fork and knife for restaurants or a tree for
parks). Repeated saves with the same Google Place ID share one pin while
retaining every recommendation and original source post in the detail sheet.
Places without resolved coordinates remain available in the list.

New saves retain where each place appears in its source. Reel, TikTok, and
YouTube results show the start of the place's main section as a timestamp.
Instagram carousels show a 1-based slide number and open the original post with
Instagram's `img_index` parameter. Caption-only matches leave the media
reference empty rather than guessing.

The map requests foreground-only location access when first opened and starts
at a neighborhood-level view around the device. A single-purpose location
control always returns to that north-up neighborhood view after browsing
elsewhere; it does not cycle through tracking or heading modes. A circular
search control at the top of the map expands into a capsule-shaped field and
provides Apple Maps suggestions for cities, neighborhoods, addresses, and
points of interest. Manual refresh remains available from the Saved and
Activity screens; selecting a map search result moves the camera while leaving
saved-place pins visible.

## Configure and build

1. Register the parent bundle `com.natcasd.placelogger` in the selected Firebase
   project `jot-app-20260915` (Jot App). This project uses standard Firebase
   Authentication on Blaze. Google is enabled; Apple setup and physical-device
   provisioning remain pending Apple Developer Program approval.
2. Download its `GoogleService-Info.plist` into `Firebase/`. The resource folder
   is bundled in both targets. The configuration is ignored by Git; no Admin SDK
   credential belongs in the app. A missing/wrong-project configuration builds
   successfully but shows sign-in unavailable and makes no library requests.
3. Copy `Config/Secrets.xcconfig.example` to `Config/Secrets.xcconfig` and set
   `GOOGLE_REVERSED_CLIENT_ID` from the downloaded configuration. This registers
   Google's native OAuth callback URL scheme. Existing legacy token settings are
   unused and do not grant access to the account API.
4. Run `xcodegen generate` from this directory.
5. Build without signing:
   `xcodebuild -project PlaceLogger.xcodeproj -scheme PlaceLogger -sdk iphonesimulator -configuration Debug CODE_SIGNING_ALLOWED=NO build`
6. Run the standalone native account-contract tests: `swift test`.

Both targets share `$(AppIdentifierPrefix)com.natcasd.placelogger.auth`. The main
app additionally requires Sign in with Apple. Physical-device provisioning must
support these capabilities; an unsigned simulator build does not verify them.
Cloud/provider configuration and actual phone login still require validation.

Firebase's project configuration API must report `subtype: FIREBASE_AUTH`.
Initialize standard Authentication through the Firebase console's Get started
flow. Do not use `identityPlatform:initializeAuth`, the optional Identity Platform
upgrade, or `firebase:provisionFirebaseApp` to initialize authentication: the last
workflow selected Identity Platform during the earlier setup. Billing linkage is
separate from the authentication tier.

The old project `place-logging-7b73af` still provides the existing Places setup.
Changing the expected Firebase project here does not migrate Places credentials
or deploy the backend. Prepare and test replacement Places configuration before
the coordinated release, and check dependencies before retiring the old project.

## Account isolation and restoration

Firebase persists and renews its session in Keychain. A separate shared session
stamp records the active UID/project and a fresh generation for each login.
Sign-out clears the stamp before clearing Firebase state, so an extension's late
refresh cannot reopen a signed-out session. Every API operation captures this
stamp and rechecks it before sending, after token refresh, and after receiving.
A 401 gets one forced refresh; a repeated 401 signs out only the original session.
A 503 preserves the session for retry. Private responses use an ephemeral,
uncached, cookieless session; redirects cannot forward the bearer credential.

Changing sessions recreates the entire library/navigation/map view tree and
clears pending notification routing. Existing private data is not assigned to
whichever account logs in first; the [backend owner-binding step](../v0/FIREBASE_AUTH.md)
must explicitly connect Nathan's verified identity to his preserved library.

A new share invocation gets a new request key; token refresh/retries reuse that
same key. Activity-card deletion removes the selected mention. Map/list deletion
removes the whole recommendation and its mentions. Location confirmation includes
the mention ID so two outputs for one place cannot edit the wrong occurrence.
Nullable social URLs support future private Siri/typed captures without inventing
an original post. The Siri connector itself is not implemented here.

The native contract tests cover account switches during token acquisition and
responses, bounded authentication retries, verification outages, stable request
identity, queued acceptance, nullable URLs, and distinct same-place mentions.
Full Apple/Google login, cross-process Keychain behavior, logout/relaunch, and
real two-device isolation remain release checks after cloud configuration.

## Welcome screen

The welcome screen uses the complete Jot bookmark beside lowercase `jot`, with a
single “Sign in or create an account” entry point and “Continue with” provider
buttons. First-time provider authentication creates the Firebase account;
returning users use the same provider credentials to reopen their library.
The welcome redesign does not change account creation, ownership, or session
logic. Apple sign-in remains unavailable in the temporary Google-only Personal
Team device build until Apple setup is complete.

## Account settings and deletion

Account shows an email-initial avatar, the account email, and its sign-in provider.
Its three-dot Account options menu contains Sign out and Delete account; provider
linking is not offered in this screen. If the provider supplies no email, the
screen uses a generic account icon and label.

Account > Account options > Delete account explains permanent library removal,
then requires typing exactly `DELETE` on a second confirmation screen before the
final deletion button enables. It then reauthenticates the original account.
Cancel is available at each step until an authentication/deletion request is busy.
Apple-linked accounts
confirm with Apple and revoke the Apple authorization through Firebase; Google-only
accounts confirm with Google. The fresh ID token authorizes the durable backend
request. After acceptance, sign-out clears the shared session and private view
state. A changed account aborts the operation. Real provider and Apple revocation
checks remain part of physical-device release validation.
