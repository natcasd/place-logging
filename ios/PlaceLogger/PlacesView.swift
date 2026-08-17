import Combine
import SwiftUI

@MainActor
final class PlacesModel: ObservableObject {
  @Published var places: [SavedPlace] = []
  @Published var isLoading = false
  @Published var errorMessage: String?

  private let api = PlaceLoggerAPI()

  func load() async {
    guard !isLoading else { return }
    isLoading = true
    defer { isLoading = false }
    do {
      places = try await api.fetchPlaces()
      errorMessage = nil
    } catch {
      errorMessage = error.localizedDescription
    }
  }
}

struct PlacesView: View {
  @ObservedObject var router: PlaceLoggerRouter
  @StateObject private var model = PlacesModel()
  @Environment(\.scenePhase) private var scenePhase
  @State private var path: [Int] = []

  var body: some View {
    NavigationStack(path: $path) {
      Group {
        if model.isLoading && model.places.isEmpty {
          ProgressView("Loading saved places…")
        } else if let error = model.errorMessage, model.places.isEmpty {
          ContentUnavailableView {
            Label("Couldn’t Load Places", systemImage: "wifi.exclamationmark")
          } description: {
            Text(error)
          } actions: {
            Button("Try Again") { Task { await model.load() } }
          }
        } else if model.places.isEmpty {
          ContentUnavailableView(
            "No Saved Places",
            systemImage: "fork.knife",
            description: Text("Share an Instagram Reel or YouTube video to Place Logger.")
          )
        } else {
          List(model.places) { place in
            PlaceRow(place: place)
          }
          .listStyle(.plain)
          .refreshable { await model.load() }
        }
      }
      .navigationTitle("Place Logger")
      .toolbar {
        ToolbarItem(placement: .topBarTrailing) {
          if model.isLoading && !model.places.isEmpty {
            ProgressView()
          } else {
            Button("Refresh", systemImage: "arrow.clockwise") {
              Task { await model.load() }
            }
          }
        }
      }
      .navigationDestination(for: Int.self) { itemID in
        SavedItemView(
          places: model.places.filter { $0.itemID == itemID },
          isLoading: model.isLoading
        )
      }
    }
    .task { await model.load() }
    .task(id: router.selectedItemID) {
      guard let itemID = router.selectedItemID else { return }
      await model.load()
      path = [itemID]
      router.selectedItemID = nil
    }
    .onChange(of: scenePhase) { _, phase in
      guard phase == .active else { return }
      Task { await model.load() }
    }
  }
}

private struct SavedItemView: View {
  let places: [SavedPlace]
  let isLoading: Bool

  var body: some View {
    Group {
      if places.isEmpty && isLoading {
        ProgressView("Loading saved place…")
      } else if places.isEmpty {
        ContentUnavailableView(
          "Saved Place Not Found",
          systemImage: "mappin.slash",
          description: Text("Try returning to the list and refreshing.")
        )
      } else {
        List(places) { place in
          PlaceRow(place: place)
        }
        .listStyle(.plain)
      }
    }
    .navigationTitle(places.count == 1 ? places[0].name : "Saved Places")
    .navigationBarTitleDisplayMode(.inline)
  }
}

private struct PlaceRow: View {
  let place: SavedPlace

  var body: some View {
    VStack(alignment: .leading, spacing: 7) {
      Text(place.name)
        .font(.headline)

      if let address = place.formattedAddress, !address.isEmpty {
        Label(address, systemImage: "mappin.and.ellipse")
          .font(.subheadline)
          .foregroundStyle(.secondary)
      }

      if !place.whyItsCool.isEmpty {
        Text(place.whyItsCool)
          .font(.subheadline)
          .lineLimit(3)
      }

      if !place.dishes.isEmpty {
        Text(place.dishes.joined(separator: " · "))
          .font(.caption)
          .foregroundStyle(.orange)
      }

      HStack {
        if let mapsURL = place.googleMapsURL {
          Link("Maps", destination: mapsURL)
        }
        Link("Original post", destination: place.sourceURL)
      }
      .font(.caption.weight(.semibold))
    }
    .padding(.vertical, 6)
  }
}
