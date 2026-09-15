import Foundation

struct PlaceLoggerAPI: Sendable {
  private let baseURL: URL
  private let session: URLSession
  private let account: AccountSessionSnapshot
  private let authorizer: any AccountAuthorizer

  init(account: AccountSessionSnapshot, authorizer: any AccountAuthorizer,
       session: URLSession? = nil, baseURL: URL = APIConfig.baseURL) {
    self.baseURL = baseURL
    self.account = account
    self.authorizer = authorizer
    let configuration = URLSessionConfiguration.ephemeral
    configuration.urlCache = nil
    configuration.httpCookieStorage = nil
    configuration.urlCredentialStorage = nil
    configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
    self.session = session ?? URLSession(configuration: configuration, delegate: RejectRedirects(), delegateQueue: nil)
  }

  func fetchEntries(limit: Int = 1_000) async throws -> [SavedEntry] {
    var components = URLComponents(
      url: baseURL.appending(path: "/api/v1/entries"),
      resolvingAgainstBaseURL: false
    )
    components?.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
    guard let url = components?.url else { throw PlaceLoggerError.invalidResponse }
    let data = try await perform(URLRequest(url: url))
    return try JSONDecoder().decode(EntriesEnvelope.self, from: data).entries
  }

  func fetchActivity(limit: Int = 200) async throws -> [IngestActivity] {
    var components = URLComponents(
      url: baseURL.appending(path: "/api/v1/activity"),
      resolvingAgainstBaseURL: false
    )
    components?.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
    guard let url = components?.url else { throw PlaceLoggerError.invalidResponse }
    let data = try await perform(URLRequest(url: url))
    return try JSONDecoder().decode(ActivityEnvelope.self, from: data).activity
  }

  func confirmActivityLocation(
    ingestID: Int,
    entryID: Int,
    mentionID: Int?,
    candidateID: String
  ) async throws {
    let url = baseURL.appending(
      path: "/api/v1/activity/\(ingestID)/entries/\(entryID)/location"
    )
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    var body: [String: Any] = ["candidate_id": candidateID]
    if let mentionID { body["mention_id"] = mentionID }
    request.httpBody = try JSONSerialization.data(withJSONObject: body)
    _ = try await perform(request)
  }

  func retryActivity(ingestID: Int) async throws {
    let url = baseURL.appending(path: "/api/v1/activity/\(ingestID)/retry")
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.timeoutInterval = 180
    _ = try await perform(request)
  }

  func deleteFailedActivity(ingestID: Int) async throws {
    let url = baseURL.appending(path: "/api/v1/activity/\(ingestID)")
    var request = URLRequest(url: url)
    request.httpMethod = "DELETE"
    _ = try await perform(request)
  }

  func ingest(sourceURL: URL, requestKey: String) async throws -> IngestResponse {
    let url = baseURL.appending(path: "/api/v1/ingests")
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.timeoutInterval = 30
    request.httpBody = try JSONSerialization.data(withJSONObject: [
      "source_url": sourceURL.absoluteString,
      "request_key": requestKey
    ])
    let data = try await perform(request)
    return try JSONDecoder().decode(IngestResponse.self, from: data)
  }

  func deleteEntry(id: Int) async throws {
    let url = baseURL.appending(path: "/api/v1/entries/\(id)")
    var request = URLRequest(url: url)
    request.httpMethod = "DELETE"
    _ = try await perform(request)
  }

  func deleteMention(id: Int) async throws {
    var request = URLRequest(url: baseURL.appending(path: "/api/v1/mentions/\(id)"))
    request.httpMethod = "DELETE"
    _ = try await perform(request)
  }

  private func perform(_ originalRequest: URLRequest) async throws -> Data {
    for forceRefresh in [false, true] {
      let token = try await authorizer.token(for: account, forceRefresh: forceRefresh)
      try Task.checkCancellation()
      try await authorizer.validate(account)
      var request = originalRequest
      request.cachePolicy = .reloadIgnoringLocalCacheData
      request.httpShouldHandleCookies = false
      request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
      request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
      let (data, response) = try await session.data(for: request)
      try Task.checkCancellation()
      try await authorizer.validate(account)
      guard let http = response as? HTTPURLResponse else { throw PlaceLoggerError.invalidResponse }
      if http.statusCode == 401 {
        if !forceRefresh { continue }
        try await authorizer.invalidate(account)
        throw PlaceLoggerError.signInRequired
      }
      guard (200..<300).contains(http.statusCode) else {
        let detail = try? JSONDecoder().decode(APIErrorEnvelope.self, from: data).detail
        throw PlaceLoggerError.server(status: http.statusCode, detail: detail)
      }
      return data
    }
    throw PlaceLoggerError.signInRequired
  }
}

private final class RejectRedirects: NSObject, URLSessionTaskDelegate {
  func urlSession(_ session: URLSession, task: URLSessionTask,
                  willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                  completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
    completionHandler(nil)
  }
}
