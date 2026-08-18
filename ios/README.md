# Place Logger for iPhone

The native MVP contains two targets:

- `PlaceLogger`: a SwiftUI list and MapKit map of saved restaurants.
- `PlaceLoggerShare`: a native share extension that accepts links and media
  shared by Instagram or YouTube and sends the extracted URL to the existing
  ingest API. The extension can close while the synchronous request continues;
  on completion, it schedules a local notification naming the saved place.

Tapping a successful notification opens the containing app, refreshes the
saved-place list, and navigates to the places created by that ingest. Native
shares use response-only API delivery, so they do not also send Telegram
progress or result messages.

The map shows resolved restaurants as selectable pins. Repeated saves with the
same Google Place ID share one pin while retaining every recommendation and
original source post in the detail sheet. Places without resolved coordinates
remain available in the list.

New saves retain where each place appears in its source. Reel and YouTube
results show the start of the place's main section as a timestamp. Instagram
carousels show a 1-based slide number and open the original post with
Instagram's `img_index` parameter. Caption-only matches leave the media
reference empty rather than guessing.

The map requests foreground-only location access when first opened and starts
at a neighborhood-level view around the device. The standard location control
recenters after browsing elsewhere. A circular search control at the top of the
map expands into a capsule-shaped field and provides Apple Maps suggestions for
cities, neighborhoods, addresses, and points of interest. A circular refresh
control remains beside it; selecting a result moves the camera while leaving
saved-place pins visible.

## Generate and build

1. Copy `Config/Secrets.xcconfig.example` to `Config/Secrets.xcconfig` and set
   `PLACE_LOGGER_API_TOKEN`. The secrets file is ignored by Git.
2. Run `xcodegen generate` from this directory.
3. Build without signing:
   `xcodebuild -project PlaceLogger.xcodeproj -scheme PlaceLogger -sdk iphonesimulator -configuration Debug CODE_SIGNING_ALLOWED=NO build`

Open `PlaceLogger.xcodeproj` only when selecting the Personal Team and
installing on a physical iPhone. All source files can be edited outside Xcode.
