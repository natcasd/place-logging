import Foundation

struct AccountSessionSnapshot: Codable, Equatable, Sendable {
  let projectID: String
  let userID: String
  let generation: UUID
}

protocol AccountAuthorizer: Sendable {
  func token(for snapshot: AccountSessionSnapshot, forceRefresh: Bool) async throws -> String
  func validate(_ snapshot: AccountSessionSnapshot) async throws
  func invalidate(_ snapshot: AccountSessionSnapshot) async throws
}

