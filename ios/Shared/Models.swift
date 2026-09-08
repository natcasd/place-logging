import Foundation

struct PlacesEnvelope: Decodable {
  let places: [SavedEntry]
}

struct EntriesEnvelope: Decodable {
  let entries: [SavedEntry]
}

struct SourcesEnvelope: Decodable {
  let sources: [SavedSource]
}

struct ActivityEnvelope: Decodable {
  let activity: [IngestActivity]
}

struct SavedEntry: Decodable, Identifiable, Sendable {
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
  let sources: [SavedEntrySource]

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

  /// A precise-location fallback for Apple Maps when its place search cannot
  /// confidently identify the venue. With `ll`, Apple Maps uses `q` only as
  /// the pin's label, so this intentionally does not claim to be a listing.
  var appleMapsFallbackURL: URL? {
    var components = URLComponents(string: "https://maps.apple.com/")
    let venueName = locationName?.trimmingCharacters(in: .whitespacesAndNewlines)
    let queryName = venueName.flatMap { $0.isEmpty ? nil : $0 } ?? name
    if let latitude, let longitude {
      components?.queryItems = [
        URLQueryItem(name: "q", value: queryName),
        URLQueryItem(name: "ll", value: "\(latitude),\(longitude)"),
      ]
    } else if let formattedAddress {
      components?.queryItems = [URLQueryItem(name: "address", value: formattedAddress)]
    } else {
      return nil
    }
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

struct SavedEntrySource: Decodable, Identifiable, Sendable {
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
  let entryCount: Int
  let needsReview: Bool
  let savedAt: String

  enum CodingKeys: String, CodingKey {
    case id, creator, caption, summary
    case sourceURL = "source_url"
    case sourcePlatform = "source_platform"
    case mediaCount = "media_count"
    case mediaPreserved = "media_preserved"
    case entryCount = "entry_count"
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
  let savedEntries: [SavedEntryOutcome]
  let alreadyLogged: Bool?

  var notificationTitle: String {
    if alreadyLogged == true { return "Already logged" }
    guard savedEntries.count == 1, let entry = savedEntries.first else {
      return savedEntries.isEmpty ? "Nothing found" : "Logged " + Self.typeCountSummary(savedEntries)
    }
    return entry.isNew
      ? "Logged \(entry.type) · \(entry.name)"
      : "Logged a new source to \(entry.type) - \(entry.name)"
  }

  var notificationBody: String {
    if alreadyLogged == true { return "" }
    guard !savedEntries.isEmpty else {
      return "The source was saved for review."
    }
    if savedEntries.count == 1, let entry = savedEntries.first {
      return entry.isNew
        ? "Logged a new \(entry.type) from this post."
        : "This \(entry.type) now has \(entry.sourceCount) logged sources."
    }
    return ""
  }

  enum CodingKeys: String, CodingKey {
    case ingestID = "ingest_id"
    case itemID = "item_id"
    case savedEntries = "saved_entries"
    case alreadyLogged = "already_logged"
  }

  private static func typeCountSummary(_ entries: [SavedEntryOutcome]) -> String {
    var orderedTypes: [String] = []
    var counts: [String: Int] = [:]

    for entry in entries {
      let type = entry.type.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
      let label = type.isEmpty ? "entry" : type
      if counts[label] == nil {
        orderedTypes.append(label)
      }
      counts[label, default: 0] += 1
    }

    return orderedTypes.map { type in
      let count = counts[type, default: 0]
      return "\(count) \(pluralized(type, count: count))"
    }.joined(separator: ", ")
  }

  private static func pluralized(_ type: String, count: Int) -> String {
    guard count != 1 else { return type }

    let splitIndex = type.lastIndex(of: " ")
    let prefix = splitIndex.map { String(type[...$0]) } ?? ""
    let word = splitIndex.map { String(type[type.index(after: $0)...]) } ?? type
    let pluralWord: String

    if word.hasSuffix("y"), word.count > 1 {
      let beforeY = word[word.index(word.endIndex, offsetBy: -2)]
      if !"aeiou".contains(beforeY) {
        pluralWord = String(word.dropLast()) + "ies"
      } else {
        pluralWord = word + "s"
      }
    } else if ["s", "x", "z", "ch", "sh"].contains(where: word.hasSuffix) {
      pluralWord = word + "es"
    } else {
      pluralWord = word + "s"
    }

    return prefix + pluralWord
  }
}

struct ReviewLocationCandidate: Decodable, Identifiable, Sendable, Hashable {
  let id: String
  let name: String
  let formattedAddress: String?
  let latitude: Double?
  let longitude: Double?

  enum CodingKeys: String, CodingKey {
    case id, name, latitude, longitude
    case formattedAddress = "formatted_address"
  }
}

struct SavedEntryOutcome: Decodable, Identifiable, Sendable, Hashable {
  let entryID: Int
  let sourceConnectionID: Int?
  let ordinal: Int
  let name: String
  let type: String
  let locationID: Int?
  let locationName: String?
  let latitude: Double?
  let longitude: Double?
  let formattedAddress: String?
  let googleMapsURL: URL?
  let timestampSeconds: Double?
  let slideIndex: Int?
  let resolutionStatus: String
  let reviewCandidates: [ReviewLocationCandidate]
  let isNew: Bool
  let sourceCount: Int

  var id: Int { entryID }
  var hasLocation: Bool { locationID != nil && latitude != nil && longitude != nil }

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

  enum CodingKeys: String, CodingKey {
    case name, type, ordinal, latitude, longitude
    case entryID = "entry_id"
    case sourceConnectionID = "source_connection_id"
    case locationID = "location_id"
    case locationName = "location_name"
    case formattedAddress = "formatted_address"
    case googleMapsURL = "google_maps_url"
    case timestampSeconds = "timestamp_seconds"
    case slideIndex = "slide_index"
    case resolutionStatus = "resolution_status"
    case reviewCandidates = "review_candidates"
    case isNew = "is_new"
    case sourceCount = "source_count"
  }

  init(from decoder: Decoder) throws {
    let values = try decoder.container(keyedBy: CodingKeys.self)
    entryID = try values.decode(Int.self, forKey: .entryID)
    sourceConnectionID = try values.decodeIfPresent(Int.self, forKey: .sourceConnectionID)
    ordinal = try values.decodeIfPresent(Int.self, forKey: .ordinal) ?? 0
    name = try values.decode(String.self, forKey: .name)
    type = try values.decode(String.self, forKey: .type)
    locationID = try values.decodeIfPresent(Int.self, forKey: .locationID)
    locationName = try values.decodeIfPresent(String.self, forKey: .locationName)
    latitude = try values.decodeIfPresent(Double.self, forKey: .latitude)
    longitude = try values.decodeIfPresent(Double.self, forKey: .longitude)
    formattedAddress = try values.decodeIfPresent(String.self, forKey: .formattedAddress)
    googleMapsURL = try values.decodeIfPresent(URL.self, forKey: .googleMapsURL)
    timestampSeconds = try values.decodeIfPresent(Double.self, forKey: .timestampSeconds)
    slideIndex = try values.decodeIfPresent(Int.self, forKey: .slideIndex)
    resolutionStatus = try values.decode(String.self, forKey: .resolutionStatus)
    reviewCandidates = try values.decodeIfPresent(
      [ReviewLocationCandidate].self,
      forKey: .reviewCandidates
    ) ?? []
    isNew = try values.decode(Bool.self, forKey: .isNew)
    sourceCount = try values.decode(Int.self, forKey: .sourceCount)
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
  let results: [SavedEntryOutcome]
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
      "Jot returned an invalid response."
    case .server(let status, let detail):
      detail ?? "Jot returned HTTP \(status)."
    case .noSharedURL:
      "Instagram did not include a usable link in this share."
    }
  }
}
