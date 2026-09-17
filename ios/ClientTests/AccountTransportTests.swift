import Foundation
import XCTest
@testable import JotClientCore

private actor TestAuthorizer: AccountAuthorizer {
  var current: AccountSessionSnapshot?
  var refreshed: [Bool] = []
  var invalidations = 0
  var switchDuringToken = false

  init(_ snapshot: AccountSessionSnapshot) { current = snapshot }
  func changeAccount(to snapshot: AccountSessionSnapshot? = nil) { current = snapshot }
  func changeDuringToken() { switchDuringToken = true }
  func token(for snapshot: AccountSessionSnapshot, forceRefresh: Bool) async throws -> String {
    try await validate(snapshot)
    refreshed.append(forceRefresh)
    if switchDuringToken { current = nil }
    return forceRefresh ? "fresh-token" : "initial-token"
  }
  func validate(_ snapshot: AccountSessionSnapshot) async throws {
    guard current == snapshot else { throw PlaceLoggerError.sessionChanged }
  }
  func invalidate(_ snapshot: AccountSessionSnapshot) async throws {
    try await validate(snapshot)
    invalidations += 1
    current = nil
  }
  func state() -> ([Bool], Int) { (refreshed, invalidations) }
}

private actor Requests {
  var values: [URLRequest] = []
  func record(_ request: URLRequest) -> Int { values.append(request); return values.count }
  func all() -> [URLRequest] { values }
}

private final class StubProtocol: URLProtocol, @unchecked Sendable {
  typealias Handler = @Sendable (URLRequest) async throws -> (Int, Data)
  private static let lock = NSLock()
  nonisolated(unsafe) private static var handler: Handler?
  private var loadingTask: Task<Void, Never>?

  static func install(_ value: @escaping Handler) { lock.withLock { handler = value } }
  override class func canInit(with request: URLRequest) -> Bool { true }
  override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
  override func startLoading() {
    let callback = Self.lock.withLock { Self.handler! }
    var outgoing = request
    if outgoing.httpBody == nil, let stream = outgoing.httpBodyStream {
      stream.open()
      var body = Data()
      var buffer = [UInt8](repeating: 0, count: 1024)
      while true {
        let count = stream.read(&buffer, maxLength: buffer.count)
        if count <= 0 { break }
        body.append(contentsOf: buffer.prefix(count))
      }
      stream.close()
      outgoing.httpBody = body
    }
    let captured = outgoing
    loadingTask = Task {
      do {
        let (status, data) = try await callback(captured)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
      } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
  }
  override func stopLoading() { loadingTask?.cancel() }
}

@MainActor
final class AccountTransportTests: XCTestCase {
  let account = AccountSessionSnapshot(projectID: "test-project", userID: "account-a", generation: UUID())

  private func api(_ authorizer: TestAuthorizer) -> PlaceLoggerAPI {
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [StubProtocol.self]
    return PlaceLoggerAPI(account: account, authorizer: authorizer,
                          session: URLSession(configuration: config), baseURL: URL(string: "https://jot.test")!)
  }

