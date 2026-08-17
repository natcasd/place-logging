import Foundation

struct PlacesEnvelope: Decodable {
  let places: [SavedPlace]
}

struct SavedPlace: Decodable, Identifiable, Sendable {
  let id: Int
  let itemID: Int
  let ordinal: Int
  let name: String
  let googlePlaceID: String?
  let latitude: Double?
  let longitude: Double?
  let formattedAddress: String?
  let googleMapsURL: URL?
  let dishes: [String]
  let whyItsCool: String
  let tags: [String]
  let resolutionStatus: String
  let sourceURL: URL
  let savedAt: String

  enum CodingKeys: String, CodingKey {
    case id, ordinal, name, latitude, longitude, dishes, tags
    case itemID = "item_id"
    case googlePlaceID = "google_place_id"
    case formattedAddress = "formatted_address"
    case googleMapsURL = "google_maps_url"
    case whyItsCool = "why_its_cool"
    case resolutionStatus = "resolution_status"
    case sourceURL = "source_url"
    case savedAt = "saved_at"
  }
}

struct IngestResponse: Decodable, Sendable {
  let itemID: Int
  let resolvedPlaces: [IngestResolvedPlace]

  var savedPlaceNames: [String] {
    resolvedPlaces.compactMap(\.displayName)
  }

  enum CodingKeys: String, CodingKey {
    case itemID = "item_id"
    case resolvedPlaces = "resolved_places"
  }
}

struct IngestResolvedPlace: Decodable, Sendable {
  let extracted: IngestExtractedPlace?
  let place: IngestGooglePlace?

  var displayName: String? {
    let name = place?.displayName?.text ?? extracted?.extractedName
    guard let name, !name.isEmpty else { return nil }
    return name
  }
}

struct IngestExtractedPlace: Decodable, Sendable {
  let extractedName: String?

  enum CodingKeys: String, CodingKey {
    case extractedName = "extracted_name"
  }
}

struct IngestGooglePlace: Decodable, Sendable {
  let displayName: IngestGoogleDisplayName?
}

struct IngestGoogleDisplayName: Decodable, Sendable {
  let text: String?
}

struct APIErrorEnvelope: Decodable {
  let detail: String?
}

enum PlaceLoggerError: LocalizedError {
  case missingToken
  case invalidResponse
  case server(status: Int, detail: String?)
  case noSharedURL

  var errorDescription: String? {
    switch self {
    case .missingToken:
      "The app's API token is not configured."
    case .invalidResponse:
      "Place Logger returned an invalid response."
    case .server(let status, let detail):
      detail ?? "Place Logger returned HTTP \(status)."
    case .noSharedURL:
      "Instagram did not include a usable link in this share."
    }
  }
}
