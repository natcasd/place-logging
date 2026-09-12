# Jot for iPhone

The native MVP contains two targets:

- `PlaceLogger`: a SwiftUI list and MapKit map of saved Entries.
- `PlaceLoggerShare`: a native share extension that accepts links and media
  shared by Instagram, TikTok, or YouTube and sends the extracted URL to the
  existing ingest API. The extension can close while the synchronous request
  continues; on completion, it schedules a local notification summarizing the
  logged Entries.

The current free, locally provisioned build cannot use APNs. Because completion
notifications are scheduled by the share extension, tapping them does not open
the containing app. When the app moves to a paid Apple Developer Program team,
migrate these to APNs notifications sent by the backend so taps can launch the
app and navigate to the logged result.

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

## Generate and build

1. Copy `Config/Secrets.xcconfig.example` to `Config/Secrets.xcconfig` and set
   `PLACE_LOGGER_API_TOKEN`. The secrets file is ignored by Git.
2. Run `xcodegen generate` from this directory.
3. Build without signing:
   `xcodebuild -project PlaceLogger.xcodeproj -scheme PlaceLogger -sdk iphonesimulator -configuration Debug CODE_SIGNING_ALLOWED=NO build`

Open `PlaceLogger.xcodeproj` only when selecting the Personal Team and
installing on a physical iPhone. All source files can be edited outside Xcode.
