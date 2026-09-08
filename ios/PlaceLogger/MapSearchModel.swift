import MapKit

struct MapSearchSuggestion: Identifiable {
  let completion: MKLocalSearchCompletion

  var id: String { "\(completion.title)\u{0}\(completion.subtitle)" }
  var title: String { completion.title }
  var subtitle: String { completion.subtitle }
}

@MainActor
final class MapSearchModel: NSObject, ObservableObject {
  @Published private(set) var suggestions: [MapSearchSuggestion] = []
  @Published var errorMessage: String?

  private let completer = MKLocalSearchCompleter()
  private var activeSearch: MKLocalSearch?

  override init() {
    super.init()
    completer.delegate = self
    completer.resultTypes = [.address, .pointOfInterest]
  }

  func updateQuery(_ query: String, region: MKCoordinateRegion?) {
    if let region {
      completer.region = region
    }
    let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
    completer.queryFragment = trimmed
    if trimmed.isEmpty {
      suggestions = []
      errorMessage = nil
    }
  }

  func clearSuggestions() {
    completer.queryFragment = ""
    suggestions = []
  }

  func resolve(_ suggestion: MapSearchSuggestion) async -> MKMapItem? {
    await run(MKLocalSearch.Request(completion: suggestion.completion))
  }

  func search(_ query: String, region: MKCoordinateRegion?) async -> MKMapItem? {
    let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else { return nil }
    let request = MKLocalSearch.Request()
    request.naturalLanguageQuery = trimmed
    request.resultTypes = [.address, .pointOfInterest]
    if let region {
      request.region = region
    }
    return await run(request)
  }

  private func run(_ request: MKLocalSearch.Request) async -> MKMapItem? {
    activeSearch?.cancel()
    errorMessage = nil
    let search = MKLocalSearch(request: request)
    activeSearch = search
    do {
      let response = try await search.start()
      guard activeSearch === search else { return nil }
      activeSearch = nil
      errorMessage = nil
      suggestions = []
      return response.mapItems.first
    } catch is CancellationError {
      return nil
    } catch {
      guard activeSearch === search else { return nil }
      activeSearch = nil
      errorMessage = error.localizedDescription
      return nil
    }
  }
}

extension MapSearchModel: @preconcurrency MKLocalSearchCompleterDelegate {
  func completerDidUpdateResults(_ completer: MKLocalSearchCompleter) {
    suggestions = completer.results.prefix(8).map(MapSearchSuggestion.init)
  }

  func completer(_ completer: MKLocalSearchCompleter, didFailWithError error: Error) {
    suggestions = []
    // Autocomplete is best-effort. Clearing or replacing its query can report
    // a transient failure even while the user's submitted search succeeds.
    // Only MKLocalSearch failures from run(_:) should trigger the alert.
  }
}

/// Finds an Apple Maps point of interest only when it agrees with the
/// canonical location Jot has already saved. A failed or ambiguous lookup is
/// deliberately not an error: callers fall back to opening that saved
/// coordinate instead of sending someone to a merely nearby business.
enum AppleMapsDestinationResolver {
  private static let strictCoordinateDistance: CLLocationDistance = 75
  private static let addressVerifiedDistance: CLLocationDistance = 400

  static func makeSearch(for entry: SavedEntry) -> MKLocalSearch? {
    guard let coordinate = entry.coordinate else { return nil }

    let name = entry.mapVenueName
    guard !name.isEmpty else { return nil }

    let request = MKLocalSearch.Request()
    request.naturalLanguageQuery = [name, entry.formattedAddress]
      .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
      .filter { !$0.isEmpty }
      .joined(separator: ", ")
    request.region = MKCoordinateRegion(
      center: coordinate,
      latitudinalMeters: 1_000,
      longitudinalMeters: 1_000
    )
    request.resultTypes = [.pointOfInterest]
    if #available(iOS 18.0, *) {
      request.regionPriority = .required
    }

