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
  let timestampSeconds: Double?
  let slideIndex: Int?
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
    case timestampSeconds = "timestamp_seconds"
    case slideIndex = "slide_index"
    case resolutionStatus = "resolution_status"
    case sourceURL = "source_url"
    case savedAt = "saved_at"
  }

  var mediaReferenceText: String? {
    let timestamp = timestampSeconds.map(Self.formatTimestamp)
    switch (slideIndex, timestamp) {
    case let (.some(slide), .some(time)):
      return "Slide \(slide) · Appears at \(time)"
    case let (.some(slide), .none):
      return "Slide \(slide)"
    case let (.none, .some(time)):
      return "Appears at \(time)"
    case (.none, .none):
      return nil
    }
  }

  var mediaReferenceSystemImage: String {
    slideIndex == nil ? "play.rectangle" : "rectangle.stack"
  }

  var linkedSourceURL: URL {
    guard var components = URLComponents(url: sourceURL, resolvingAgainstBaseURL: false) else {
      return sourceURL
    }
    var items = components.queryItems ?? []

    if let slideIndex {
      items.removeAll { $0.name == "img_index" }
      items.append(URLQueryItem(name: "img_index", value: String(slideIndex)))
    }

    if let timestampSeconds, isYouTubeSource {
      items.removeAll { $0.name == "t" }
      items.append(
        URLQueryItem(name: "t", value: "\(max(0, Int(timestampSeconds.rounded())))s")
      )
    }

    components.queryItems = items
    return components.url ?? sourceURL
  }

  var sourceLinkText: String {
    let host = sourceURL.host?.lowercased() ?? ""
    if host.contains("instagram") { return "Instagram Post" }
    if isYouTubeSource { return "Watch on YouTube" }
    if host.contains("tiktok") { return "Open in TikTok" }
    return "Open original post"
  }

  var sourceSystemImage: String {
    let host = sourceURL.host?.lowercased() ?? ""
    if host.contains("instagram") { return "camera" }
    if isYouTubeSource { return "play.rectangle.fill" }
    if host.contains("tiktok") { return "music.note" }
    return "link"
  }

  var appleMapsURL: URL? {
    guard latitude != nil || formattedAddress != nil else { return nil }
    var components = URLComponents(string: "https://maps.apple.com/")
    var items = [URLQueryItem(name: "q", value: name)]
    if let latitude, let longitude {
      items.append(URLQueryItem(name: "ll", value: "\(latitude),\(longitude)"))
    } else if let formattedAddress {
      items.append(URLQueryItem(name: "address", value: formattedAddress))
    }
    components?.queryItems = items
    return components?.url
  }

  private var isYouTubeSource: Bool {
    let host = sourceURL.host?.lowercased() ?? ""
    return host.contains("youtube.com") || host.contains("youtu.be")
  }

  private static func formatTimestamp(_ value: Double) -> String {
    let totalSeconds = max(0, Int(value.rounded()))
    let hours = totalSeconds / 3600
    let minutes = (totalSeconds % 3600) / 60
    let seconds = totalSeconds % 60
    if hours > 0 {
      return String(format: "%d:%02d:%02d", hours, minutes, seconds)
    }
    return String(format: "%d:%02d", minutes, seconds)
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
