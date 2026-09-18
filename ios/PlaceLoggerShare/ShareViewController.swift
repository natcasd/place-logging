import SwiftUI
import UniformTypeIdentifiers
import UserNotifications
import OSLog

private let shareLogger = Logger(subsystem: "com.natcasd.placelogger.share", category: "ShareFlow")

final class ShareViewController: UIViewController {
  override func viewDidLoad() {
    super.viewDidLoad()
    configureSheet()
    let root = ShareStatusView(
      loadURL: { [weak self] in
        guard let self else { throw PlaceLoggerError.noSharedURL }
        return try await self.sharedURL()
      },
      complete: { [weak self] in
        shareLogger.info("Requesting automatic share dismissal")
        self?.extensionContext?.completeRequest(returningItems: nil)
      },
      cancel: { [weak self] error in
        self?.extensionContext?.cancelRequest(withError: error)
      }
    )
    let host = UIHostingController(rootView: root)
    addChild(host)
    host.view.translatesAutoresizingMaskIntoConstraints = false
    view.addSubview(host.view)
    NSLayoutConstraint.activate([
      host.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
      host.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
      host.view.topAnchor.constraint(equalTo: view.topAnchor),
      host.view.bottomAnchor.constraint(equalTo: view.bottomAnchor),
    ])
    host.didMove(toParent: self)
  }

  override func viewWillAppear(_ animated: Bool) {
    super.viewWillAppear(animated)
    configureSheet()
  }

  override func viewWillLayoutSubviews() {
    super.viewWillLayoutSubviews()
    // The extension's host owns presentation. Give it a compact preferred size
    // even when it does not expose a sheet presentation controller to us.
    let screenHeight = view.window?.windowScene?.coordinateSpace.bounds.height
      ?? view.window?.screen.bounds.height ?? 800
    let height = max(320, screenHeight / 2)
    let size = CGSize(width: view.bounds.width, height: height)
    if preferredContentSize != size { preferredContentSize = size }
  }

  private func configureSheet() {
    preferredContentSize = CGSize(width: 390, height: 400)
    guard let sheet = sheetPresentationController else { return }
    sheet.detents = [.medium(), .large()]
    sheet.selectedDetentIdentifier = .medium
    sheet.prefersGrabberVisible = true
    sheet.prefersScrollingExpandsWhenScrolledToEdge = false
  }

  private func sharedURL() async throws -> URL {
    guard let items = extensionContext?.inputItems as? [NSExtensionItem] else {
      throw PlaceLoggerError.noSharedURL
    }
    let providers = items.compactMap(\.attachments).flatMap { $0 }

    for provider in providers
    where provider.hasItemConformingToTypeIdentifier(UTType.url.identifier) {
      if let value = try? await provider.loadItem(forTypeIdentifier: UTType.url.identifier),
        let url = Self.url(from: value)
      {
        return url
      }
    }

    for type in [
      UTType.plainText.identifier, UTType.text.identifier, UTType.propertyList.identifier,
    ] {
      for provider in providers where provider.hasItemConformingToTypeIdentifier(type) {
        if let value = try? await provider.loadItem(forTypeIdentifier: type),
          let url = Self.url(from: value)
        {
          return url
        }
      }
    }

    for item in items {
      if let text = item.attributedContentText?.string,
        let url = Self.firstHTTPURL(in: text)
      {
        return url
      }
    }
    throw PlaceLoggerError.noSharedURL
  }

  private static func url(from value: NSSecureCoding?) -> URL? {
    if let url = value as? URL, url.scheme?.hasPrefix("http") == true { return url }
    if let string = value as? String { return firstHTTPURL(in: string) }
    if let data = value as? Data, let string = String(data: data, encoding: .utf8) {
      return firstHTTPURL(in: string)
    }
    if let dictionary = value as? [String: Any] {
      for child in dictionary.values {
        if let url = url(from: child as? NSSecureCoding) { return url }
      }
    }
    if let array = value as? [Any] {
      for child in array {
        if let url = url(from: child as? NSSecureCoding) { return url }
      }
    }
    return firstHTTPURL(in: String(describing: value))
  }

  private static func firstHTTPURL(in text: String) -> URL? {
    guard let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)
    else {
      return nil
    }
    let range = NSRange(text.startIndex..., in: text)
    return detector.matches(in: text, range: range)
      .compactMap(\.url)
      .first { $0.scheme == "https" || $0.scheme == "http" }
  }
}

private struct ShareStatusView: View {
  let loadURL: () async throws -> URL
  let complete: () -> Void
  let cancel: (Error) -> Void

  @State private var state: Phase = .starting
  @State private var requestKey = UUID().uuidString

  enum Phase {
    case starting
    case saving(URL)
    case processing
    case failed(String)
  }

