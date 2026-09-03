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

  @Published var pendingDestination: PlaceLoggerDestination?

  private init() {}
}

enum PlaceLoggerDestination: Equatable {
  case mapThing(Int)
  case savedThing(Int)
  case activity(Int)
  case legacyItem(Int)
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
    let userInfo = response.notification.request.content.userInfo
    let thingID = Self.intValue(userInfo["thing_id"])
    let ingestID = Self.intValue(userInfo["ingest_id"])
    let itemID = Self.intValue(userInfo["item_id"])
    let hasLocation = Self.boolValue(userInfo["has_location"])
    let destination: PlaceLoggerDestination?
    if let thingID {
      destination = hasLocation ? .mapThing(thingID) : .savedThing(thingID)
    } else if let ingestID {
      destination = .activity(ingestID)
    } else if let itemID {
      destination = .legacyItem(itemID)
    } else {
      destination = nil
    }
    guard let destination else { return }
    await MainActor.run {
      PlaceLoggerRouter.shared.pendingDestination = destination
    }
  }

  private nonisolated static func intValue(_ value: Any?) -> Int? {
    if let value = value as? Int { return value }
    if let value = value as? NSNumber { return value.intValue }
    if let value = value as? String { return Int(value) }
    return nil
  }

  private nonisolated static func boolValue(_ value: Any?) -> Bool {
    if let value = value as? Bool { return value }
    if let value = value as? NSNumber { return value.boolValue }
    if let value = value as? String {
      return ["true", "1", "yes"].contains(value.lowercased())
    }
    return false
  }
}
