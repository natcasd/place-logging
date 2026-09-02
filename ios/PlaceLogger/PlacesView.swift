import Combine
import MapKit
import SwiftUI

@MainActor
final class PlacesModel: ObservableObject {
  @Published var places: [SavedPlace] = []
  @Published var sources: [SavedSource] = []
  @Published var isLoading = false
  @Published var errorMessage: String?

  private let api = PlaceLoggerAPI()

  func load() async {
    guard !isLoading else { return }
    isLoading = true
    defer { isLoading = false }
    do {
      async let loadedThings = api.fetchThings()
      async let loadedSources = api.fetchSources()
      places = try await loadedThings
      sources = try await loadedSources
      errorMessage = nil
    } catch {
      errorMessage = error.localizedDescription
    }
  }

  func delete(_ place: SavedPlace) async throws {
    try await api.deleteThing(id: place.id)
    places.removeAll { candidate in
      if let googlePlaceID = place.googlePlaceID, !googlePlaceID.isEmpty {
        return candidate.googlePlaceID == googlePlaceID
      }
      return candidate.id == place.id
    }
    sources = try await api.fetchSources()
  }
}

struct PlacesView: View {
  @ObservedObject var router: PlaceLoggerRouter
  @StateObject private var model = PlacesModel()
  @Environment(\.scenePhase) private var scenePhase
  @State private var path: [Int] = []
  @State private var selectedTab: PlacesTab = .saved