    return MKLocalSearch(request: request)
  }

  static func resolvedMapItem(for entry: SavedEntry, using search: MKLocalSearch) async -> MKMapItem? {
    guard let coordinate = entry.coordinate else { return nil }
    do {
      let response = try await search.start()
      let matches = response.mapItems.filter { isDirectMatch($0, for: entry, coordinate: coordinate) }
      return matches.count == 1 ? matches[0] : nil
    } catch {
      return nil
    }
  }

  private static func isDirectMatch(
    _ item: MKMapItem,
    for entry: SavedEntry,
    coordinate: CLLocationCoordinate2D
  ) -> Bool {
    guard namesMatch(item.name, entry.mapVenueName) else { return false }

    let candidateCoordinate = item.placemark.coordinate
    guard CLLocationCoordinate2DIsValid(candidateCoordinate) else { return false }
    let distance = CLLocation(latitude: coordinate.latitude, longitude: coordinate.longitude)
      .distance(from: CLLocation(latitude: candidateCoordinate.latitude, longitude: candidateCoordinate.longitude))

    if distance <= strictCoordinateDistance { return true }
    return distance <= addressVerifiedDistance && addressMatches(item, expected: entry.formattedAddress)
  }

  private static func namesMatch(_ candidate: String?, _ expected: String) -> Bool {
    guard let candidate else { return false }
    let normalizedCandidate = normalize(candidate)
    let normalizedExpected = normalize(expected)
    guard !normalizedCandidate.isEmpty, !normalizedExpected.isEmpty else { return false }
    return normalizedCandidate == normalizedExpected
  }

  private static func addressMatches(_ item: MKMapItem, expected: String?) -> Bool {
    guard let expected else { return false }
    let expectedTokens = significantAddressTokens(expected)
    guard !expectedTokens.isEmpty else { return false }

    let candidateTokens = significantAddressTokens(item.placemark.title ?? "")
    let expectedNumbers = expectedTokens.filter { $0.allSatisfy(\.isNumber) }
    let expectedWords = expectedTokens.filter { !$0.allSatisfy(\.isNumber) }

    // Apple often omits a postal code from a result's compact title, so require
    // a shared numeric component (normally the street number), not every one.
    guard expectedNumbers.isEmpty || expectedNumbers.contains(where: candidateTokens.contains) else {
      return false
    }
    return expectedWords.filter(candidateTokens.contains).count >= min(2, expectedWords.count)
  }

  private static func significantAddressTokens(_ value: String) -> Set<String> {
    let ignored = ["the", "and", "new", "york", "usa", "united", "states"]
    return Set(
      normalize(value)
        .split(separator: " ")
        .map(String.init)
        .filter { token in
          token.allSatisfy(\.isNumber) || (token.count >= 3 && !ignored.contains(token))
        }
    )
  }

  private static func normalize(_ value: String) -> String {
    value
      .folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: .current)
      .unicodeScalars
      .map { CharacterSet.alphanumerics.contains($0) ? Character(String($0)) : " " }
      .reduce(into: "") { $0.append($1) }
      .split(whereSeparator: \.isWhitespace)
      .joined(separator: " ")
  }
}

/// Keeps Apple Maps lookups off the interaction path. It shares an in-flight
/// lookup with a Maps tap, retains completed answers for the current map
/// session, and lets a disappearing detail view cancel work that is no longer
/// useful.
@MainActor
final class AppleMapsDestinationCache: ObservableObject {
  private enum Destination {
    case mapItem(MKMapItem)
    case fallback

    var mapItem: MKMapItem? {
      if case let .mapItem(item) = self { return item }
      return nil
    }
  }

  private struct Key: Hashable {
    let entryID: Int
    let name: String
    let address: String?
    let latitude: Double?
    let longitude: Double?

    init(_ entry: SavedEntry) {
      entryID = entry.id
      name = entry.mapVenueName
      address = entry.formattedAddress
      latitude = entry.latitude
      longitude = entry.longitude
    }
  }

  private struct Pending {
    let id: UUID
    let search: MKLocalSearch
    let task: Task<Destination, Never>
  }

  private var cached: [Key: Destination] = [:]
  private var pending: [Key: Pending] = [:]

  func prefetch(_ entry: SavedEntry) async {
    _ = await mapItem(for: entry)
  }

  func mapItem(for entry: SavedEntry) async -> MKMapItem? {
    let key = Key(entry)
    if let cached = cached[key] {
      return cached.mapItem
    }
    if let pending = pending[key] {
      return (await pending.task.value).mapItem
    }
    guard let search = AppleMapsDestinationResolver.makeSearch(for: entry) else {
      cached[key] = .fallback
      return nil
    }

    let requestID = UUID()
    let task = Task { [entry, search] in
      if let mapItem = await AppleMapsDestinationResolver.resolvedMapItem(for: entry, using: search) {
        return Destination.mapItem(mapItem)
      }
      return Destination.fallback
    }
    pending[key] = Pending(id: requestID, search: search, task: task)

    let destination = await task.value
    guard pending[key]?.id == requestID else { return destination.mapItem }
    pending.removeValue(forKey: key)
    if !task.isCancelled {
      cached[key] = destination
    }
    return destination.mapItem
  }

  func cancelPrefetch(for entry: SavedEntry) {
    let key = Key(entry)
    guard let pending = pending.removeValue(forKey: key) else { return }
    pending.search.cancel()
    pending.task.cancel()
  }
}

private extension SavedEntry {
  var coordinate: CLLocationCoordinate2D? {
    guard let latitude, let longitude else { return nil }
    let coordinate = CLLocationCoordinate2D(latitude: latitude, longitude: longitude)
    return CLLocationCoordinate2DIsValid(coordinate) ? coordinate : nil
  }

  var mapVenueName: String {
    let locationName = locationName?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    return locationName.isEmpty ? name : locationName
  }
}
