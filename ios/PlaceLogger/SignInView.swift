import AuthenticationServices
import Combine
import CryptoKit
import FirebaseAuth
import FirebaseCore
import GoogleSignIn
import GoogleSignInSwift
import Security
import SwiftUI

@MainActor
final class SignInModel: ObservableObject {
  @Published var isBusy = false
  @Published var errorMessage: String?
  @Published var statusMessage: String?
  private var appleNonce: String?
  private var appleAccount: AccountSessionSnapshot?

  func prepareApple(_ request: ASAuthorizationAppleIDRequest, linking: Bool, deleting: AccountSessionSnapshot? = nil) {
    errorMessage = nil
    statusMessage = nil
    appleNonce = nil
    do {
      appleAccount = try deleting ?? (linking ? AccountSession.shared.requireSnapshot() : nil)
    } catch { show(error); return }
    var bytes = [UInt8](repeating: 0, count: 32)
    guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
      errorMessage = "Couldn’t start sign-in. Please try again."
      return
    }
    let nonce = Data(bytes).base64EncodedString()
    appleNonce = nonce
    request.requestedScopes = [.fullName, .email]
    request.nonce = SHA256.hash(data: Data(nonce.utf8)).map { String(format: "%02x", $0) }.joined()
  }

  func completeApple(_ result: Result<ASAuthorization, Error>, linking: Bool, deleting: AccountSessionSnapshot? = nil) async {
    guard !isBusy else { return }
    isBusy = true
    defer { isBusy = false; appleNonce = nil; appleAccount = nil }
    do {
      let authorization = try result.get()
      guard let apple = authorization.credential as? ASAuthorizationAppleIDCredential,
            let data = apple.identityToken, let token = String(data: data, encoding: .utf8),
            let nonce = appleNonce else { throw PlaceLoggerError.signInUnavailable }
      let credential = OAuthProvider.appleCredential(withIDToken: token, rawNonce: nonce, fullName: apple.fullName)
      if let deleting {
        guard appleAccount == deleting, let code = apple.authorizationCode,
              let authorizationCode = String(data: code, encoding: .utf8) else {
          throw PlaceLoggerError.signInUnavailable
        }
        try await deleteAccount(credential, expected: deleting, appleCode: authorizationCode)
      } else {
        try await accept(credential, linking: linking, expected: appleAccount)
      }
    } catch {
      if (error as NSError).code != ASAuthorizationError.canceled.rawValue { show(error) }
    }
  }

  func google(linking: Bool, deleting: AccountSessionSnapshot? = nil) async {
    guard !isBusy else { return }
    isBusy = true
    errorMessage = nil
    statusMessage = nil
    defer { isBusy = false }
    do {
      let expected = try deleting ?? (linking ? AccountSession.shared.requireSnapshot() : nil)
      guard let clientID = FirebaseApp.app()?.options.clientID,
            let scene = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }).first,
            var controller = scene.windows.first(where: \.isKeyWindow)?.rootViewController
      else { throw PlaceLoggerError.signInUnavailable }
      while let presented = controller.presentedViewController { controller = presented }
      GIDSignIn.sharedInstance.configuration = GIDConfiguration(clientID: clientID)
      let result = try await GIDSignIn.sharedInstance.signIn(withPresenting: controller)
      guard let idToken = result.user.idToken?.tokenString else { throw PlaceLoggerError.signInUnavailable }
      let credential = GoogleAuthProvider.credential(withIDToken: idToken, accessToken: result.user.accessToken.tokenString)
      if let deleting {
        try await deleteAccount(credential, expected: deleting)
      } else {
        try await accept(credential, linking: linking, expected: expected)
      }
    } catch {
      if (error as NSError).code != GIDSignInError.canceled.rawValue { show(error) }
    }
  }

  private func accept(_ credential: AuthCredential, linking: Bool,
                      expected: AccountSessionSnapshot?) async throws {
    if linking {
      guard let expected else { throw PlaceLoggerError.sessionChanged }
      try await AccountSession.shared.validate(expected)
      guard let user = Auth.auth().currentUser, user.uid == expected.userID else {
        throw PlaceLoggerError.sessionChanged
      }
      _ = try await user.link(with: credential)
      try await AccountSession.shared.validate(expected)
      statusMessage = "Connected. You can now use this sign-in method for your library."
    } else {
      let result = try await Auth.auth().signIn(with: credential)
      try AccountSession.shared.completeSignIn(userID: result.user.uid)
    }
  }

  private func deleteAccount(_ credential: AuthCredential, expected: AccountSessionSnapshot,
                             appleCode: String? = nil) async throws {
    let session = AccountSession.shared
    try await session.validate(expected)
    guard let user = Auth.auth().currentUser, user.uid == expected.userID else {
      throw PlaceLoggerError.sessionChanged
    }
    // An Apple-linked account must revoke its Apple grant as part of deletion.
    if user.providerData.contains(where: { $0.providerID == "apple.com" }), appleCode == nil {
      throw PlaceLoggerError.signInUnavailable
    }
    // Keep the SDK User on the main actor; only Void crosses the callback.
    try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
      user.reauthenticate(with: credential) { _, error in
        if let error { continuation.resume(throwing: error) }
        else { continuation.resume() }
      }
    }
    try await session.validate(expected)
    if let appleCode {
      try await Auth.auth().revokeToken(withAuthorizationCode: appleCode)
      try await session.validate(expected)
    }
    try await PlaceLoggerAPI(account: expected, authorizer: session).deleteAccount()
    try await session.invalidate(expected)
    GIDSignIn.sharedInstance.signOut()
  }

  private func show(_ error: Error) {
    let code = AuthErrorCode(rawValue: (error as NSError).code)
    if code == .credentialAlreadyInUse || code == .accountExistsWithDifferentCredential {
      errorMessage = "This sign-in belongs to another account. Use your original sign-in method to open that library."
    } else if code == .userMismatch {
      errorMessage = "Use the Apple or Google account connected to this Jot account."
    } else if code == .providerAlreadyLinked {
      errorMessage = "This sign-in method is already connected."
    } else {
      errorMessage = "Couldn’t sign in. Please try again."
    }
  }
}

