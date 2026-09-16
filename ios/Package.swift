// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "JotClientContracts",
  platforms: [.macOS(.v13)],
  products: [.library(name: "JotClientCore", targets: ["JotClientCore"])],
  targets: [
    .target(name: "JotClientCore", path: "Shared", exclude: ["AccountSession.swift", "SaveCompletionNotifications.swift"],
            sources: ["AccountSessionContract.swift", "APIConfig.swift", "Models.swift", "PlaceLoggerAPI.swift", "SaveNotificationContext.swift"]),
    .testTarget(name: "JotClientCoreTests", dependencies: ["JotClientCore"], path: "ClientTests"),
  ]
)
