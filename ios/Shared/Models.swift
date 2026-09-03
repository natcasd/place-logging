import Foundation

struct PlacesEnvelope: Decodable {
  let places: [SavedPlace]
}

struct ThingsEnvelope: Decodable {
  let things: [SavedPlace]
}

struct SourcesEnvelope: Decodable {
  let sources: [SavedSource]
}

struct ActivityEnvelope: Decodable {
  let activity: [IngestActivity]
}

struct SavedPlace: Decodable, Identifiable, Sendable {
  let id: Int
  let locationID: Int?
  let itemID: Int
  let ordinal: Int
  let name: String
  let googlePlaceID: String?
  let latitude: Double?
  let longitude: Double?
  let formattedAddress: String?
  let googleMapsURL: URL?
  let locationName: String?
  let dishes: [String]
  let whyItsCool: String
  let tags: [String]
  let timestampSeconds: Double?
  let slideIndex: Int?
  let resolutionStatus: String
  let type: String?
  let description: String?
  let startsAt: String?
  let endsAt: String?
  let recurrenceText: String?
  let sourceURL: URL
  let savedAt: String
  let sources: [SavedThingSource]

  enum CodingKeys: String, CodingKey {
    case id, ordinal, name, latitude, longitude, dishes, tags
    case sources
    case locationID = "location_id"
    case itemID = "item_id"
    case googlePlaceID = "google_place_id"
    case formattedAddress = "formatted_address"
    case googleMapsURL = "google_maps_url"
    case locationName = "location_name"
    case whyItsCool = "why_its_cool"
    case timestampSeconds = "timestamp_seconds"
    case slideIndex = "slide_index"
    case resolutionStatus = "resolution_status"
    case type, description
    case startsAt = "starts_at"
    case endsAt = "ends_at"
    case recurrenceText = "recurrence_text"
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

  var displayType: String {
    guard let type, !type.isEmpty else { return "Place" }
    return type
  }

  var detailedDescription: String {
    guard let description, !description.isEmpty else { return whyItsCool }
    return description
  }

  var isCurrentlyRelevant: Bool {
    guard let endsAt, let endDate = Self.parseFlexibleDate(endsAt) else { return true }
    return endDate >= Calendar.current.startOfDay(for: Date())
  }

  var availabilityText: String? {
    if let recurrenceText, !recurrenceText.isEmpty { return recurrenceText }
    switch (startsAt, endsAt) {
    case let (.some(start), .some(end)):
      return "\(start) – \(end)"
    case let (.some(start), .none):
      return "Starts \(start)"
    case let (.none, .some(end)):
      return "Through \(end)"
    case (.none, .none):
      return nil
    }
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

  private static func parseFlexibleDate(_ value: String) -> Date? {
    if let date = ISO8601DateFormatter().date(from: value) { return date }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.dateFormat = "yyyy-MM-dd"
    return formatter.date(from: value)
  }
}

struct SavedThingSource: Decodable, Identifiable, Sendable {
  let id: Int
  let itemID: Int
  let ordinal: Int
  let name: String
  let type: String
  let sourceURL: URL
  let sourcePlatform: String
  let creator: String?
  let description: String
  let dishes: [String]
  let whyItsCool: String
  let tags: [String]
  let timestampSeconds: Double?
  let slideIndex: Int?
  let resolutionStatus: String
  let locationQuery: String?
  let savedAt: String

  enum CodingKeys: String, CodingKey {
    case id, ordinal, name, type, description, dishes, tags
    case itemID = "item_id"
    case sourceURL = "source_url"
    case sourcePlatform = "source_platform"
    case creator
    case whyItsCool = "why_its_cool"
    case timestampSeconds = "timestamp_seconds"
    case slideIndex = "slide_index"
    case resolutionStatus = "resolution_status"
    case locationQuery = "location_query"
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
    if let creator, !creator.isEmpty { return creator }
    let host = sourceURL.host?.lowercased() ?? ""
    if host.contains("instagram") { return "Instagram Post" }
    if isYouTubeSource { return "Watch on YouTube" }
    return "Open original post"
  }

