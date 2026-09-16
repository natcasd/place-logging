import Foundation
import UserNotifications

/// A background download lets iOS deliver the result to the containing app even
/// after the share extension exits. No polling task depends on the sheet's life.
@MainActor
final class SaveCompletionNotifications: NSObject, URLSessionDownloadDelegate {
  static let identifier = "com.natcasd.placelogger.share.save-results"
  static let appGroup = "group.com.natcasd.placelogger"
  private static var instance: SaveCompletionNotifications?
  private var completion: (() -> Void)?
  private var pendingCallbacks = 0
  private var finishedEvents = false
  private var receivedDownloads: Set<Int> = []

  private lazy var session: URLSession = {
    let configuration = URLSessionConfiguration.background(withIdentifier: Self.identifier)
    configuration.sharedContainerIdentifier = Self.appGroup
    configuration.isDiscretionary = false
    configuration.sessionSendsLaunchEvents = true
    configuration.timeoutIntervalForRequest = 60
    configuration.timeoutIntervalForResource = 3600
    configuration.httpCookieStorage = nil
    configuration.urlCredentialStorage = nil
    configuration.urlCache = nil
    configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
    return URLSession(configuration: configuration, delegate: self, delegateQueue: .main)
  }()

  private static func coordinator() -> SaveCompletionNotifications {
    if let instance { return instance }
    let value = SaveCompletionNotifications()
    instance = value
    return value
  }

  static func reconnect(identifier: String, completion: @escaping () -> Void) {
    guard identifier == Self.identifier else { completion(); return }
    AccountSession.shared.configure()
    let value = coordinator()
    value.completion = completion
    _ = value.session
    value.finishIfReady()
  }