  func testAccountDeletionUsesFreshTokenAndDeleteEndpoint() async throws {
    let auth = TestAuthorizer(account)
    let requests = Requests()
    StubProtocol.install { request in
      _ = await requests.record(request)
      return (202, Data(#"{"status":"deletion_requested"}"#.utf8))
    }
    try await api(auth).deleteAccount()
    let recorded = await requests.all()
    XCTAssertEqual(recorded.count, 1)
    XCTAssertEqual(recorded.first?.httpMethod, "DELETE")
    XCTAssertEqual(recorded.first?.url?.path, "/api/v1/account")
    XCTAssertEqual(recorded.first?.value(forHTTPHeaderField: "Authorization"), "Bearer fresh-token")
    let state = await auth.state()
    XCTAssertEqual(state.0, [true])
    XCTAssertEqual(state.1, 0)
  }

  func testDeletionReauthenticationFailureKeepsSession() async throws {
    let auth = TestAuthorizer(account)
    StubProtocol.install { _ in (403, Data(#"{"detail":"Sign in again","code":"recent_sign_in_required"}"#.utf8)) }
    do { try await api(auth).deleteAccount(); XCTFail("Accepted stale login") }
    catch PlaceLoggerError.server(let status, _) { XCTAssertEqual(status, 403) }
    let state = await auth.state()
    XCTAssertEqual(state.0, [true])
    XCTAssertEqual(state.1, 0)
    try await auth.validate(account)
  }

  func testAccountChangeDuringDeletionRefreshPreventsSendingRequest() async throws {
    let auth = TestAuthorizer(account)
    await auth.changeDuringToken()
    let requests = Requests()
    StubProtocol.install { request in
      _ = await requests.record(request)
      return (202, Data())
    }
    do { try await api(auth).deleteAccount(); XCTFail("Deleted after account changed") }
    catch PlaceLoggerError.sessionChanged { }
    let recorded = await requests.all()
    XCTAssertTrue(recorded.isEmpty)
  }

  func testTokenRefreshReusesSameSaveKeyAndBody() async throws {
    let auth = TestAuthorizer(account)
    let requests = Requests()
    StubProtocol.install { request in
      let n = await requests.record(request)
      return (n == 1 ? 401 : 202,
              Data(#"{"ingest_id":42,"item_id":7,"status":"queued","accepted_sequence":1,"saved_entries":[]}"#.utf8))
    }
    let result = try await api(auth).ingest(sourceURL: URL(string: "https://youtu.be/post")!, requestKey: "same-intent")
    XCTAssertEqual(result.status, "queued")
    XCTAssertEqual(result.notificationTitle, "Post accepted")
    let recorded = await requests.all()
    XCTAssertEqual(recorded.count, 2)
    XCTAssertEqual(recorded[0].httpBody, recorded[1].httpBody)
    let body = try JSONSerialization.jsonObject(with: XCTUnwrap(recorded[0].httpBody)) as? [String: String]
    XCTAssertEqual(body?["request_key"], "same-intent")
    XCTAssertEqual(URLComponents(url: recorded[0].url!, resolvingAgainstBaseURL: false)?.queryItems,
                   [URLQueryItem(name: "wait_seconds", value: "150")])
    XCTAssertEqual(recorded[0].timeoutInterval, 180)
    XCTAssertEqual(body?["source_url"], "https://youtu.be/post")
    XCTAssertEqual(recorded[0].value(forHTTPHeaderField: "Authorization"), "Bearer initial-token")
    XCTAssertEqual(recorded[1].value(forHTTPHeaderField: "Authorization"), "Bearer fresh-token")
    XCTAssertEqual(recorded[0].value(forHTTPHeaderField: "Cache-Control"), "no-store")
    XCTAssertFalse(recorded[0].httpShouldHandleCookies)
    let state = await auth.state()
    XCTAssertEqual(state.0, [false, true])
    XCTAssertEqual(state.1, 0)
  }

  func testAcceptanceAndResultWaitReuseOneSaveIntent() async throws {
    let auth = TestAuthorizer(account)
    let requests = Requests()
    StubProtocol.install { request in
      _ = await requests.record(request)
      return (202, Data(#"{"ingest_id":42,"item_id":7,"status":"queued","accepted_sequence":1,"saved_entries":[]}"#.utf8))
    }
    let client = api(auth)
    let url = URL(string: "https://youtu.be/post")!
    _ = try await client.ingest(sourceURL: url, requestKey: "one-share", waitForResult: false)
    _ = try await client.ingest(sourceURL: url, requestKey: "one-share")
    let recorded = await requests.all()
    XCTAssertEqual(recorded.count, 2)
    let firstBody = try JSONSerialization.jsonObject(with: XCTUnwrap(recorded[0].httpBody)) as? [String: String]
    let secondBody = try JSONSerialization.jsonObject(with: XCTUnwrap(recorded[1].httpBody)) as? [String: String]
    XCTAssertEqual(firstBody, secondBody)
    XCTAssertEqual(URLComponents(url: recorded[0].url!, resolvingAgainstBaseURL: false)?.queryItems,
                   [URLQueryItem(name: "wait_seconds", value: "0")])
    XCTAssertEqual(URLComponents(url: recorded[1].url!, resolvingAgainstBaseURL: false)?.queryItems,
                   [URLQueryItem(name: "wait_seconds", value: "150")])
  }

  func testOldResponseCannotPopulateNewSession() async throws {
    let auth = TestAuthorizer(account)
    StubProtocol.install { _ in
      await auth.changeAccount()
      return (200, Data(#"{"entries":[]}"#.utf8))
    }
    do { _ = try await api(auth).fetchEntries(); XCTFail("Accepted an old-account response") }
    catch PlaceLoggerError.sessionChanged { }
  }

  func testAccountChangeDuringTokenRefreshPreventsSendingRequest() async throws {
    let auth = TestAuthorizer(account)
    await auth.changeDuringToken()
    let requests = Requests()
    StubProtocol.install { request in
      _ = await requests.record(request)
      return (200, Data(#"{"entries":[]}"#.utf8))
    }
    do { _ = try await api(auth).fetchEntries(); XCTFail("Sent after account changed") }
    catch PlaceLoggerError.sessionChanged { }
    let recorded = await requests.all()
    XCTAssertTrue(recorded.isEmpty)
  }

  func testVerificationOutageKeepsSessionWithoutRefreshLoop() async throws {
    let auth = TestAuthorizer(account)
    StubProtocol.install { _ in (503, Data(#"{"detail":"Try again"}"#.utf8)) }
    do { _ = try await api(auth).fetchEntries(); XCTFail("Expected an outage") }
    catch PlaceLoggerError.server(let status, _) { XCTAssertEqual(status, 503) }
    let state = await auth.state()
    XCTAssertEqual(state.0, [false])
    XCTAssertEqual(state.1, 0)
    try await auth.validate(account)
  }

  func testRepeatedUnauthorizedRequiresSignInOnlyForOriginalSession() async throws {
    let auth = TestAuthorizer(account)
    StubProtocol.install { _ in (401, Data()) }
    do { _ = try await api(auth).fetchEntries(); XCTFail("Accepted revoked session") }
    catch PlaceLoggerError.signInRequired { }
    let state = await auth.state()
    XCTAssertEqual(state.0, [false, true])
    XCTAssertEqual(state.1, 1)
  }

  func testAccountSwitchOnUnauthorizedDoesNotSignOutReplacement() async throws {
    let auth = TestAuthorizer(account)
    let replacement = AccountSessionSnapshot(projectID: "test-project", userID: "account-b", generation: UUID())
    StubProtocol.install { _ in
      await auth.changeAccount(to: replacement)
      return (401, Data())
    }
    do { _ = try await api(auth).fetchEntries(); XCTFail("Accepted switched session") }
    catch PlaceLoggerError.sessionChanged { }
    let state = await auth.state()
    XCTAssertEqual(state.0, [false])
    XCTAssertEqual(state.1, 0)
    try await auth.validate(replacement)
  }

  func testLoggingBackIntoSameUIDStillRejectsEarlierResponse() async throws {
    let auth = TestAuthorizer(account)
    let nextLogin = AccountSessionSnapshot(projectID: account.projectID, userID: account.userID, generation: UUID())
    StubProtocol.install { _ in
      await auth.changeAccount(to: nextLogin)
      return (200, Data(#"{"entries":[]}"#.utf8))
    }
    do { _ = try await api(auth).fetchEntries(); XCTFail("Accepted earlier login's response") }
    catch PlaceLoggerError.sessionChanged { }
    try await auth.validate(nextLogin)
  }
}