  var sourceSystemImage: String {
    let host = sourceURL.host?.lowercased() ?? ""
    if host.contains("instagram") { return "camera" }
    if isYouTubeSource { return "play.rectangle.fill" }
    return "link"
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

struct SavedSource: Decodable, Identifiable, Sendable {
  let id: Int
  let sourceURL: URL
  let sourcePlatform: String
  let creator: String?
  let caption: String?
  let summary: String?
  let mediaCount: Int
  let mediaPreserved: Bool
  let thingCount: Int
  let needsReview: Bool
  let savedAt: String

  enum CodingKeys: String, CodingKey {
    case id, creator, caption, summary
    case sourceURL = "source_url"
    case sourcePlatform = "source_platform"
    case mediaCount = "media_count"
    case mediaPreserved = "media_preserved"
    case thingCount = "thing_count"
    case needsReview = "needs_review"
    case savedAt = "saved_at"
  }

  var title: String {
    if let creator, !creator.isEmpty { return creator }
    return sourcePlatform.capitalized
  }
}

struct IngestResponse: Decodable, Sendable {
  let ingestID: Int
  let itemID: Int
  let savedThings: [SavedThingOutcome]

  var notificationTitle: String {
    guard savedThings.count == 1, let thing = savedThings.first else {
      return savedThings.isEmpty ? "No things found" : "Saved \(savedThings.count) things"
    }
    return thing.isNew
      ? "Saved \(thing.type) · \(thing.name)"
      : "Added to \(thing.type) · \(thing.name)"
  }

  var notificationBody: String {
    guard !savedThings.isEmpty else {
      return "The source was saved for review."
    }
    if savedThings.count == 1, let thing = savedThings.first {
      return thing.isNew
        ? "Created a new Thing from this post."
        : "This Thing now has \(thing.sourceCount) saved sources."
    }
    let newThings = savedThings.filter(\.isNew)
    let existingThings = savedThings.filter { !$0.isNew }
    var sections: [String] = []
    if !newThings.isEmpty {
      sections.append("New: " + Self.summary(newThings))
    }
    if !existingThings.isEmpty {
      sections.append("Added: " + Self.summary(existingThings))
    }
    return sections.joined(separator: "\n")
  }

  enum CodingKeys: String, CodingKey {
    case ingestID = "ingest_id"
    case itemID = "item_id"
    case savedThings = "saved_things"
  }

  private static func summary(_ things: [SavedThingOutcome]) -> String {
    let visible = things.prefix(3).map { "\($0.type) · \($0.name)" }
    let remaining = things.count - visible.count
    return visible.joined(separator: "; ") + (remaining > 0 ? "; +\(remaining) more" : "")
  }
}

struct SavedThingOutcome: Decodable, Identifiable, Sendable, Hashable {
  let thingID: Int
  let name: String
  let type: String
  let locationID: Int?
  let locationName: String?
  let latitude: Double?
  let longitude: Double?
  let resolutionStatus: String
  let isNew: Bool
  let sourceCount: Int

  var id: Int { thingID }
  var hasLocation: Bool { locationID != nil && latitude != nil && longitude != nil }

  enum CodingKeys: String, CodingKey {
    case name, type, latitude, longitude
    case thingID = "thing_id"
    case locationID = "location_id"
    case locationName = "location_name"
    case resolutionStatus = "resolution_status"
    case isNew = "is_new"
    case sourceCount = "source_count"
  }
}

struct IngestActivity: Decodable, Identifiable, Sendable {
  let id: Int
  let itemID: Int?
  let sourceURL: URL
  let sourcePlatform: String
  let creator: String?
  let caption: String?
  let summary: String?
  let status: String
  let stage: String
  let errorType: String?
  let errorMessage: String?
  let startedAt: String
  let updatedAt: String
  let completedAt: String?
  let results: [SavedThingOutcome]
  let events: [IngestActivityEvent]

  var title: String {
    if let creator, !creator.isEmpty { return creator }
    return sourcePlatform.capitalized
  }

  var statusText: String {
    switch status {
    case "processing": return "Processing · \(stage.replacingOccurrences(of: "_", with: " ").capitalized)"
    case "partial": return "Saved · Needs review"
    case "failed": return "Failed"
    default: return "Saved"
    }
  }

  enum CodingKeys: String, CodingKey {
    case id, creator, caption, summary, status, stage, results, events
    case itemID = "item_id"
    case sourceURL = "source_url"
    case sourcePlatform = "source_platform"
    case errorType = "error_type"
    case errorMessage = "error_message"
    case startedAt = "started_at"
    case updatedAt = "updated_at"
    case completedAt = "completed_at"
  }
}

struct IngestActivityEvent: Decodable, Identifiable, Sendable {
  let id: Int
  let stage: String
  let status: String
  let message: String
  let createdAt: String

  enum CodingKeys: String, CodingKey {
    case id, stage, status, message
    case createdAt = "created_at"
  }
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
