import Combine
import MapKit
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
  @State private var selectedTab: PlacesTab = .list

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
          TabView(selection: $selectedTab) {
            PlacesList(places: model.places) {
              await model.load()
            }
            .tabItem {
              Label("List", systemImage: "list.bullet")
            }
            .tag(PlacesTab.list)

            PlacesMap(places: model.places)
              .tabItem {
                Label("Map", systemImage: "map")
              }
              .tag(PlacesTab.map)
          }
        }
      }
      .navigationTitle(selectedTab == .map ? "Saved Map" : "Place Logger")
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
      selectedTab = .list
      path = [itemID]
      router.selectedItemID = nil
    }
    .onChange(of: scenePhase) { _, phase in
      guard phase == .active else { return }
      Task { await model.load() }
    }
  }
}

private enum PlacesTab: Hashable {
  case list
  case map
}

private struct PlacesList: View {
  let places: [SavedPlace]
  let refresh: () async -> Void

  var body: some View {
    List(places) { place in
      PlaceRow(place: place)
    }
    .listStyle(.plain)
    .refreshable { await refresh() }
  }
}

private struct MappedPlaceGroup: Identifiable {
  let id: String
  var places: [SavedPlace]

  var primary: SavedPlace { places[0] }
  var name: String { primary.name }
  var coordinate: CLLocationCoordinate2D {
    CLLocationCoordinate2D(
      latitude: primary.latitude ?? 0,
      longitude: primary.longitude ?? 0
    )
  }

  var sourceCount: Int {
    Set(places.map(\.sourceURL)).count
  }

  var dishes: [String] {
    uniqueStrings(places.flatMap(\.dishes))
  }

  var tags: [String] {
    uniqueStrings(places.flatMap(\.tags))
  }

  private func uniqueStrings(_ strings: [String]) -> [String] {
    var seen: Set<String> = []
    return strings.filter { value in
      let normalized = value.lowercased()
      guard !normalized.isEmpty, !seen.contains(normalized) else { return false }
      seen.insert(normalized)
      return true
    }
  }

  static func make(from places: [SavedPlace]) -> [MappedPlaceGroup] {
    var groups: [MappedPlaceGroup] = []
    var indexes: [String: Int] = [:]

    for place in places {
      guard place.latitude != nil, place.longitude != nil else { continue }
      let key = place.googlePlaceID.map { "google:\($0)" } ?? "saved:\(place.id)"
      if let index = indexes[key] {
        groups[index].places.append(place)
      } else {
        indexes[key] = groups.count
        groups.append(MappedPlaceGroup(id: key, places: [place]))
      }
    }
    return groups
  }
}

private struct PlacesMap: View {
  let places: [SavedPlace]
  @StateObject private var locationModel = LocationModel()
  @StateObject private var searchModel = MapSearchModel()
  @State private var cameraPosition: MapCameraPosition = .automatic
  @State private var selectedGroupID: String?
  @State private var detailGroup: MappedPlaceGroup?
  @State private var searchText = ""
  @State private var searchResult: MKMapItem?
  @State private var visibleRegion: MKCoordinateRegion?
  @State private var hasChosenInitialCamera = false

  private var groups: [MappedPlaceGroup] {
    MappedPlaceGroup.make(from: places)
  }

  private var selectedGroup: MappedPlaceGroup? {
    groups.first { $0.id == selectedGroupID }
  }

  var body: some View {
    if groups.isEmpty {
      ContentUnavailableView(
        "No Mapped Places",
        systemImage: "mappin.slash",
        description: Text("Places will appear here after their locations are resolved.")
      )
    } else {
      Map(position: $cameraPosition, selection: $selectedGroupID) {
        UserAnnotation()

        ForEach(groups) { group in
          Marker(group.name, coordinate: group.coordinate)
            .tint(.red)
            .tag(group.id)
        }

        if let searchResult {
          Marker(
            searchResult.name ?? "Search Result",
            coordinate: searchResult.placemark.coordinate
          )
          .tint(.blue)
        }
      }
      .mapControls {
        MapUserLocationButton()
        MapCompass()
        MapScaleView()
      }
      .onMapCameraChange(frequency: .onEnd) { context in
        visibleRegion = context.region
        if cameraPosition.positionedByUser {
          hasChosenInitialCamera = true
        }
      }
      .searchable(
        text: $searchText,
        placement: .navigationBarDrawer(displayMode: .always),
        prompt: "City, neighborhood, address, or place"
      )
      .searchSuggestions {
        ForEach(searchModel.suggestions) { suggestion in
          Button {
            searchText = suggestion.title
            searchModel.clearSuggestions()
            Task { await selectSearchSuggestion(suggestion) }
          } label: {
            VStack(alignment: .leading, spacing: 2) {
              Text(suggestion.title)
              if !suggestion.subtitle.isEmpty {
                Text(suggestion.subtitle)
                  .font(.caption)
                  .foregroundStyle(.secondary)
              }
            }
          }
        }
      }
      .onSubmit(of: .search) {
        Task { await submitSearch() }
      }
      .onChange(of: searchText) { _, query in
        searchModel.updateQuery(query, region: visibleRegion)
        if query.isEmpty {
          searchResult = nil
        }
      }
      .onReceive(locationModel.$location.compactMap { $0 }) { location in
        guard !hasChosenInitialCamera else { return }
        hasChosenInitialCamera = true
        cameraPosition = .region(
          MKCoordinateRegion(
            center: location.coordinate,
            latitudinalMeters: 12_000,
            longitudinalMeters: 12_000
          )
        )
      }
      .task {
        locationModel.requestCurrentLocation()
      }
      .alert(
        "Search Failed",
        isPresented: Binding(
          get: { searchModel.errorMessage != nil },
          set: { if !$0 { searchModel.errorMessage = nil } }
        )
      ) {
        Button("OK", role: .cancel) { searchModel.errorMessage = nil }
      } message: {
        Text(searchModel.errorMessage ?? "MapKit could not complete that search.")
      }
      .safeAreaInset(edge: .bottom) {
        if let selectedGroup {
          MapPlaceCard(group: selectedGroup) {
            detailGroup = selectedGroup
          } onDismiss: {
            selectedGroupID = nil
          }
          .padding(.horizontal)
          .padding(.bottom, 8)
        }
      }
      .sheet(item: $detailGroup) { group in
        NavigationStack {
          MapPlaceDetail(group: group)
        }
        .presentationDetents([.medium, .large])
      }
    }
  }

