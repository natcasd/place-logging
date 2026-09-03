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
    places.removeAll { $0.id == place.id }
    sources = try await api.fetchSources()
  }

  func deleteThingCard(_ thing: SavedPlace) async throws {
    try await api.deleteThing(id: thing.id)
    places.removeAll { $0.id == thing.id }
    sources = try await api.fetchSources()
  }
}

struct PlacesView: View {
  @ObservedObject var router: PlaceLoggerRouter
  @StateObject private var model = PlacesModel()
  @Environment(\.scenePhase) private var scenePhase
  @State private var path: [Int] = []
  @State private var selectedTab: PlacesTab = .aroundMe

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
            PlacesMap(
              places: model.places,
              isRefreshing: model.isLoading,
              refresh: { await model.load() },
              deleteThingCard: { thing in try await model.deleteThingCard(thing) }
            )
              .tabItem {
                Label("Around Me", systemImage: "location")
              }
              .tag(PlacesTab.aroundMe)

            PlacesList(
              places: model.places,
              refresh: { await model.load() },
              deletePlace: { place in try await model.delete(place) }
            )
            .tabItem {
              Label("Saved", systemImage: "tray.full")
            }
            .tag(PlacesTab.saved)

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
          places: model.places.filter { thing in
            thing.itemID == itemID || thing.sources.contains { $0.itemID == itemID }
          },
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
        Text(deleteMessage(thing: place))
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

}

private struct MappedThingGroup: Identifiable {
  let thing: SavedPlace

  var id: Int { thing.id }
  var primary: SavedPlace { thing }
  var name: String { primary.name }
  var type: String { primary.displayType }
  var sourceCount: Int { primary.sources.count }
  var dishes: [String] { primary.dishes }
}

private struct MappedPlaceGroup: Identifiable {
  let id: String
  var places: [SavedPlace]

  var primary: SavedPlace { places[0] }
  var name: String {
    places.compactMap(\.locationName).first { !$0.isEmpty } ?? primary.name
  }
  var thingGroups: [MappedThingGroup] { places.map { MappedThingGroup(thing: $0) } }
  var coordinate: CLLocationCoordinate2D {
    CLLocationCoordinate2D(
      latitude: primary.latitude ?? 0,
      longitude: primary.longitude ?? 0
    )
  }

