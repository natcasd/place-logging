import Foundation

/// Only account identity and the operation ID travel with the system-owned task.
/// Credentials stay in the authenticated request, never in its URL or metadata.
struct SaveNotificationContext: Codable, Sendable {
  let account: AccountSessionSnapshot
  let ingestID: Int
  let startedAt: Date
  var failures = 0

  func accepts(_ result: IngestResponse) -> Bool {
    result.ingestID == ingestID && ["completed", "partial", "failed"].contains(result.status ?? "")
  }
}
