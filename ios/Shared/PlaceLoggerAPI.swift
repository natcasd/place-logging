import Foundation

struct PlaceLoggerAPI: Sendable {
  private let session: URLSession

  init(session: URLSession = .shared) {
    self.session = session
  }

  func fetchPlaces(limit: Int = 200) async throws -> [SavedPlace] {
    var components = URLComponents(
      url: APIConfig.baseURL.appending(path: "/api/v1/places"),
      resolvingAgainstBaseURL: false
    )
    components?.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
    guard let url = components?.url else { throw PlaceLoggerError.invalidResponse }
    let data = try await perform(URLRequest(url: url))
    return try JSONDecoder().decode(PlacesEnvelope.self, from: data).places
  }

  func ingest(sourceURL: URL) async throws -> IngestResponse {
    let url = APIConfig.baseURL.appending(path: "/api/v1/ingests")
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.timeoutInterval = 180
    request.httpBody = try JSONSerialization.data(withJSONObject: [
      "source_url": sourceURL.absoluteString,
      "delivery": "telegram",
    ])
    let data = try await perform(request)
    return try JSONDecoder().decode(IngestResponse.self, from: data)
  }

  private func perform(_ originalRequest: URLRequest) async throws -> Data {
    guard !APIConfig.token.isEmpty else { throw PlaceLoggerError.missingToken }
    var request = originalRequest
    request.setValue("Bearer \(APIConfig.token)", forHTTPHeaderField: "Authorization")
    let (data, response) = try await session.data(for: request)
    guard let http = response as? HTTPURLResponse else {
      throw PlaceLoggerError.invalidResponse
    }
    guard (200..<300).contains(http.statusCode) else {
      let detail = try? JSONDecoder().decode(APIErrorEnvelope.self, from: data).detail
      throw PlaceLoggerError.server(status: http.statusCode, detail: detail)
    }
    return data
  }
}
