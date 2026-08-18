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
    errorMessage = error.localizedDescription
  }
}
