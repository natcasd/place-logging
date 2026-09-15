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

  func prepareApple(_ request: ASAuthorizationAppleIDRequest, linking: Bool) {
    errorMessage = nil
    statusMessage = nil
    appleNonce = nil
    do {
      appleAccount = linking ? try AccountSession.shared.requireSnapshot() : nil
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

  func completeApple(_ result: Result<ASAuthorization, Error>, linking: Bool) async {
    guard !isBusy else { return }
    isBusy = true
    defer { isBusy = false; appleNonce = nil; appleAccount = nil }
    do {
      let authorization = try result.get()
      guard let apple = authorization.credential as? ASAuthorizationAppleIDCredential,
            let data = apple.identityToken, let token = String(data: data, encoding: .utf8),
            let nonce = appleNonce else { throw PlaceLoggerError.signInUnavailable }
      let credential = OAuthProvider.appleCredential(withIDToken: token, rawNonce: nonce, fullName: apple.fullName)
      try await accept(credential, linking: linking, expected: appleAccount)
    } catch {
      if (error as NSError).code != ASAuthorizationError.canceled.rawValue { show(error) }
    }
  }

  func google(linking: Bool) async {
    guard !isBusy else { return }
    isBusy = true
    errorMessage = nil
    statusMessage = nil
    defer { isBusy = false }
    do {
      let expected = linking ? try AccountSession.shared.requireSnapshot() : nil
      guard let clientID = FirebaseApp.app()?.options.clientID,
            let scene = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }).first,
            var controller = scene.windows.first(where: \.isKeyWindow)?.rootViewController
      else { throw PlaceLoggerError.signInUnavailable }
      while let presented = controller.presentedViewController { controller = presented }
      GIDSignIn.sharedInstance.configuration = GIDConfiguration(clientID: clientID)
      let result = try await GIDSignIn.sharedInstance.signIn(withPresenting: controller)
      guard let idToken = result.user.idToken?.tokenString else { throw PlaceLoggerError.signInUnavailable }
      let credential = GoogleAuthProvider.credential(withIDToken: idToken, accessToken: result.user.accessToken.tokenString)
      try await accept(credential, linking: linking, expected: expected)
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

  private func show(_ error: Error) {
    let code = AuthErrorCode(rawValue: (error as NSError).code)
    if code == .credentialAlreadyInUse || code == .accountExistsWithDifferentCredential {
      errorMessage = "This sign-in belongs to another account. Use your original sign-in method to open that library."
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
    VStack(spacing: 24) {
      Text("Jot").font(.system(size: 48, weight: .bold, design: .rounded))
      Text("Your discoveries, saved for you.").font(.title3).foregroundStyle(.secondary)
      if let error = session.configurationError {
        Text(error).multilineTextAlignment(.center)
      } else {
        SignInButtons(linking: false)
        Text("New here? Continuing creates your account.")
          .font(.footnote).foregroundStyle(.secondary)
      }
    }
    .padding(32)
    .frame(maxWidth: 420)
  }
}

private struct SignInButtons: View {
  let linking: Bool
  @StateObject private var model = SignInModel()

  var body: some View {
    VStack(spacing: 16) {
      SignInWithAppleButton(.continue, onRequest: { model.prepareApple($0, linking: linking) }) { result in
        Task { await model.completeApple(result, linking: linking) }
      }
      .signInWithAppleButtonStyle(.black)
      .frame(height: 50)
      GoogleSignInButton {
        Task { await model.google(linking: linking) }
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
  }
}

struct AccountSettingsView: View {
  @State private var errorMessage: String?

  var body: some View {
    NavigationStack {
      Form {
        Section("Sign-in methods") {
          Text("Connect Apple and Google to open this same library with either one.")
            .font(.footnote).foregroundStyle(.secondary)
          SignInButtons(linking: true)
        }
        Section {
          Button("Sign Out") {
            do {
              try AccountSession.shared.signOut()
              GIDSignIn.sharedInstance.signOut()
            } catch { errorMessage = error.localizedDescription }
          }
          if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
        }
      }
      .navigationTitle("Account")
    }
  }
}
