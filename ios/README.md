# Jot for iPhone

The native client has two targets:

- `PlaceLogger`: Apple/Google sign-in, a private saved library, map, Activity,
  and account settings with verified provider linking and sign-out.
- `PlaceLoggerShare`: uses the same secure Keychain session to queue a social
  URL, then closes after acceptance. Processing continues on the server.

The app reads account-scoped `/api/v1/entries` and `/api/v1/activity`. It refreshes
pending Activity while foregrounded. A share notification acknowledges acceptance;
it does not claim processing has finished. Backend APNs completion notifications
remain a separate integration. Notification payloads include the account session,
and a tap from another/older session is ignored.

There is no bundled shared API token or legacy-token fallback in this client.
The currently installed phone app and production backend have not been changed.
This client requires the complete Firebase account service and coordinated cutover.

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
   project `place-logging-7b73af`. Enable Apple and Google authentication.
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
