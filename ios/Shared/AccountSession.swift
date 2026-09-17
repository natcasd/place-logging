import Combine
import FirebaseAuth
import FirebaseCore
import Foundation
import Security

/// A separate keychain stamp makes logout authoritative even if an extension's
/// in-flight Firebase refresh finishes afterward and writes its cached user.
private struct SharedSessionStamp: Codable {
  let session: AccountSessionSnapshot?

  static func read(group: String?) throws -> SharedSessionStamp? {
    var query = baseQuery(group: group)
    query[kSecReturnData as String] = true
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound { return nil }
    guard status == errSecSuccess, let data = result as? Data else {
      throw PlaceLoggerError.sessionUnavailable
    }
    return try JSONDecoder().decode(Self.self, from: data)
  }

  func write(group: String?) throws {
    let data = try JSONEncoder().encode(self)
    let query = Self.baseQuery(group: group)
    var status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
    if status == errSecItemNotFound {
      var item = query
      item[kSecValueData as String] = data
      item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
      status = SecItemAdd(item as CFDictionary, nil)
    }
    guard status == errSecSuccess else { throw PlaceLoggerError.sessionUnavailable }
  }

  private static func baseQuery(group: String?) -> [String: Any] {
    var query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: "com.natcasd.jot.session",
      kSecAttrAccount as String: "active-account",
    ]
    if let group { query[kSecAttrAccessGroup as String] = group }
    return query
  }
}

@MainActor
final class AccountSession: ObservableObject, AccountAuthorizer {
  static let shared = AccountSession()
  @Published private(set) var snapshot: AccountSessionSnapshot?
  @Published private(set) var configurationError: String?
  private(set) var isConfigured = false
  private var listener: AuthStateDidChangeListenerHandle?

  /// Xcode 26 simulator apps are unsigned by default and therefore cannot use
  /// an explicit Keychain access group. Device builds keep the shared group so
  /// the app and share extension continue to use the same Firebase session.
  private var sessionAccessGroup: String? {
#if targetEnvironment(simulator)
    nil
#else
    APIConfig.keychainGroup
#endif
  }

  func configure() {
    guard !isConfigured else { return }
    do {
      guard let path = Bundle.main.path(forResource: "GoogleService-Info", ofType: "plist", inDirectory: "Firebase"),
            let options = FirebaseOptions(contentsOfFile: path),
            options.projectID == APIConfig.firebaseProjectID,
            !APIConfig.keychainGroup.isEmpty, !APIConfig.keychainGroup.contains("$(")
      else { throw PlaceLoggerError.signInUnavailable }
      if FirebaseApp.app() == nil { FirebaseApp.configure(options: options) }
      if let group = sessionAccessGroup { try Auth.auth().useUserAccessGroup(group) }
      isConfigured = true
      configurationError = nil
      reloadSharedSession()
      listener = Auth.auth().addStateDidChangeListener { [weak self] _, _ in
        Task { @MainActor in self?.reloadSharedSession() }
      }
    } catch {
      snapshot = nil
      configurationError = PlaceLoggerError.signInUnavailable.localizedDescription
    }
  }

  func reloadSharedSession() {
    guard isConfigured else { return }
    do {
      let stamp = try SharedSessionStamp.read(group: sessionAccessGroup)
#if targetEnvironment(simulator)
      let stored = Auth.auth().currentUser
#else
      let stored = try Auth.auth().getStoredUser(forAccessGroup: APIConfig.keychainGroup)
#endif
      if let session = stamp?.session, session.projectID == APIConfig.firebaseProjectID,
         stored?.uid == session.userID {
        snapshot = session
      } else {
        snapshot = nil
      }
    } catch {
      snapshot = nil
    }
  }

  func requireSnapshot() throws -> AccountSessionSnapshot {
    configure()
    reloadSharedSession()
    guard isConfigured else { throw PlaceLoggerError.signInUnavailable }
    guard let snapshot else { throw PlaceLoggerError.signInRequired }
    return snapshot
  }

  func completeSignIn(userID: String) throws {
    guard isConfigured, Auth.auth().currentUser?.uid == userID else {
      throw PlaceLoggerError.sessionChanged
    }
    let session = AccountSessionSnapshot(projectID: APIConfig.firebaseProjectID,
                                         userID: userID, generation: UUID())
    try SharedSessionStamp(session: session).write(group: sessionAccessGroup)
    snapshot = session
  }

  func signOut() throws {
    guard isConfigured else { return }
    try SharedSessionStamp(session: nil).write(group: sessionAccessGroup)
    snapshot = nil
    try Auth.auth().signOut()
  }

  func validate(_ expected: AccountSessionSnapshot) async throws {
#if targetEnvironment(simulator)
    let storedUserID = Auth.auth().currentUser?.uid
#else
    let storedUserID = try Auth.auth().getStoredUser(forAccessGroup: APIConfig.keychainGroup)?.uid
#endif
    guard isConfigured,
          try SharedSessionStamp.read(group: sessionAccessGroup)?.session == expected,
          storedUserID == expected.userID
    else { throw PlaceLoggerError.sessionChanged }
  }

  func token(for expected: AccountSessionSnapshot, forceRefresh: Bool) async throws -> String {
    try await validate(expected)
#if targetEnvironment(simulator)
    let stored = Auth.auth().currentUser
#else
    let stored = try Auth.auth().getStoredUser(forAccessGroup: APIConfig.keychainGroup)
#endif
    guard let user = stored else {
      throw PlaceLoggerError.signInRequired
    }
    let token: String
    do {
      token = try await user.getIDToken(forcingRefresh: forceRefresh)
    } catch {
      let code = AuthErrorCode(rawValue: (error as NSError).code)
      if [.userDisabled, .userNotFound, .invalidUserToken, .userTokenExpired].contains(code) {
        try await invalidate(expected)
        throw PlaceLoggerError.signInRequired
      }
      throw error
    }
    try await validate(expected)
    return token
  }

  func invalidate(_ expected: AccountSessionSnapshot) async throws {
    try await validate(expected)
    try signOut()
  }
}
