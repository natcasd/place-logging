import SwiftUI
import UIKit
import UserNotifications

@main
struct PlaceLoggerApp: App {
  @UIApplicationDelegateAdaptor(PlaceLoggerAppDelegate.self) private var appDelegate
  @StateObject private var router = PlaceLoggerRouter.shared

  var body: some Scene {
    WindowGroup {
      PlacesView(router: router)
        .task {
          _ = try? await UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound]
          )
        }
    }
  }
}

@MainActor
final class PlaceLoggerRouter: ObservableObject {
  static let shared = PlaceLoggerRouter()

  @Published var selectedItemID: Int?

  private init() {}
}

final class PlaceLoggerAppDelegate: NSObject, UIApplicationDelegate,
  UNUserNotificationCenterDelegate
{
  func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
  ) -> Bool {
    UNUserNotificationCenter.current().delegate = self
    return true
  }

  nonisolated func userNotificationCenter(
    _ center: UNUserNotificationCenter,
    willPresent notification: UNNotification
  ) async -> UNNotificationPresentationOptions {
    [.banner, .sound]
  }

  nonisolated func userNotificationCenter(
    _ center: UNUserNotificationCenter,
    didReceive response: UNNotificationResponse
  ) async {
    guard
      let itemID = response.notification.request.content.userInfo["item_id"] as? Int
    else {
      return
    }
    await MainActor.run {
      PlaceLoggerRouter.shared.selectedItemID = itemID
    }
  }
}