  var body: some View {
    VStack(spacing: 18) {
      switch state {
      case .starting:
        ProgressView()
        Text("Reading shared link…")
      case .saving(let url):
        ProgressView()
        Text("Saving to Jot…")
          .font(.headline)
        Text(url.host() ?? url.absoluteString)
          .font(.caption)
          .foregroundStyle(.secondary)
      case .processing:
        ShareProcessingConfirmation()
      case .failed(let message):
        Image(systemName: "exclamationmark.triangle.fill")
          .font(.largeTitle)
          .foregroundStyle(.orange)
        Text("Couldn’t confirm save")
          .font(.headline)
        Text(message)
          .multilineTextAlignment(.center)
        Button("Close") { cancel(PlaceLoggerError.noSharedURL) }
          .buttonStyle(.borderedProminent)
      }
    }
    .padding(28)
    .task {
      do {
        let url = try await loadURL()
        state = .saving(url)
        let current = try AccountSession.shared.requireSnapshot()
        let api = PlaceLoggerAPI(account: current, authorizer: AccountSession.shared)
        let key = requestKey
        _ = try await api.ingest(sourceURL: url, requestKey: key, waitForResult: false)
        shareLogger.info("Save durably accepted")
        state = .processing

        // This task deliberately outlives the SwiftUI view's task. Reuse the
        // accepted request key so the waiting POST cannot create another save.
        // iOS may still terminate the extension after completeRequest: this is
        // an on-device experiment, not a guarantee of background notifications.
        Task {
          do {
            let result = try await api.ingest(sourceURL: url, requestKey: key)
            shareLogger.info("Save result received after acceptance")
            if result.hasNotificationOutcome {
              await LocalNotification.send(title: result.notificationTitle, body: result.notificationBody,
                ingestID: result.ingestID, itemID: result.itemID,
                entry: result.savedEntries.count == 1 ? result.savedEntries.first : nil,
                identifier: result.notificationIdentifier(accountGeneration: current.generation), account: current)
            }
          } catch {
            // Receipt is already confirmed. A failed result request must not
            // produce a misleading failure/accepted notification.
            shareLogger.info("Result request ended without a notification outcome")
          }
        }
        // The check finishes in 220 ms, then holds before system dismissal.
        // Aim for roughly one second total, including that system animation.
        try await Task.sleep(for: .milliseconds(750))
        complete()
      } catch {
        guard !Task.isCancelled else { return }
        // A transport error is not a processing outcome. Keep it in the sheet;
        // the accepted save may still finish successfully on the server.
        state = .failed(error.localizedDescription)
      }
    }
  }
}

/// Acknowledges receipt, not completion of post processing.
private struct ShareProcessingConfirmation: View {
  var body: some View {
    VStack(spacing: 19) {
      ShareReceiptCheckmark()
      VStack(spacing: 9) {
        Text("Processing…")
          .font(.title2.weight(.semibold))
        Text("You will be notified on completion.")
          .font(.subheadline)
          .foregroundStyle(.secondary)
          .multilineTextAlignment(.center)
      }
    }
  }
}

private struct ShareReceiptCheckmark: View {
  @Environment(\.accessibilityReduceMotion) private var reduceMotion
  @State private var ringProgress: CGFloat = 0
  @State private var checkProgress: CGFloat = 0

  var body: some View {
    ZStack {
      Circle()
        .trim(from: 0, to: reduceMotion ? 1 : ringProgress)
        .stroke(style: StrokeStyle(lineWidth: 3, lineCap: .round))
        .rotationEffect(.degrees(-90))
      CheckmarkStroke()
        .trim(from: 0, to: reduceMotion ? 1 : checkProgress)
        .stroke(style: StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round))
    }
    .foregroundStyle(.green)
    .frame(width: 68, height: 68)
    .accessibilityLabel("Post received")
    .task {
      guard !reduceMotion else { return }
      withAnimation(.easeOut(duration: 0.16)) { ringProgress = 1 }
      do {
        try await Task.sleep(for: .milliseconds(100))
        withAnimation(.easeOut(duration: 0.12)) { checkProgress = 1 }
      } catch {
        // Dismissal cancels the remaining animation.
      }
    }
  }

  private struct CheckmarkStroke: Shape {
    func path(in rect: CGRect) -> Path {
      Path { path in
        path.move(to: CGPoint(x: rect.width * 0.29, y: rect.height * 0.51))
        path.addLine(to: CGPoint(x: rect.width * 0.43, y: rect.height * 0.65))
        path.addLine(to: CGPoint(x: rect.width * 0.71, y: rect.height * 0.36))
      }
    }
  }
}

#Preview("Share received") {
  ShareProcessingConfirmation()
}

@MainActor
private enum LocalNotification {
  static func send(title: String, body: String, ingestID: Int? = nil, itemID: Int? = nil,
                   entry: SavedEntryOutcome? = nil, identifier: String? = nil,
                   account: AccountSessionSnapshot) async {
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
    let notificationID = identifier
      ?? "jot.save.\(account.generation.uuidString).\(UUID().uuidString)"
    let request = UNNotificationRequest(identifier: notificationID, content: content,
                                         trigger: UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false))
    guard (try? await AccountSession.shared.validate(account)) != nil else { return }
    do {
      try await center.add(request)
      shareLogger.info("Completion notification scheduled")
    } catch {
      shareLogger.info("Completion notification could not be scheduled")
    }
    if (try? await AccountSession.shared.validate(account)) == nil {
      center.removePendingNotificationRequests(withIdentifiers: [notificationID])
      center.removeDeliveredNotifications(withIdentifiers: [notificationID])
    }
  }
}