struct SignInView: View {
  @ObservedObject var session: AccountSession

  var body: some View {
    GeometryReader { geometry in
      ScrollView {
        VStack(spacing: 0) {
          Spacer(minLength: 40)

          VStack(spacing: 24) {
            HStack(spacing: 15) {
              Image("JotWordmarkLogo")
                .resizable()
                .scaledToFit()
                .frame(width: 88, height: 88)
                .frame(width: 54, height: 68)
                .clipped()
                .accessibilityHidden(true)
              Text("jot")
                .font(.system(size: 68, weight: .bold, design: .rounded))
                .tracking(-3)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Jot")

            Text("Your discoveries, saved for you.")
              .font(.body)
              .foregroundStyle(.secondary)
              .multilineTextAlignment(.center)
          }

          Spacer(minLength: 64)

          VStack(spacing: 16) {
            if let error = session.configurationError {
              Text(error).multilineTextAlignment(.center)
            } else {
              Text("Sign in or create an account")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
              SignInButtons(linking: false)
            }
          }
          .padding(.bottom, 48)
        }
        .frame(maxWidth: 420)
        .frame(minHeight: geometry.size.height)
        .padding(.horizontal, 28)
        .frame(maxWidth: .infinity)
      }
    }
    .background(Color(uiColor: .systemBackground))
  }
}

private struct ContinueWithGoogleButton: View {
  let action: () -> Void
  @Environment(\.colorScheme) private var colorScheme

