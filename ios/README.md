# Place Logger for iPhone

The native MVP contains two targets:

- `PlaceLogger`: a SwiftUI list of saved restaurants.
- `PlaceLoggerShare`: a native share extension that accepts links and media
  shared by Instagram or YouTube and sends the extracted URL to the existing
  ingest API. The extension can close while the synchronous request continues;
  on completion, it schedules a local notification naming the saved place.

Tapping a successful notification opens the containing app, refreshes the
saved-place list, and navigates to the places created by that ingest. Native
shares use response-only API delivery, so they do not also send Telegram
progress or result messages.

## Generate and build

1. Copy `Config/Secrets.xcconfig.example` to `Config/Secrets.xcconfig` and set
   `PLACE_LOGGER_API_TOKEN`. The secrets file is ignored by Git.
2. Run `xcodegen generate` from this directory.
3. Build without signing:
   `xcodebuild -project PlaceLogger.xcodeproj -scheme PlaceLogger -sdk iphonesimulator -configuration Debug CODE_SIGNING_ALLOWED=NO build`

Open `PlaceLogger.xcodeproj` only when selecting the Personal Team and
installing on a physical iPhone. All source files can be edited outside Xcode.