  static func follow(_ result: IngestResponse, account: AccountSessionSnapshot) async throws {
    try await AccountSession.shared.validate(account)
    let context = SaveNotificationContext(account: account, ingestID: result.ingestID, startedAt: Date())
    if context.accepts(result) {
      await notify(result, context: context)
      return
    }
    guard FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: appGroup) != nil else {
      throw PlaceLoggerError.notificationHandoffUnavailable
    }
    let value = coordinator()
    let tasks = await value.session.allTasks
    if tasks.contains(where: { task in
      guard let existing = decode(task.taskDescription) else { return false }
      return existing.account == account && existing.ingestID == result.ingestID
    }) { return }
    try await value.schedule(context)
  }

  private func schedule(_ context: SaveNotificationContext, forceRefresh: Bool = false) async throws {
    let token = try await AccountSession.shared.token(for: context.account, forceRefresh: forceRefresh)
    try await AccountSession.shared.validate(context.account)
    var components = URLComponents(url: APIConfig.baseURL.appending(path: "/api/v1/ingests/\(context.ingestID)"), resolvingAgainstBaseURL: false)!
    components.queryItems = [URLQueryItem(name: "wait_seconds", value: "25")]
    var request = URLRequest(url: components.url!)
    request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
    request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
    request.httpShouldHandleCookies = false
    let task = session.downloadTask(with: request)
    task.taskDescription = String(data: try JSONEncoder().encode(context), encoding: .utf8)
    task.resume()
  }

  private nonisolated static func decode(_ text: String?) -> SaveNotificationContext? {
    guard let data = text?.data(using: .utf8) else { return nil }
    return try? JSONDecoder().decode(SaveNotificationContext.self, from: data)
  }

  nonisolated func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                              didFinishDownloadingTo location: URL) {
    guard let context = Self.decode(downloadTask.taskDescription) else { return }
    let status = (downloadTask.response as? HTTPURLResponse)?.statusCode ?? 0
    // The URLSession temporary file exists only during this callback.
    let size = (try? location.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? Int.max
    let data = size <= 1_000_000 ? try? Data(contentsOf: location) : nil
    MainActor.assumeIsolated {
      receivedDownloads.insert(downloadTask.taskIdentifier)
      pendingCallbacks += 1
      Task {
        await received(data, status: status, context: context)
        pendingCallbacks -= 1
        finishIfReady()
      }
    }
  }

  nonisolated func urlSession(_ session: URLSession, task: URLSessionTask,
                              didCompleteWithError error: Error?) {
    MainActor.assumeIsolated {
      guard receivedDownloads.remove(task.taskIdentifier) == nil,
            error != nil, let context = Self.decode(task.taskDescription) else { return }
      pendingCallbacks += 1
      Task {
        await retry(context)
        pendingCallbacks -= 1
        finishIfReady()
      }
    }
  }

  nonisolated func urlSession(_ session: URLSession, task: URLSessionTask,
                              willPerformHTTPRedirection response: HTTPURLResponse,
                              newRequest request: URLRequest,
                              completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
    completionHandler(nil)
  }

  nonisolated func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
    MainActor.assumeIsolated {
      finishedEvents = true
      finishIfReady()
    }
  }

  private func finishIfReady() {
    guard finishedEvents, pendingCallbacks == 0, let completion else { return }
    self.completion = nil
    finishedEvents = false
    completion()
  }

  private func received(_ data: Data?, status: Int, context: SaveNotificationContext) async {
    guard (try? await AccountSession.shared.validate(context.account)) != nil else { return }
    if status == 404 { return } // The user removed this operation.
    if status == 401 {
      await retry(context, forceRefresh: true)
      return
    }
    guard status == 200, let data,
          let result = try? JSONDecoder().decode(IngestResponse.self, from: data),
          result.ingestID == context.ingestID else {
      await retry(context)
      return
    }
    if context.accepts(result) {
      await Self.notify(result, context: context)
    } else if ["queued", "processing", "retry_scheduled"].contains(result.status ?? ""),
              Date().timeIntervalSince(context.startedAt) < 3600 {
      var next = context
      next.failures = 0
      do { try await schedule(next) } catch { await unavailable(context) }
    } else {
      await unavailable(context)
    }
  }

  private func retry(_ context: SaveNotificationContext, forceRefresh: Bool = false) async {
    guard (try? await AccountSession.shared.validate(context.account)) != nil else { return }
    guard context.failures < 2, Date().timeIntervalSince(context.startedAt) < 3600 else {
      await unavailable(context)
      return
    }
    var next = context
    next.failures += 1
    do { try await schedule(next, forceRefresh: forceRefresh) }
    catch { await unavailable(context) }
  }

  private func unavailable(_ context: SaveNotificationContext) async {
    await Self.send(title: "Couldn't check your save", body: "Your post may still be processing. Open Activity in Jot to check.",
                    ingestID: context.ingestID, account: context.account)
  }

  private static func notify(_ result: IngestResponse, context: SaveNotificationContext) async {
    await send(title: result.notificationTitle, body: result.notificationBody,
               ingestID: result.ingestID, itemID: result.itemID,
               entry: result.savedEntries.count == 1 ? result.savedEntries.first : nil,
               account: context.account)
  }

  static func send(title: String, body: String, ingestID: Int? = nil, itemID: Int? = nil,
                   entry: SavedEntryOutcome? = nil, account: AccountSessionSnapshot) async {
    let center = UNUserNotificationCenter.current()
    guard (try? await AccountSession.shared.validate(account)) != nil else { return }
    let settings = await center.notificationSettings()
    guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else { return }
    let content = UNMutableNotificationContent()
    content.title = title
    content.body = body
    content.sound = .default
    var info: [String: Any] = ["account_uid": account.userID, "account_generation": account.generation.uuidString]
    if let ingestID { info["ingest_id"] = ingestID }
    if let itemID { info["item_id"] = itemID }
    if let entry { info["entry_id"] = entry.entryID; info["has_location"] = entry.hasLocation }
    content.userInfo = info
    let identifier = "jot.save.\(account.generation).\(ingestID.map(String.init) ?? UUID().uuidString)"
    let request = UNNotificationRequest(identifier: identifier, content: content,
                                         trigger: UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false))
    guard (try? await AccountSession.shared.validate(account)) != nil else { return }
    try? await center.add(request)
    if (try? await AccountSession.shared.validate(account)) == nil {
      center.removePendingNotificationRequests(withIdentifiers: [identifier])
      center.removeDeliveredNotifications(withIdentifiers: [identifier])
    }
  }
}