  var body: some View {
    Button(action: action) {
      HStack(spacing: 12) {
        Image("GoogleSignInMark")
          .resizable()
          .scaledToFit()
          .frame(width: 20, height: 20)
          .accessibilityHidden(true)
        Text("Continue with Google")
          .font(.custom("GoogleSans-Medium", size: 16, relativeTo: .body))
          .fixedSize(horizontal: false, vertical: true)
      }
      .foregroundStyle(colorScheme == .dark
        ? Color(red: 227 / 255, green: 227 / 255, blue: 227 / 255)
        : Color(red: 31 / 255, green: 31 / 255, blue: 31 / 255))
      .padding(.horizontal, 16)
      .padding(.vertical, 15)
      .frame(maxWidth: .infinity, minHeight: 54)
      .background(colorScheme == .dark
        ? Color(red: 19 / 255, green: 19 / 255, blue: 20 / 255)
        : Color(red: 242 / 255, green: 242 / 255, blue: 242 / 255), in: Capsule())
      .overlay {
        if colorScheme == .dark {
          Capsule().strokeBorder(Color(red: 142 / 255, green: 145 / 255, blue: 143 / 255), lineWidth: 1)
        }
      }
      .contentShape(Capsule())
    }
    .buttonStyle(.plain)
  }
}

private struct SignInButtons: View {
  let linking: Bool
  var deleting: AccountSessionSnapshot? = nil
  var onBusyChange: (Bool) -> Void = { _ in }
  @StateObject private var model = SignInModel()
  @Environment(\.colorScheme) private var colorScheme

  private var hasApple: Bool {
    Auth.auth().currentUser?.providerData.contains(where: { $0.providerID == "apple.com" }) == true
  }

  var body: some View {
    VStack(spacing: 16) {
#if GOOGLE_ONLY_DEVICE_DEBUG
      if deleting != nil && hasApple {
        Text("Apple verification is unavailable in this development build.")
          .font(.footnote)
          .foregroundStyle(.secondary)
          .multilineTextAlignment(.center)
      }
#else
      if deleting == nil || hasApple {
        SignInWithAppleButton(.continue, onRequest: { model.prepareApple($0, linking: linking, deleting: deleting) }) { result in
          Task { await model.completeApple(result, linking: linking, deleting: deleting) }
        }
        .signInWithAppleButtonStyle(deleting == nil && colorScheme == .dark ? .white : .black)
        .frame(height: deleting == nil ? 54 : 50)
        .clipShape(RoundedRectangle(cornerRadius: deleting == nil ? 27 : 8))
      }
#endif
      if deleting == nil || !hasApple {
        if deleting == nil {
          ContinueWithGoogleButton {
            Task { await model.google(linking: linking) }
          }
        } else {
          GoogleSignInButton {
            Task { await model.google(linking: linking, deleting: deleting) }
          }
        }
      }
      if model.isBusy { ProgressView() }
      if let error = model.errorMessage {
        Text(error).font(.footnote).foregroundStyle(.red).multilineTextAlignment(.center)
      }
      if let status = model.statusMessage {
        Text(status).font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
      }
    }
    .disabled(model.isBusy)
    .onChange(of: model.isBusy) { _, busy in onBusyChange(busy) }
  }
}

struct AccountSettingsView: View {
  @State private var errorMessage: String?
  @State private var showDeletion = false
  @State private var deletionAccount: AccountSessionSnapshot?
  @State private var showShareSheetSetup = false

  private var email: String? {
    guard let email = Auth.auth().currentUser?.email?.trimmingCharacters(in: .whitespacesAndNewlines),
          !email.isEmpty else { return nil }
    return email
  }

  private var signInDescription: String {
    let providers = Auth.auth().currentUser?.providerData.map(\.providerID) ?? []
    if providers.contains("apple.com") && providers.contains("google.com") {
      return "Connected with Apple and Google"
    }
    if providers.contains("apple.com") { return "Signed in with Apple" }
    if providers.contains("google.com") { return "Signed in with Google" }
    return "Signed in"
  }