  static func make(from places: [SavedPlace]) -> [MappedPlaceGroup] {
    var groups: [MappedPlaceGroup] = []
    var indexes: [String: Int] = [:]

    for place in places {
      guard place.latitude != nil, place.longitude != nil else { continue }
      let key = place.locationID.map { "location:\($0)" } ?? "saved:\(place.id)"
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
  let deleteThingCard: (SavedPlace) async throws -> Void
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
            try await deleteThingCard($0.primary)
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
  let deleteThing: (MappedThingGroup) async throws -> Void
  @Environment(\.dismiss) private var dismiss
  @State private var pendingDeletion: MappedThingGroup?
  @State private var deletingThingID: Int?
  @State private var deletionError: String?

  var body: some View {
    ScrollView {
      LazyVStack(alignment: .leading, spacing: 20) {
        if let thing = group.thingGroups.first, group.thingGroups.count == 1 {
          SingleThingLocationDetails(
            thing: thing,
            address: group.primary.formattedAddress,
            mapsURL: group.primary.appleMapsURL,
            isDeleting: deletingThingID == thing.id,
            requestDeletion: { pendingDeletion = thing }
          )
        } else {
          LocationHeader(group: group)

          Text("Things at this location")
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
            .textCase(.uppercase)

          ForEach(group.thingGroups) { thing in
            ThingAtLocationCard(
              thing: thing,
              isDeleting: deletingThingID == thing.id,
              requestDeletion: { pendingDeletion = thing }
            )
          }
        }
      }
      .padding(.horizontal)
      .padding(.top, 18)
      .padding(.bottom, 28)
    }
    .confirmationDialog(
      pendingDeletion.map { "Delete \($0.name)?" } ?? "Delete Thing?",
      isPresented: Binding(
        get: { pendingDeletion != nil },
        set: { if !$0 { pendingDeletion = nil } }
      ),
      titleVisibility: .visible
    ) {
      if let thing = pendingDeletion {
        Button("Delete Thing", role: .destructive) {
          pendingDeletion = nil
          Task {
            deletingThingID = thing.id
            defer { deletingThingID = nil }
            do {
              try await deleteThing(thing)
              dismiss()
            } catch {
              deletionError = error.localizedDescription
            }
          }
        }
      }
      Button("Cancel", role: .cancel) { pendingDeletion = nil }
    } message: {
      if let thing = pendingDeletion {
        Text(logicalThingDeleteMessage(thing))
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
}

private func deleteMessage(thing: SavedPlace) -> String {
  let references = thing.sources.count == 1
    ? "its saved reference"
    : "its \(thing.sources.count) saved references"
  return "This removes \(thing.name) and \(references). Original source posts stay saved."
}

private func logicalThingDeleteMessage(_ thing: MappedThingGroup) -> String {
  let references = thing.sourceCount == 1
    ? "its saved reference"
    : "its \(thing.sourceCount) saved references"
  return "This removes \(thing.name) and \(references). Original source posts and other things at this location stay saved."
}

private struct LocationHeader: View {
  let group: MappedPlaceGroup

  var body: some View {
    HStack(alignment: .firstTextBaseline, spacing: 8) {
      VStack(alignment: .leading, spacing: 5) {
        Text(group.name)
          .font(.title2.bold())
        if let address = group.primary.formattedAddress, !address.isEmpty {
          Text(address)
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
        Text("\(group.thingGroups.count) saved things")
          .font(.caption)
          .foregroundStyle(.secondary)
      }

      Spacer(minLength: 8)

      if let mapsURL = group.primary.appleMapsURL {
        Link(destination: mapsURL) {
          Image(systemName: "map")
            .font(.headline)
            .frame(width: 36, height: 36)
            .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Open in Maps")
      }
    }
  }
}

private struct SingleThingLocationDetails: View {
  let thing: MappedThingGroup
  let address: String?
  let mapsURL: URL?
  let isDeleting: Bool
  let requestDeletion: () -> Void

  var body: some View {
    HStack(alignment: .firstTextBaseline, spacing: 8) {
      VStack(alignment: .leading, spacing: 5) {
        Text(thing.name)
          .font(.title2.bold())
        ThingMetadata(thing: thing)
      }

      Spacer(minLength: 8)
      ThingActions(
        mapsURL: mapsURL,
        isDeleting: isDeleting,
        requestDeletion: requestDeletion
      )
    }

    if let address, !address.isEmpty {
      Text(address)
        .font(.subheadline)
        .foregroundStyle(.secondary)
    }

    ThingContent(thing: thing)

    SourceLinks(thing: thing.primary)
  }
}

private struct ThingAtLocationCard: View {
  let thing: MappedThingGroup
  let isDeleting: Bool
  let requestDeletion: () -> Void

  var body: some View {
    VStack(alignment: .leading, spacing: 12) {
      HStack(alignment: .firstTextBaseline, spacing: 8) {
        VStack(alignment: .leading, spacing: 5) {
          Text(thing.name)
            .font(.headline)
          ThingMetadata(thing: thing)
        }

        Spacer(minLength: 8)
        ThingActions(
          mapsURL: nil,
          isDeleting: isDeleting,
          requestDeletion: requestDeletion
        )
      }

      ThingContent(thing: thing)

      SourceLinks(thing: thing.primary)
    }
    .padding(14)
    .background(.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 14))
  }
}

private struct ThingMetadata: View {
  let thing: MappedThingGroup

  var body: some View {
    HStack(spacing: 8) {
      Text(thing.type)
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
      if let availability = thing.primary.availabilityText {
        Label(availability, systemImage: "calendar")
          .font(.caption)
          .foregroundStyle(.secondary)
      }
      Text("\(thing.sourceCount) \(thing.sourceCount == 1 ? "post" : "posts")")
        .font(.caption)
        .foregroundStyle(.secondary)
    }
  }
}

private struct ThingActions: View {
  let mapsURL: URL?
  let isDeleting: Bool
  let requestDeletion: () -> Void

  var body: some View {
    HStack(spacing: 6) {
      if let mapsURL {
        Link(destination: mapsURL) {
          Image(systemName: "map")
            .frame(width: 34, height: 34)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Open in Maps")
      }

      if isDeleting {
        ProgressView()
          .controlSize(.small)
          .frame(width: 34, height: 34)
      } else {
        Menu {
          Button("Delete Thing", systemImage: "trash", role: .destructive) {
            requestDeletion()
          }
        } label: {
          Image(systemName: "ellipsis")
            .foregroundStyle(.secondary)
            .frame(width: 34, height: 34)
        }
        .accessibilityLabel("More Actions")
      }
    }
  }
}

private struct ThingContent: View {
  let thing: MappedThingGroup

  var body: some View {
    if !thing.primary.detailedDescription.isEmpty {
      Text(thing.primary.detailedDescription)
        .font(.subheadline)
    }

    if !thing.dishes.isEmpty {
      VStack(alignment: .leading, spacing: 5) {
        Text("Things to Try")
          .font(.subheadline.weight(.semibold))
        Text(thing.dishes.joined(separator: " · "))
          .font(.subheadline)
          .foregroundStyle(.orange)
      }
    }
  }
}

private struct SourceLinks: View {
  let thing: SavedPlace

  var body: some View {
    VStack(alignment: .leading, spacing: 8) {
      Text("Saved from \(sourceCount) \(sourceCount == 1 ? "post" : "posts")")
        .font(.subheadline.weight(.semibold))

      ForEach(thing.sources) { source in
        Link(destination: source.linkedSourceURL) {
          HStack(spacing: 12) {
            Image(systemName: source.sourceSystemImage)
              .font(.title3)
              .frame(width: 28)

            VStack(alignment: .leading, spacing: 3) {
              Text(source.sourceLinkText)
                .font(.subheadline.weight(.semibold))
              if let mediaReference = source.mediaReferenceText {
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
      }
    }
  }

  private var sourceCount: Int {
    thing.sources.count
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
