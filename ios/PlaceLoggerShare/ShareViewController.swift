import SwiftUI
import UniformTypeIdentifiers

final class ShareViewController: UIViewController {
  override func viewDidLoad() {
    super.viewDidLoad()
    let root = ShareStatusView(
      loadURL: { [weak self] in
        guard let self else { throw PlaceLoggerError.noSharedURL }
        return try await self.sharedURL()
      },
      complete: { [weak self] in
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

  enum Phase {
    case starting
    case saving(URL)
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
        Text("Saving to Place Logger…")
          .font(.headline)
        Text(url.host() ?? url.absoluteString)
          .font(.caption)
          .foregroundStyle(.secondary)
        Text("This can take about a minute. You’ll get the result in Telegram.")
          .font(.caption)
          .multilineTextAlignment(.center)
          .foregroundStyle(.secondary)
      case .failed(let message):
        Image(systemName: "exclamationmark.triangle.fill")
          .font(.largeTitle)
          .foregroundStyle(.orange)
        Text("Couldn’t Save")
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
        _ = try await PlaceLoggerAPI().ingest(sourceURL: url)
        complete()
      } catch {
        state = .failed(error.localizedDescription)
      }
    }
  }
}