  var body: some View {
    NavigationStack {
      ScrollView {
        VStack(spacing: 28) {
          HStack(spacing: 12) {
            ZStack {
              Circle().fill(Color.primary.opacity(0.07))
              if let initial = email?.first {
                Text(String(initial).uppercased()).font(.title2.weight(.medium))
              } else {
                Image(systemName: "person.fill").font(.title2)
              }
            }
            .frame(width: 48, height: 48)
            .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 5) {
              Text(email ?? "Your account").font(.body)
              Text(signInDescription).font(.footnote).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)

            Menu {
              Button {
                do {
                  try AccountSession.shared.signOut()
                  GIDSignIn.sharedInstance.signOut()
                } catch { errorMessage = error.localizedDescription }
              } label: {
                Label("Sign out", systemImage: "rectangle.portrait.and.arrow.right")
              }
              Button(role: .destructive) {
                do {
                  deletionAccount = try AccountSession.shared.requireSnapshot()
                  showDeletion = true
                } catch { errorMessage = error.localizedDescription }
              } label: {
                Label("Delete account", systemImage: "trash")
              }
            } label: {
              Image(systemName: "ellipsis")
                .font(.title3)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
            }
            .tint(.primary)
            .accessibilityLabel("Account options")
          }
          .padding(.horizontal, 20)

          VStack(alignment: .leading, spacing: 8) {
            Text("HELP")
              .font(.footnote.weight(.semibold))
              .foregroundStyle(.secondary)
              .padding(.horizontal, 16)

            Button { showShareSheetSetup = true } label: {
              HStack(spacing: 14) {
                Image(systemName: "square.and.arrow.up")
                  .font(.title3.weight(.semibold))
                  .foregroundStyle(.orange)
                  .frame(width: 32)

                VStack(alignment: .leading, spacing: 3) {
                  Text("Set up the Share Sheet")
                    .foregroundStyle(.primary)
                  Text("Add Jot to Favorites")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                Image(systemName: "chevron.right")
                  .font(.footnote.weight(.semibold))
                  .foregroundStyle(.tertiary)
              }
              .padding(16)
              .background(Color(uiColor: .secondarySystemGroupedBackground))
              .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            }
            .buttonStyle(.plain)
          }
          .padding(.horizontal, 20)
        }
        .padding(.top, 24)
      }
      .background(Color(uiColor: .systemGroupedBackground))
      .navigationTitle("Account")
      .alert("Couldn’t update account", isPresented: Binding(
        get: { errorMessage != nil }, set: { if !$0 { errorMessage = nil } }
      )) {
        Button("OK", role: .cancel) { errorMessage = nil }
      } message: {
        Text(errorMessage ?? "Please try again.")
      }
      .sheet(isPresented: $showDeletion) {
        if let deletionAccount { DeleteAccountView(account: deletionAccount) }
      }
      .sheet(isPresented: $showShareSheetSetup) {
        ShareSheetSetupView()
      }
    }
  }
}

private struct DeleteAccountView: View {
  private enum Step { case warning, confirmation, verification }

  let account: AccountSessionSnapshot
  @Environment(\.dismiss) private var dismiss
  @State private var step: Step = .warning
  @State private var confirmationText = ""
  @State private var isBusy = false
  @FocusState private var confirmationFocused: Bool

  var body: some View {
    NavigationStack {
      ScrollView {
        VStack(spacing: 24) {
          switch step {
          case .warning:
            Text("Delete your account?").font(.title2.bold())
            Text("This permanently removes your Jot account and saved library.")
            Button("Continue", role: .destructive) { step = .confirmation }
              .buttonStyle(.bordered).tint(.red)
          case .confirmation:
            Text("This can’t be undone.").font(.title2.bold())
            Text("Type DELETE to confirm you want to permanently delete your account and saved library.")
            TextField("DELETE", text: $confirmationText)
              .textFieldStyle(.roundedBorder)
              .textInputAutocapitalization(.characters)
              .autocorrectionDisabled()
              .focused($confirmationFocused)
              .accessibilityLabel("Type DELETE to confirm account deletion")
            Button("Delete account", role: .destructive) {
              guard confirmationText == "DELETE" else { return }
              confirmationFocused = false
              step = .verification
            }
            .buttonStyle(.borderedProminent).tint(.red)
            .disabled(confirmationText != "DELETE")
          case .verification:
            Text("Verify it’s you").font(.title2.bold())
            Text("Confirm with your sign-in account to finish permanently deleting your account and saved library.")
              .foregroundStyle(.secondary)
            SignInButtons(linking: false, deleting: account, onBusyChange: { isBusy = $0 })
          }
        }
        .multilineTextAlignment(.center)
        .padding(32)
      }
      .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() }.disabled(isBusy) } }
      .onChange(of: step) { _, step in confirmationFocused = step == .confirmation }
    }
    .interactiveDismissDisabled(isBusy)
  }
}