  var body: some View {
    NavigationStack(path: $path) {
      Group {
        if model.isLoading && model.places.isEmpty && model.sources.isEmpty {
          ProgressView("Loading saved things…")
        } else if let error = model.errorMessage,
                  model.places.isEmpty && model.sources.isEmpty {
          ContentUnavailableView {
            Label("Couldn’t Load Saves", systemImage: "wifi.exclamationmark")
          } description: {
            Text(error)
          } actions: {
            Button("Try Again") { Task { await model.load() } }
          }
        } else {
          TabView(selection: $selectedTab) {
            PlacesList(
              places: model.places,
              refresh: { await model.load() },
              deletePlace: { place in try await model.delete(place) }
            )
            .tabItem {
              Label("Saved", systemImage: "tray.full")
            }
            .tag(PlacesTab.saved)

            PlacesMap(
              places: model.places,
              isRefreshing: model.isLoading,
              refresh: { await model.load() },
              deletePlace: { place in try await model.delete(place) }
            )
              .tabItem {
                Label("Around Me", systemImage: "location")
              }
              .tag(PlacesTab.aroundMe)

            SourcesList(sources: model.sources)
              .tabItem {
                Label("Sources", systemImage: "rectangle.stack")
              }
              .tag(PlacesTab.sources)
          }
        }
      }
      .navigationTitle(selectedTab == .aroundMe ? "" : "Place Logger")
      .navigationBarTitleDisplayMode(selectedTab == .aroundMe ? .inline : .automatic)
      .toolbar(selectedTab == .aroundMe ? .hidden : .visible, for: .navigationBar)
      .toolbar {
        ToolbarItem(placement: .topBarTrailing) {
          if selectedTab != .aroundMe {
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
      selectedTab = .saved
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
  case saved
  case aroundMe
  case sources
}

private struct PlacesList: View {
  let places: [SavedPlace]
  let refresh: () async -> Void
  let deletePlace: (SavedPlace) async throws -> Void
  @State private var pendingDeletion: SavedPlace?
  @State private var deletionError: String?
  @State private var searchText = ""
  @State private var selectedType = "All"

  private var types: [String] {
    Array(Set(places.map(\.displayType))).sorted()
  }

  private var filteredPlaces: [SavedPlace] {
    places.filter { place in
      let matchesType = selectedType == "All" || place.displayType == selectedType
      let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
      let matchesSearch = query.isEmpty
        || place.name.localizedCaseInsensitiveContains(query)
        || place.detailedDescription.localizedCaseInsensitiveContains(query)
        || place.displayType.localizedCaseInsensitiveContains(query)
      return matchesType && matchesSearch
    }
  }

  var body: some View {
    Group {
      if places.isEmpty {
        ContentUnavailableView(
          "No Saved Things",
          systemImage: "tray",
          description: Text("Share an Instagram Reel or YouTube video to get started.")
        )
      } else {
        List {
          if !types.isEmpty {
            Picker("Type", selection: $selectedType) {
              Text("All").tag("All")
              ForEach(types, id: \.self) { type in
                Text(type).tag(type)
              }
            }
            .pickerStyle(.menu)
          }

          ForEach(filteredPlaces) { place in
            PlaceRow(place: place)
              .swipeActions {
                Button("Delete", systemImage: "trash", role: .destructive) {
                  pendingDeletion = place
                }
              }
          }
        }
        .listStyle(.plain)
        .searchable(text: $searchText, prompt: "Search saved things")
        .refreshable { await refresh() }
      }
    }
    .confirmationDialog(
      pendingDeletion.map { "Delete \($0.name)?" } ?? "Delete Thing?",
      isPresented: Binding(
        get: { pendingDeletion != nil },
        set: { if !$0 { pendingDeletion = nil } }
      ),
      titleVisibility: .visible
    ) {
      if let place = pendingDeletion {
        Button("Delete Thing", role: .destructive) {
          pendingDeletion = nil
          Task {
            do {
              try await deletePlace(place)
            } catch {
              deletionError = error.localizedDescription
            }
          }
        }
      }
      Button("Cancel", role: .cancel) {
        pendingDeletion = nil
      }
    } message: {
      if let place = pendingDeletion {
        let count = savedReferenceCount(for: place)
        Text(deleteMessage(name: place.name, referenceCount: count))
      }
    }
    .alert(
      "Couldn’t Delete Thing",
      isPresented: Binding(
        get: { deletionError != nil },
        set: { if !$0 { deletionError = nil } }
      )
    ) {
      Button("OK", role: .cancel) { deletionError = nil }
    } message: {
      Text(deletionError ?? "Please try again.")
    }
  }

  private func savedReferenceCount(for place: SavedPlace) -> Int {
    guard let googlePlaceID = place.googlePlaceID, !googlePlaceID.isEmpty else {
      return 1
    }
    return Set(
      places
        .filter { $0.googlePlaceID == googlePlaceID }
        .map(\.sourceURL)
    ).count
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

private struct SourcesList: View {
  let sources: [SavedSource]

  var body: some View {
    if sources.isEmpty {
      ContentUnavailableView(
        "No Saved Sources",
        systemImage: "rectangle.stack",
        description: Text("Original posts will appear here after they are saved.")
      )
    } else {
      List(sources) { source in
        Link(destination: source.sourceURL) {
          VStack(alignment: .leading, spacing: 6) {
            HStack {
              Text(source.title)
                .font(.headline)
                .foregroundStyle(.primary)
              Spacer()
              if source.needsReview {
                Label("Needs Review", systemImage: "exclamationmark.circle")
                  .font(.caption.weight(.semibold))
                  .foregroundStyle(.orange)
              }
            }

            if let caption = source.caption, !caption.isEmpty {
              Text(caption)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(3)
            } else if let summary = source.summary, !summary.isEmpty {
              Text(summary)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(3)
            }

            HStack(spacing: 12) {
              Label("\(source.thingCount) things", systemImage: "tray.full")
              if source.mediaPreserved {
                Label("Media preserved", systemImage: "checkmark.circle")
              }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
          }
          .padding(.vertical, 5)
        }
      }
      .listStyle(.plain)
    }
  }
}

private struct PlacesMap: View {
  let places: [SavedPlace]
  let isRefreshing: Bool
  let refresh: () async -> Void
  let deletePlace: (SavedPlace) async throws -> Void
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
    MappedPlaceGroup.make(from: places.filter(\.isCurrentlyRelevant))
  }

  var body: some View {
    if groups.isEmpty {
      ContentUnavailableView(
        "Nothing Nearby Yet",
        systemImage: "mappin.slash",
        description: Text("Current things appear here after their locations are resolved.")
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
          PlaceDetailSheet(group: group) {
            try await deletePlace(group.primary)
          }
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
  let deletePlace: () async throws -> Void
  @Environment(\.dismiss) private var dismiss
  @State private var isConfirmingDeletion = false
  @State private var isDeleting = false
  @State private var deletionError: String?

  var body: some View {
    ScrollView {
      LazyVStack(alignment: .leading, spacing: 20) {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
          Text(group.name)
            .font(.title2.bold())

          Spacer(minLength: 8)

          if isDeleting {
            ProgressView()
              .controlSize(.small)
          } else {
            Menu {
              if let mapsURL = group.primary.appleMapsURL {
                Link(destination: mapsURL) {
                  Label("Open in Maps", systemImage: "map")
                }

                Divider()
              }

              Button("Delete Place", systemImage: "trash", role: .destructive) {
                isConfirmingDeletion = true
              }
            } label: {
              Image(systemName: "ellipsis")
                .font(.headline)
                .frame(width: 36, height: 36)
                .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(.primary)
            .accessibilityLabel("More Options")
          }
        }

        HStack(spacing: 10) {
          Text(group.primary.displayType)
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
          if let availability = group.primary.availabilityText {
            Label(availability, systemImage: "calendar")
              .font(.caption)
              .foregroundStyle(.secondary)
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

        if !group.primary.detailedDescription.isEmpty {
          VStack(alignment: .leading, spacing: 6) {
            Text("About")
              .font(.headline)
            Text(group.primary.detailedDescription)
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
      .padding(.top, 18)
      .padding(.bottom, 28)
    }
    .confirmationDialog(
      "Delete \(group.name)?",
      isPresented: $isConfirmingDeletion,
      titleVisibility: .visible
    ) {
      Button("Delete Place", role: .destructive) {
        Task {
          isDeleting = true
          defer { isDeleting = false }
          do {
            try await deletePlace()
            dismiss()
          } catch {
            deletionError = error.localizedDescription
          }
        }
      }
      Button("Cancel", role: .cancel) {}
    } message: {
      Text(
        deleteMessage(
          name: group.name,
          referenceCount: Set(group.places.map(\.sourceURL)).count
        )
      )
    }
    .alert(
      "Couldn’t Delete Place",
      isPresented: Binding(
        get: { deletionError != nil },
        set: { if !$0 { deletionError = nil } }
      )
    ) {
      Button("OK", role: .cancel) { deletionError = nil }
    } message: {
      Text(deletionError ?? "Please try again.")
    }
  }
}

private func deleteMessage(name: String, referenceCount: Int) -> String {
  let references = referenceCount == 1
    ? "its saved post"
    : "its \(referenceCount) saved posts"
  return "This removes \(name) and \(references). Other places from those posts will remain."
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

      if showsDetails, !place.detailedDescription.isEmpty {
        Text(place.detailedDescription)
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

      HStack(spacing: 10) {
        Text(place.displayType)
          .font(.caption.weight(.semibold))
          .foregroundStyle(.secondary)
        if let availability = place.availabilityText {
          Label(availability, systemImage: "calendar")
            .font(.caption)
            .foregroundStyle(.secondary)
        }
      }

      if let address = place.formattedAddress, !address.isEmpty {
        Label(address, systemImage: "mappin.and.ellipse")
          .font(.subheadline)
          .foregroundStyle(.secondary)
      }

      if !place.detailedDescription.isEmpty {
        Text(place.detailedDescription)
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