  private func selectSearchSuggestion(_ suggestion: MapSearchSuggestion) async {
    guard let item = await searchModel.resolve(suggestion) else { return }
    showSearchResult(item)
  }

  private func submitSearch() async {
    searchModel.clearSuggestions()
    guard let item = await searchModel.search(searchText, region: visibleRegion) else { return }
    searchText = item.name ?? searchText
    showSearchResult(item)
  }

  private func showSearchResult(_ item: MKMapItem) {
    hasChosenInitialCamera = true
    selectedGroupID = nil
    searchResult = item
    cameraPosition = .item(item, allowsAutomaticPitch: false)
  }
}

private struct MapPlaceCard: View {
  let group: MappedPlaceGroup
  let showDetails: () -> Void
  let onDismiss: () -> Void

  var body: some View {
    VStack(alignment: .leading, spacing: 8) {
      HStack(alignment: .firstTextBaseline) {
        Text(group.name)
          .font(.headline)
        Spacer()
        Button("Close", systemImage: "xmark.circle.fill", action: onDismiss)
          .labelStyle(.iconOnly)
          .foregroundStyle(.secondary)
      }

      if let address = group.primary.formattedAddress, !address.isEmpty {
        Text(address)
          .font(.subheadline)
          .foregroundStyle(.secondary)
          .lineLimit(2)
      }

      if group.sourceCount > 1 {
        Label("Saved from \(group.sourceCount) posts", systemImage: "square.stack")
          .font(.caption.weight(.semibold))
          .foregroundStyle(.secondary)
      }

      if !group.primary.whyItsCool.isEmpty {
        Text(group.primary.whyItsCool)
          .font(.subheadline)
          .lineLimit(2)
      }

      if !group.dishes.isEmpty {
        Text(group.dishes.joined(separator: " · "))
          .font(.caption)
          .foregroundStyle(.orange)
          .lineLimit(2)
      }

      if let mediaReference = group.primary.mediaReferenceText {
        Label(mediaReference, systemImage: group.primary.mediaReferenceSystemImage)
          .font(.caption.weight(.semibold))
          .foregroundStyle(.secondary)
      }

      HStack {
        Button("More Info", systemImage: "info.circle", action: showDetails)
          .buttonStyle(.borderedProminent)
        if let mapsURL = group.primary.googleMapsURL {
          Link("Open in Maps", destination: mapsURL)
            .buttonStyle(.bordered)
        }
      }
      .font(.subheadline.weight(.semibold))
    }
    .padding()
    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18))
    .shadow(radius: 8, y: 3)
  }
}

private struct MapPlaceDetail: View {
  let group: MappedPlaceGroup
  @Environment(\.dismiss) private var dismiss

  var body: some View {
    List {
      Section {
        if let address = group.primary.formattedAddress, !address.isEmpty {
          Label(address, systemImage: "mappin.and.ellipse")
        }
        if let mapsURL = group.primary.googleMapsURL {
          Link("Open in Maps", destination: mapsURL)
        }
      }

      if !group.dishes.isEmpty {
        Section("Things to Try") {
          Text(group.dishes.joined(separator: " · "))
            .foregroundStyle(.orange)
        }
      }

      if !group.tags.isEmpty {
        Section("Tags") {
          Text(group.tags.joined(separator: " · "))
            .foregroundStyle(.secondary)
        }
      }

      Section(group.sourceCount == 1 ? "Saved Post" : "Saved from \(group.sourceCount) Posts") {
        ForEach(group.places) { place in
          VStack(alignment: .leading, spacing: 8) {
            if !place.whyItsCool.isEmpty {
              Text(place.whyItsCool)
            }
            if !place.dishes.isEmpty {
              Text(place.dishes.joined(separator: " · "))
                .font(.caption)
                .foregroundStyle(.orange)
            }
            if let mediaReference = place.mediaReferenceText {
              Label(mediaReference, systemImage: place.mediaReferenceSystemImage)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            }
            Link(place.sourceLinkText, destination: place.linkedSourceURL)
              .font(.subheadline.weight(.semibold))
          }
          .padding(.vertical, 4)
        }
      }
    }
    .navigationTitle(group.name)
    .navigationBarTitleDisplayMode(.inline)
    .toolbar {
      ToolbarItem(placement: .confirmationAction) {
        Button("Done") { dismiss() }
      }
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

      if let mediaReference = place.mediaReferenceText {
        Label(mediaReference, systemImage: place.mediaReferenceSystemImage)
          .font(.caption.weight(.semibold))
          .foregroundStyle(.secondary)
      }

      HStack {
        if let mapsURL = place.googleMapsURL {
          Link("Maps", destination: mapsURL)
        }
        Link(place.sourceLinkText, destination: place.linkedSourceURL)
      }
      .font(.caption.weight(.semibold))
    }
    .padding(.vertical, 6)
  }
}
