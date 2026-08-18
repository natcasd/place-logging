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

            PlacesMap(
              places: model.places,
              isRefreshing: model.isLoading
            ) {
              await model.load()
            }
              .tabItem {
                Label("Map", systemImage: "map")
              }
              .tag(PlacesTab.map)
          }
        }
      }
      .navigationTitle(selectedTab == .map ? "" : "Place Logger")
      .navigationBarTitleDisplayMode(selectedTab == .map ? .inline : .automatic)
      .toolbar(selectedTab == .map ? .hidden : .visible, for: .navigationBar)
      .toolbar {
        ToolbarItem(placement: .topBarTrailing) {
          if selectedTab == .list {
            if model.isLoading && !model.places.isEmpty {
              ProgressView()
            } else {
              Button("Refresh", systemImage: "arrow.clockwise") {
                Task { await model.load() }
              }
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
  let isRefreshing: Bool
  let refresh: () async -> Void
  @StateObject private var locationModel = LocationModel()
  @StateObject private var searchModel = MapSearchModel()
  @State private var cameraPosition: MapCameraPosition = .automatic
  @State private var selectedGroupID: String?
  @State private var detailGroup: MappedPlaceGroup?
  @State private var searchText = ""
  @State private var searchResult: MKMapItem?
  @State private var visibleRegion: MKCoordinateRegion?
  @State private var hasChosenInitialCamera = false
  @State private var isSearchExpanded = false
  @FocusState private var searchIsFocused: Bool

  private var groups: [MappedPlaceGroup] {
    MappedPlaceGroup.make(from: places)
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
      .onChange(of: selectedGroupID) { _, groupID in
        guard let groupID, let group = groups.first(where: { $0.id == groupID }) else {
          return
        }
        detailGroup = group
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
            latitudinalMeters: 4_000,
            longitudinalMeters: 4_000
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
      .safeAreaInset(edge: .top, spacing: 0) {
        VStack(spacing: 8) {
          HStack(spacing: 8) {
            if isSearchExpanded {
              HStack(spacing: 10) {
                Image(systemName: "magnifyingglass")
                  .foregroundStyle(.secondary)

                TextField(
                  "City, neighborhood, address, or place",
                  text: $searchText
                )
                .focused($searchIsFocused)
                .submitLabel(.search)
                .onSubmit {
                  searchIsFocused = false
                  Task { await submitSearch() }
                }

                Button("Close Search", systemImage: "xmark.circle.fill") {
                  collapseSearch()
                }
                .labelStyle(.iconOnly)
                .foregroundStyle(.secondary)
              }
              .padding(.horizontal, 14)
              .frame(height: 46)
              .frame(maxWidth: .infinity)
              .background(.regularMaterial, in: Capsule())
              .transition(.scale(scale: 0.25, anchor: .leading).combined(with: .opacity))
            } else {
              Button("Search Map", systemImage: "magnifyingglass") {
                withAnimation(.snappy) {
                  isSearchExpanded = true
                }
                searchIsFocused = true
              }
              .labelStyle(.iconOnly)
              .buttonStyle(.plain)
              .font(.headline)
              .frame(width: 46, height: 46)
              .background(.regularMaterial, in: Circle())
              .transition(.scale.combined(with: .opacity))
            }

            Spacer(minLength: 0)

            Button("Refresh", systemImage: "arrow.clockwise") {
              Task { await refresh() }
            }
            .labelStyle(.iconOnly)
            .buttonStyle(.plain)
            .font(.headline)
            .frame(width: 46, height: 46)
            .background(.regularMaterial, in: Circle())
            .disabled(isRefreshing)
            .overlay {
              if isRefreshing {
                ProgressView()
                  .controlSize(.small)
                  .frame(width: 46, height: 46)
              }
            }
          }
          .frame(maxWidth: .infinity, alignment: .leading)

          if searchIsFocused && !searchModel.suggestions.isEmpty {
            VStack(spacing: 0) {
              ForEach(searchModel.suggestions.prefix(5)) { suggestion in
                Button {
                  searchText = suggestion.title
                  searchModel.clearSuggestions()
                  searchIsFocused = false
                  Task { await selectSearchSuggestion(suggestion) }
                } label: {
                  VStack(alignment: .leading, spacing: 2) {
                    Text(suggestion.title)
                      .foregroundStyle(.primary)
                    if !suggestion.subtitle.isEmpty {
                      Text(suggestion.subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                  }
                  .frame(maxWidth: .infinity, alignment: .leading)
                  .padding(.horizontal, 14)
                  .padding(.vertical, 8)
                }
                .buttonStyle(.plain)
              }
            }
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
            .padding(.trailing, 54)
          }
        }
        .animation(.snappy, value: isSearchExpanded)
        .shadow(radius: 5, y: 2)
        .padding(.horizontal)
        .padding(.top, 8)
        .padding(.bottom, 6)
      }
      .sheet(item: $detailGroup, onDismiss: {
        selectedGroupID = nil
      }) { group in
        NavigationStack {
          PlaceDetailSheet(group: group)
        }
        .presentationDetents([.fraction(0.58), .large])
        .presentationDragIndicator(.visible)
        .presentationContentInteraction(.scrolls)
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

  private func collapseSearch() {
    searchText = ""
    searchModel.clearSuggestions()
    searchIsFocused = false
    withAnimation(.snappy) {
      isSearchExpanded = false
    }
  }
}

private struct PlaceDetailSheet: View {
  let group: MappedPlaceGroup
  @Environment(\.dismiss) private var dismiss

  var body: some View {
    ScrollView {
      LazyVStack(alignment: .leading, spacing: 20) {
        HStack(alignment: .center, spacing: 12) {
          Text(group.name)
            .font(.title2.bold())
            .frame(maxWidth: .infinity, alignment: .leading)

          if let mapsURL = group.primary.appleMapsURL {
            Link(destination: mapsURL) {
              Label("Maps", systemImage: "map.fill")
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
          }
        }

        VStack(alignment: .leading, spacing: 12) {
          ForEach(group.places) { place in
            SourceDetailCard(
              place: place,
              showsDetails: group.places.count > 1
            )
          }
        }

        if !group.primary.whyItsCool.isEmpty {
          VStack(alignment: .leading, spacing: 6) {
            Text("Why It’s Cool")
              .font(.headline)
            Text(group.primary.whyItsCool)
              .font(.subheadline)
          }
        }

        if !group.dishes.isEmpty {
          VStack(alignment: .leading, spacing: 6) {
            Text("Things to Try")
              .font(.headline)
            Text(group.dishes.joined(separator: " · "))
              .font(.subheadline)
              .foregroundStyle(.orange)
          }
        }

        if !group.tags.isEmpty {
          VStack(alignment: .leading, spacing: 6) {
            Text("Tags")
              .font(.headline)
            Text(group.tags.joined(separator: " · "))
              .font(.subheadline)
              .foregroundStyle(.secondary)
          }
        }

      }
      .padding(.horizontal)
      .padding(.bottom, 28)
    }
    .navigationTitle("Place Details")
    .navigationBarTitleDisplayMode(.inline)
    .toolbar {
      ToolbarItem(placement: .confirmationAction) {
        Button("Done") { dismiss() }
      }
    }
  }
}

private struct SourceDetailCard: View {
  let place: SavedPlace
  let showsDetails: Bool

  var body: some View {
    VStack(alignment: .leading, spacing: 12) {
      Link(destination: place.linkedSourceURL) {
        HStack(spacing: 12) {
          Image(systemName: place.sourceSystemImage)
            .font(.title3)
            .frame(width: 28)

          VStack(alignment: .leading, spacing: 3) {
            Text(place.sourceLinkText)
              .font(.subheadline.weight(.semibold))
            if let mediaReference = place.mediaReferenceText {
              Text(mediaReference)
                .font(.caption)
                .foregroundStyle(.secondary)
            }
          }

          Spacer()
          Image(systemName: "arrow.up.right")
            .font(.caption.weight(.bold))
        }
        .contentShape(Rectangle())
      }
      .buttonStyle(.plain)
      .foregroundStyle(.tint)

      if showsDetails, !place.whyItsCool.isEmpty {
        Text(place.whyItsCool)
          .font(.subheadline)
      }

      if showsDetails, !place.dishes.isEmpty {
        Text(place.dishes.joined(separator: " · "))
          .font(.caption)
          .foregroundStyle(.orange)
      }
    }
    .padding(14)
    .background(.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 14))
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
        if let mapsURL = place.appleMapsURL {
          Link("Maps", destination: mapsURL)
        }
        Link(place.sourceLinkText, destination: place.linkedSourceURL)
      }
      .font(.caption.weight(.semibold))
    }
    .padding(.vertical, 6)
  }
}
