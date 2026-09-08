import Combine
import MapKit
import SwiftUI
import UIKit

@MainActor
final class PlacesModel: ObservableObject {
  @Published var places: [SavedEntry] = []
  @Published var activity: [IngestActivity] = []
  @Published var isLoading = false
  @Published var errorMessage: String?

  private let api = PlaceLoggerAPI()

  func load() async {
    guard !isLoading else { return }
    isLoading = true
    defer { isLoading = false }
    do {
      async let loadedEntries = api.fetchEntries()
      async let loadedActivity = api.fetchActivity()
      places = try await loadedEntries
      activity = try await loadedActivity
      errorMessage = nil
    } catch {
      errorMessage = error.localizedDescription
    }
  }

  func ensureLoaded() async {
    if isLoading {
      while isLoading {
        try? await Task.sleep(nanoseconds: 50_000_000)
      }
      return
    }
    await load()
  }

  func delete(_ place: SavedEntry) async throws {
    try await api.deleteEntry(id: place.id)
    places.removeAll { $0.id == place.id }
    activity = try await api.fetchActivity()
  }

  func deleteEntryCard(_ entry: SavedEntry) async throws {
    try await api.deleteEntry(id: entry.id)
    places.removeAll { $0.id == entry.id }
    activity = try await api.fetchActivity()
  }
}

struct PlacesView: View {
  @ObservedObject var router: PlaceLoggerRouter
  @StateObject private var model = PlacesModel()
  @Environment(\.scenePhase) private var scenePhase
  @State private var path: [PlacesNavigation] = []
  @State private var selectedTab: PlacesTab = .aroundMe
  @State private var requestedMapEntryID: Int?
  @State private var aroundMeFilterType: String?

  var body: some View {
    NavigationStack(path: $path) {
      Group {
        if model.isLoading && model.places.isEmpty && model.activity.isEmpty {
          ProgressView("Loading saved entries…")
        } else if let error = model.errorMessage,
                  model.places.isEmpty && model.activity.isEmpty {
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
              requestedEntryID: $requestedMapEntryID,
              selectedType: $aroundMeFilterType,
              refresh: { await model.load() },
              deleteEntryCard: { entry in try await model.deleteEntryCard(entry) }
            )
              .tabItem {
                Label("Around Me", systemImage: "location")
              }
              .tag(PlacesTab.aroundMe)

            PlacesList(
              places: model.places,
              refresh: { await model.load() }
            )
            .tabItem {
              Label("Saved", systemImage: "tray.full")
            }
            .tag(PlacesTab.saved)

            ActivityList(activity: model.activity)
              .tabItem {
                Label("Activity", systemImage: "clock.arrow.circlepath")
              }
              .tag(PlacesTab.activity)
          }
        }
      }
      .navigationTitle(selectedTab == .aroundMe ? "" : selectedTab == .saved ? "Saved" : "Activity")
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
      .navigationDestination(for: PlacesNavigation.self) { destination in
        switch destination {
        case .entry(let entryID):
          SavedItemView(
            places: model.places.filter { $0.id == entryID },
            isLoading: model.isLoading,
            deleteEntry: { entry in try await model.deleteEntryCard(entry) }
          )
        case .activity(let ingestID):
          if let run = model.activity.first(where: { $0.id == ingestID }) {
            ActivityDetail(activity: run)
          } else {
            ContentUnavailableView("Activity Not Found", systemImage: "clock.badge.questionmark")
          }
        case .legacyItem(let itemID):
          SavedItemView(
            places: model.places.filter { entry in
              entry.itemID == itemID || entry.sources.contains { $0.itemID == itemID }
            },
            isLoading: model.isLoading,
            deleteEntry: { entry in try await model.deleteEntryCard(entry) }
          )
        case .category(let type):
          SavedCategoryList(
            category: SavedCategory.category(for: type),
            places: model.places.filter { $0.displayType.caseInsensitiveCompare(type) == .orderedSame },
            refresh: { await model.load() },
            deletePlace: { place in try await model.delete(place) },
            viewAroundMe: {
              aroundMeFilterType = type
              path = []
              selectedTab = .aroundMe
            }
          )
        }
      }
    }
    .task { await model.load() }
    .task(id: router.pendingDestination) {
      guard let destination = router.pendingDestination else { return }
      await model.ensureLoaded()
      switch destination {
      case .mapEntry(let entryID):
        if let entry = model.places.first(where: { $0.id == entryID }),
           entry.latitude != nil, entry.longitude != nil, entry.isCurrentlyRelevant {
          path = []
          selectedTab = .aroundMe
          aroundMeFilterType = nil
          requestedMapEntryID = entryID
        } else {
          selectedTab = .saved
          path = [.entry(entryID)]
        }
      case .savedEntry(let entryID):
        selectedTab = .saved
        path = [.entry(entryID)]
      case .activity(let ingestID):
        selectedTab = .activity
        path = [.activity(ingestID)]
      case .legacyItem(let itemID):
        selectedTab = .saved
        path = [.legacyItem(itemID)]
      }
      router.pendingDestination = nil
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
  case activity
}

private enum PlacesNavigation: Hashable {
  case entry(Int)
  case activity(Int)
  case legacyItem(Int)
  case category(String)
}

private struct PlacesList: View {
  let places: [SavedEntry]
  let refresh: () async -> Void

  private var categories: [SavedCategory] { SavedCategory.categories(for: places) }

  var body: some View {
    Group {
      if places.isEmpty {
        ContentUnavailableView(
          "No Saved Entries",
          systemImage: "tray",
          description: Text("Share an Instagram Reel or YouTube video to get started.")
        )
      } else {
        ScrollView {
          LazyVGrid(
            columns: [
              GridItem(.flexible(minimum: 0), spacing: 12),
              GridItem(.flexible(minimum: 0), spacing: 12),
            ],
            spacing: 12
          ) {
            ForEach(categories) { category in
              NavigationLink(value: PlacesNavigation.category(category.type)) {
                SavedCategoryTile(category: category)
              }
              .buttonStyle(.plain)
              .accessibilityLabel(category.title)
            }
          }
          .padding(.horizontal)
          .padding(.top, 12)
          .padding(.bottom, 24)
        }
        .refreshable { await refresh() }
      }
    }
  }
}

private struct SavedCategoryTile: View {
  let category: SavedCategory

  var body: some View {
    GeometryReader { geometry in
      ZStack(alignment: .bottomLeading) {
        if let artAssetName = category.artAssetName {
          Image(artAssetName)
            .resizable()
            .scaledToFill()
            .frame(width: geometry.size.width, height: geometry.size.height)
            .clipped()
            .saturation(0)
            .colorMultiply(category.artTint)
        } else {
          LinearGradient(
            colors: [.purple, .indigo],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
          )
          Image(systemName: category.icon)
            .font(.system(size: 38, weight: .medium))
            .foregroundStyle(.white.opacity(0.88))
        }

        LinearGradient(
          colors: [.black.opacity(0.3), .clear],
          startPoint: .bottom,
          endPoint: .top
        )

        Text(category.title)
          .font(.headline.weight(.bold))
          .foregroundStyle(.white)
          .padding(14)
      }
      .frame(width: geometry.size.width, height: geometry.size.height)
    }
    .aspectRatio(1.5, contentMode: .fit)
    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
    .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
  }
}

private struct SavedCategoryList: View {
  let category: SavedCategory
  let places: [SavedEntry]
  let refresh: () async -> Void
  let deletePlace: (SavedEntry) async throws -> Void
  let viewAroundMe: () -> Void
  @State private var pendingDeletion: SavedEntry?
  @State private var deletionError: String?
  @State private var searchText = ""

  private var filteredPlaces: [SavedEntry] {
    let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !query.isEmpty else { return places }
    return places.filter {
      $0.name.localizedCaseInsensitiveContains(query)
        || $0.detailedDescription.localizedCaseInsensitiveContains(query)
    }
  }

  var body: some View {
    List {
      if category.isPlaceBased {
        Section {
          Button(action: viewAroundMe) {
            Label("View in Around Me", systemImage: "map")
              .font(.body.weight(.semibold))
          }
        }
      }

      ForEach(filteredPlaces) { place in
        NavigationLink(value: PlacesNavigation.entry(place.id)) {
          PlaceRow(place: place)
        }
        .swipeActions {
          Button("Delete", systemImage: "trash", role: .destructive) {
            pendingDeletion = place
          }
        }
      }
    }
    .listStyle(.plain)
    .navigationTitle(category.title)
    .navigationBarTitleDisplayMode(.inline)
    .searchable(text: $searchText, prompt: "Search \(category.title.lowercased())")
    .refreshable { await refresh() }
    .confirmationDialog(
      pendingDeletion.map { "Delete \($0.name)?" } ?? "Delete Entry?",
      isPresented: Binding(
        get: { pendingDeletion != nil },
        set: { if !$0 { pendingDeletion = nil } }
      ),
      titleVisibility: .visible
    ) {
      if let place = pendingDeletion {
        Button("Delete Entry", role: .destructive) {
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
      Button("Cancel", role: .cancel) { pendingDeletion = nil }
    } message: {
      if let place = pendingDeletion { Text(deleteMessage(entry: place)) }
    }
    .alert(
      "Couldn’t Delete Entry",
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

private struct MappedEntryGroup: Identifiable {
  let entry: SavedEntry

  var id: Int { entry.id }
  var primary: SavedEntry { entry }
  var name: String { primary.name }
  var type: String { primary.displayType }
  var sourceCount: Int { primary.sources.count }
  var dishes: [String] { primary.dishes }
}

private struct MappedPlaceGroup: Identifiable {
  let id: String
  var places: [SavedEntry]

  var primary: SavedEntry { places[0] }
  var category: SavedCategory { SavedCategory.category(for: primary.displayType) }
  var name: String {
    if let googleName = places.compactMap(\.locationName).first(where: { !$0.isEmpty }) {
      return googleName
    }
    return places.first(where: { !$0.isTemporaryLocationEntry })?.name ?? primary.name
  }
  var entryGroups: [MappedEntryGroup] { places.map { MappedEntryGroup(entry: $0) } }
  var coordinate: CLLocationCoordinate2D {
    CLLocationCoordinate2D(
      latitude: primary.latitude ?? 0,
      longitude: primary.longitude ?? 0
    )
  }

  static func make(from places: [SavedEntry]) -> [MappedPlaceGroup] {
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

private extension SavedEntry {
  var isTemporaryLocationEntry: Bool {
    if startsAt != nil || endsAt != nil || recurrenceText != nil { return true }
    let normalizedType = displayType.lowercased()
    return [
      "concert", "event", "exhibit", "exhibition", "festival", "performance",
      "pop-up", "popup", "screening", "show",
    ].contains { normalizedType.contains($0) }
  }
}

private struct ActivityList: View {
  let activity: [IngestActivity]

  var body: some View {
    if activity.isEmpty {
      ContentUnavailableView(
        "No Activity Yet",
        systemImage: "clock.arrow.circlepath",
        description: Text("Shared posts and their processing results will appear here.")
      )
    } else {
      List(activity) { run in
        NavigationLink(value: PlacesNavigation.activity(run.id)) {
          VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
              Image(systemName: run.statusSystemImage)
                .foregroundStyle(run.statusColor)
              Text(run.title)
                .font(.headline)
              Spacer()
              Text(run.statusText)
                .font(.caption.weight(.semibold))
                .foregroundStyle(run.statusColor)
            }

            if !run.results.isEmpty {
              Text(run.results.prefix(3).map { "\($0.type) · \($0.name)" }.joined(separator: ", "))
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(2)
            } else if let message = run.errorMessage ?? run.events.last?.message {
              Text(message)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(2)
            }

            Text(run.startedAt)
              .font(.caption)
              .foregroundStyle(.tertiary)
          }
          .padding(.vertical, 5)
        }
      }
      .listStyle(.plain)
    }
  }
}

private struct ActivityDetail: View {
  let activity: IngestActivity

  var body: some View {
    List {
      Section {
        HStack(spacing: 10) {
          Image(systemName: activity.statusSystemImage)
            .font(.title2)
            .foregroundStyle(activity.statusColor)
          VStack(alignment: .leading, spacing: 2) {
            Text(activity.statusText)
              .font(.headline)
            Text(activity.startedAt)
              .font(.caption)
              .foregroundStyle(.secondary)
          }
        }

        if let error = activity.errorMessage, !error.isEmpty {
          Text(error)
            .foregroundStyle(.red)
        }

        Link(destination: activity.sourceURL) {
          Label("Open original post", systemImage: "arrow.up.right.square")
        }
      }

      if !activity.results.isEmpty {
        Section("Entries from this post") {
          ForEach(activity.results) { result in
            NavigationLink(value: PlacesNavigation.entry(result.entryID)) {
              VStack(alignment: .leading, spacing: 4) {
                Text(result.name)
                  .font(.headline)
                HStack(spacing: 8) {
                  Text(result.type)
                  Text(result.isNew ? "New Entry" : "Added source · \(result.sourceCount) total")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
              }
              .padding(.vertical, 3)
            }
          }
        }
      }

      if let summary = activity.summary, !summary.isEmpty {
        Section("Post summary") {
          Text(summary)
        }
      } else if let caption = activity.caption, !caption.isEmpty {
        Section("Caption") {
          Text(caption)
        }
      }

      if !activity.events.isEmpty {
        Section("Processing log") {
          ForEach(activity.events) { event in
            HStack(alignment: .top, spacing: 10) {
              Image(systemName: event.status == "failed" ? "xmark.circle.fill" : "checkmark.circle")
                .foregroundStyle(event.status == "failed" ? .red : .secondary)
              VStack(alignment: .leading, spacing: 2) {
                Text(event.message)
                Text(event.createdAt)
                  .font(.caption)
                  .foregroundStyle(.secondary)
              }
            }
          }
        }
      }
    }
    .navigationTitle(activity.title)
    .navigationBarTitleDisplayMode(.inline)
  }
}

private extension IngestActivity {
  var statusSystemImage: String {
    switch status {
    case "processing": return "arrow.triangle.2.circlepath"
    case "partial": return "exclamationmark.circle.fill"
    case "failed": return "xmark.circle.fill"
    default: return "checkmark.circle.fill"
    }
  }

  var statusColor: Color {
    switch status {
    case "processing": return .blue
    case "partial": return .orange
    case "failed": return .red
    default: return .green
    }
  }
}

private struct PlacesMap: View {
  let places: [SavedEntry]
  let isRefreshing: Bool
  @Binding var requestedEntryID: Int?
  @Binding var selectedType: String?
  let refresh: () async -> Void
  let deleteEntryCard: (SavedEntry) async throws -> Void
  @StateObject private var locationModel = LocationModel()
  @StateObject private var searchModel = MapSearchModel()
  @StateObject private var appleMapsDestinations = AppleMapsDestinationCache()
  @State private var cameraPosition: MapCameraPosition = .automatic
  @State private var selectedGroupID: String?
  @State private var detailGroup: MappedPlaceGroup?
  @State private var preferredDetailEntryID: Int?
  @State private var searchText = ""
  @State private var searchResult: MKMapItem?
  @State private var visibleRegion: MKCoordinateRegion?
  @State private var hasChosenInitialCamera = false
  @State private var isSearchExpanded = false
  @FocusState private var searchIsFocused: Bool

  private var groups: [MappedPlaceGroup] {
    let activeType = selectedType
    return MappedPlaceGroup.make(
      from: places.filter { place in
        guard place.isCurrentlyRelevant else { return false }
        guard let activeType else { return true }
        return place.displayType.caseInsensitiveCompare(activeType) == .orderedSame
      }
    )
  }

  var body: some View {
    if groups.isEmpty {
      ContentUnavailableView(
        "Nothing Nearby Yet",
        systemImage: "mappin.slash",
        description: Text("Current entries appear here after their locations are resolved.")
      )
    } else {
      Map(position: $cameraPosition, selection: $selectedGroupID) {
        UserAnnotation()

        ForEach(groups) { group in
          Marker(
            group.name,
            systemImage: group.category.icon,
            coordinate: group.coordinate
          )
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
        guard let groupID else {
          dismissSelectedPlace()
          return
        }
        guard let group = groups.first(where: { $0.id == groupID }) else { return }
        preferredDetailEntryID = nil
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
      .task(id: requestedEntryID) {
        guard let entryID = requestedEntryID,
              let group = groups.first(where: { group in
                group.places.contains { $0.id == entryID }
              })
        else { return }
        var focusedPlaces = group.places
        if let index = focusedPlaces.firstIndex(where: { $0.id == entryID }) {
          focusedPlaces.insert(focusedPlaces.remove(at: index), at: 0)
        }
        let focusedGroup = MappedPlaceGroup(id: group.id, places: focusedPlaces)
        hasChosenInitialCamera = true
        cameraPosition = .region(
          MKCoordinateRegion(
            center: focusedGroup.coordinate,
            latitudinalMeters: 1_500,
            longitudinalMeters: 1_500
          )
        )
        selectedGroupID = focusedGroup.id
        preferredDetailEntryID = entryID
        detailGroup = focusedGroup
        requestedEntryID = nil
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

          if let selectedType {
            Button {
              self.selectedType = nil
            } label: {
              Label(selectedType, systemImage: "xmark")
                .font(.subheadline.weight(.semibold))
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(.regularMaterial, in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Clear \(selectedType) filter")
          }

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
        clearSelectedPlace()
      }) { group in
        PlaceDetailSheet(
          group: group,
          initialEntryID: preferredDetailEntryID,
          appleMapsDestinations: appleMapsDestinations
        ) { entry in
          try await deleteEntryCard(entry)
        }
        .presentationDetents([.fraction(0.58), .large])
        .presentationDragIndicator(.visible)
        .presentationContentInteraction(.scrolls)
        .background(
          SheetWillDismissObserver {
            clearSelectedPlace()
          }
        )
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
    clearSelectedPlace()
    searchResult = item
    cameraPosition = .item(item, allowsAutomaticPitch: false)
  }

  private func clearSelectedPlace() {
    selectedGroupID = nil
    dismissSelectedPlace()
  }

  private func dismissSelectedPlace() {
    detailGroup = nil
    preferredDetailEntryID = nil
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

private struct SheetWillDismissObserver: UIViewControllerRepresentable {
  let action: () -> Void

  func makeUIViewController(context: Context) -> DismissObserverViewController {
    DismissObserverViewController(action: action)
  }

  func updateUIViewController(_ viewController: DismissObserverViewController, context: Context) {
    viewController.action = action
  }

  final class DismissObserverViewController: UIViewController {
    var action: () -> Void
    private var hasNotified = false

    init(action: @escaping () -> Void) {
      self.action = action
      super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
      fatalError("init(coder:) has not been implemented")
    }

    override func viewDidAppear(_ animated: Bool) {
      super.viewDidAppear(animated)
      hasNotified = false
    }

    override func viewWillDisappear(_ animated: Bool) {
      super.viewWillDisappear(animated)
      guard !hasNotified,
            isBeingDismissed || parent?.isBeingDismissed == true || navigationController?.isBeingDismissed == true
      else { return }
      hasNotified = true
      action()
    }
  }
}

private struct PlaceDetailSheet: View {
  let group: MappedPlaceGroup
  let deleteEntry: (SavedEntry) async throws -> Void
  @ObservedObject var appleMapsDestinations: AppleMapsDestinationCache
  @Environment(\.dismiss) private var dismiss
  @State private var entries: [SavedEntry]
  @State private var selectedEntryID: Int?
  @State private var pendingDeletion: SavedEntry?
  @State private var deletingEntryID: Int?
  @State private var deletionError: String?

  init(
    group: MappedPlaceGroup,
    initialEntryID: Int?,
    appleMapsDestinations: AppleMapsDestinationCache,
    deleteEntry: @escaping (SavedEntry) async throws -> Void
  ) {
    self.group = group
    self.deleteEntry = deleteEntry
    self.appleMapsDestinations = appleMapsDestinations
    _entries = State(initialValue: group.places)
    let requestedEntryExists = initialEntryID.map { requestedID in
      group.places.contains { $0.id == requestedID }
    } ?? false
    _selectedEntryID = State(
      initialValue: requestedEntryExists
        ? initialEntryID
        : group.places.count == 1 ? group.places.first?.id : nil
    )
  }

  private var selectedEntry: SavedEntry? {
    guard let selectedEntryID else { return nil }
    return entries.first { $0.id == selectedEntryID }
  }

  var body: some View {
    ScrollView {
      LazyVStack(alignment: .leading, spacing: 18) {
        if let selectedEntry {
          EntryDetailContent(
            entry: selectedEntry,
            backAction: entries.count > 1 ? { selectedEntryID = nil } : nil,
            isDeleting: deletingEntryID == selectedEntry.id,
            requestDeletion: { pendingDeletion = selectedEntry },
            appleMapsDestinations: appleMapsDestinations
          )
        } else if !entries.isEmpty {
          LocationEntryPicker(
            group: MappedPlaceGroup(id: group.id, places: entries),
            selectEntry: { selectedEntryID = $0.id },
            appleMapsDestinations: appleMapsDestinations
          )
        }
      }
      .padding(.horizontal)
      .padding(.top, 26)
      .padding(.bottom, 28)
    }
    .confirmationDialog(
      pendingDeletion.map { "Delete \($0.name)?" } ?? "Delete Entry?",
      isPresented: Binding(
        get: { pendingDeletion != nil },
        set: { if !$0 { pendingDeletion = nil } }
      ),
      titleVisibility: .visible
    ) {
      if let entry = pendingDeletion {
        Button("Delete Entry", role: .destructive) {
          pendingDeletion = nil
          Task { await performDeletion(entry) }
        }
      }
      Button("Cancel", role: .cancel) { pendingDeletion = nil }
    } message: {
      if let entry = pendingDeletion { Text(deleteMessage(entry: entry)) }
    }
    .alert(
      "Couldn’t Delete Entry",
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

  private func performDeletion(_ entry: SavedEntry) async {
    deletingEntryID = entry.id
    defer { deletingEntryID = nil }
    do {
      try await deleteEntry(entry)
      entries.removeAll { $0.id == entry.id }
      if entries.isEmpty {
        dismiss()
      } else if entries.count == 1 {
        selectedEntryID = entries[0].id
      } else {
        selectedEntryID = nil
      }
    } catch {
      deletionError = error.localizedDescription
    }
  }
}

private struct LocationEntryPicker: View {
  let group: MappedPlaceGroup
  let selectEntry: (SavedEntry) -> Void
  @ObservedObject var appleMapsDestinations: AppleMapsDestinationCache

  var body: some View {
    VStack(alignment: .leading, spacing: 16) {
      HStack(alignment: .top, spacing: 8) {
        VStack(alignment: .leading, spacing: 4) {
          Text("Location")
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
            .textCase(.uppercase)
          Text(group.name)
            .font(.title2.bold())
        }

        Spacer(minLength: 8)

        AppleMapsButton(entry: group.primary, destinations: appleMapsDestinations)

      }

      Text("\(group.entryGroups.count) saved entries at this location")
        .font(.subheadline)
        .foregroundStyle(.secondary)

      ForEach(group.entryGroups) { entryGroup in
        Button {
          selectEntry(entryGroup.primary)
        } label: {
          HStack(spacing: 12) {
            Image(systemName: SavedCategory.category(for: entryGroup.type).icon)
              .font(.headline)
              .foregroundStyle(.indigo)
              .frame(width: 42, height: 42)
              .background(.indigo.opacity(0.12), in: RoundedRectangle(cornerRadius: 11))

            VStack(alignment: .leading, spacing: 3) {
              Text(entryGroup.name)
                .font(.headline)
                .foregroundStyle(.primary)
                .multilineTextAlignment(.leading)
              Text(entryGroup.type)
                .font(.caption)
                .foregroundStyle(.secondary)
            }

            Spacer(minLength: 8)
            Image(systemName: "chevron.right")
              .font(.caption.weight(.semibold))
              .foregroundStyle(.tertiary)
          }
          .padding(13)
          .contentShape(Rectangle())
          .background(.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
        }
        .buttonStyle(.plain)
      }
    }
  }
}

private struct AppleMapsButton: View {
  let entry: SavedEntry
  @ObservedObject var destinations: AppleMapsDestinationCache
  @Environment(\.openURL) private var openURL
  @State private var isOpening = false

  var body: some View {
    if entry.appleMapsFallbackURL != nil {
      Button(action: openMaps) {
        Label(isOpening ? "Opening Maps" : "Maps", systemImage: "arrow.up.right")
      }
      .buttonStyle(.bordered)
      .controlSize(.small)
      .disabled(isOpening)
      // Let the detail sheet finish its presentation before MapKit does any
      // lookup setup on the UI actor. A Maps tap can still start or join this
      // same cache entry immediately.
      .task(id: entry.id, priority: .utility) {
        do {
          try await Task.sleep(nanoseconds: 350_000_000)
        } catch {
          return
        }
        guard !Task.isCancelled else { return }
        await destinations.prefetch(entry)
      }
      .onDisappear {
        if !isOpening {
          destinations.cancelPrefetch(for: entry)
        }
      }
    }
  }

  private func openMaps() {
    guard !isOpening else { return }
    isOpening = true
    Task {
      if let mapItem = await destinations.mapItem(for: entry) {
        mapItem.openInMaps(launchOptions: nil)
      } else if let fallbackURL = entry.appleMapsFallbackURL {
        openURL(fallbackURL)
      }
      isOpening = false
    }
  }
}

private struct EntryDetailContent: View {
  let entry: SavedEntry
  let backAction: (() -> Void)?
  let isDeleting: Bool
  let requestDeletion: () -> Void
  @ObservedObject var appleMapsDestinations: AppleMapsDestinationCache

  var body: some View {
    VStack(alignment: .leading, spacing: 16) {
      HStack(alignment: .top, spacing: 8) {
        if let backAction {
          Button("Back", systemImage: "chevron.left") { backAction() }
            .labelStyle(.iconOnly)
            .buttonStyle(.plain)
            .frame(width: 32, height: 32)
        }

        VStack(alignment: .leading, spacing: 4) {
          Text(entry.name)
            .font(.title2.bold())

          HStack(spacing: 10) {
            Text(entry.displayType)
              .font(.subheadline)
              .foregroundStyle(.secondary)

            AppleMapsButton(entry: entry, destinations: appleMapsDestinations)
          }

          if let availability = entry.availabilityText {
            Label(availability, systemImage: "calendar")
              .font(.caption)
              .foregroundStyle(.secondary)
          }
        }

        Spacer(minLength: 6)

        if isDeleting {
          ProgressView()
            .controlSize(.small)
            .frame(width: 32, height: 32)
        } else {
          Menu {
            Button("Delete Entry", systemImage: "trash", role: .destructive) {
              requestDeletion()
            }
          } label: {
            Image(systemName: "ellipsis")
              .foregroundStyle(.secondary)
              .frame(width: 32, height: 32)
              .contentShape(Rectangle())
          }
          .buttonStyle(.plain)
          .accessibilityLabel("More Actions")
        }
      }

      ForEach(entry.sources) { source in
        EntrySourceCard(source: source)
      }
    }
  }
}

private struct EntrySourceCard: View {
  let source: SavedEntrySource

  private var description: String {
    let detailed = source.description.trimmingCharacters(in: .whitespacesAndNewlines)
    if !detailed.isEmpty { return detailed }
    return source.whyItsCool.trimmingCharacters(in: .whitespacesAndNewlines)
  }

  private var platformName: String {
    let platform = source.sourcePlatform.trimmingCharacters(in: .whitespacesAndNewlines)
    return platform.isEmpty ? "Original post" : platform.capitalized
  }

  private var brandAssetName: String? {
    let platform = source.sourcePlatform.lowercased()
    let host = source.sourceURL.host?.lowercased() ?? ""
    if platform.contains("instagram") || host.contains("instagram") {
      return "InstagramBrandIcon"
    }
    if platform.contains("youtube") || host.contains("youtube.com") || host.contains("youtu.be") {
      return "YouTubeBrandIcon"
    }
    return nil
  }

  var body: some View {
    Link(destination: source.linkedSourceURL) {
      VStack(alignment: .leading, spacing: 10) {
        HStack(spacing: 10) {
          if let brandAssetName {
            Image(brandAssetName)
              .resizable()
              .scaledToFit()
              .frame(width: 30, height: 30)
              .accessibilityHidden(true)
          } else {
            Image(systemName: source.sourceSystemImage)
              .font(.subheadline.weight(.semibold))
              .foregroundStyle(.white)
              .frame(width: 30, height: 30)
              .background(.blue, in: RoundedRectangle(cornerRadius: 8))
              .accessibilityHidden(true)
          }

          VStack(alignment: .leading, spacing: 2) {
            Text(source.sourceLinkText)
              .font(.subheadline.weight(.semibold))
            Text(platformName)
              .font(.caption)
              .foregroundStyle(.secondary)
          }

          Spacer(minLength: 8)

          Image(systemName: "arrow.up.right")
            .font(.caption.weight(.bold))
            .foregroundStyle(.blue)
        }

        if !description.isEmpty {
          Text(description)
            .font(.subheadline)
        }

        if let mediaReference = source.mediaReferenceText {
          Label(mediaReference, systemImage: source.mediaReferenceSystemImage)
            .font(.caption)
            .foregroundStyle(.secondary)
          }
      }
      .padding(14)
      .frame(maxWidth: .infinity, alignment: .leading)
      .contentShape(Rectangle())
    }
    .buttonStyle(.plain)
    .background(.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
    .clipShape(RoundedRectangle(cornerRadius: 14))
    .accessibilityHint("Opens the original post")
  }
}

private func deleteMessage(entry: SavedEntry) -> String {
  let references = entry.sources.count == 1
    ? "its saved reference"
    : "its \(entry.sources.count) saved references"
  return "This removes \(entry.name) and \(references). Original source posts stay saved."
}

private struct SavedItemView: View {
  let places: [SavedEntry]
  let isLoading: Bool
  let deleteEntry: (SavedEntry) async throws -> Void

  var body: some View {
    Group {
      if places.isEmpty && isLoading {
        ProgressView("Loading saved entry…")
      } else if places.isEmpty {
        ContentUnavailableView(
          "Saved Entry Not Found",
          systemImage: "tray",
          description: Text("Try returning to the list and refreshing.")
        )
      } else if places.count == 1, let entry = places.first {
        EntryDetailPage(entry: entry, deleteEntry: deleteEntry)
      } else {
        List(places) { entry in
          NavigationLink(value: PlacesNavigation.entry(entry.id)) {
            PlaceRow(place: entry)
          }
        }
        .listStyle(.plain)
      }
    }
    .navigationTitle(places.count == 1 ? "" : "Saved Entries")
    .navigationBarTitleDisplayMode(.inline)
  }
}

private struct EntryDetailPage: View {
  let entry: SavedEntry
  let deleteEntry: (SavedEntry) async throws -> Void
  @Environment(\.dismiss) private var dismiss
  @State private var isDeleting = false
  @State private var showDeleteConfirmation = false
  @State private var deletionError: String?
  @StateObject private var appleMapsDestinations = AppleMapsDestinationCache()

  var body: some View {
    ScrollView {
      EntryDetailContent(
        entry: entry,
        backAction: nil,
        isDeleting: isDeleting,
        requestDeletion: { showDeleteConfirmation = true },
        appleMapsDestinations: appleMapsDestinations
      )
      .padding(.horizontal)
      .padding(.top, 14)
      .padding(.bottom, 28)
    }
    .confirmationDialog(
      "Delete \(entry.name)?",
      isPresented: $showDeleteConfirmation,
      titleVisibility: .visible
    ) {
      Button("Delete Entry", role: .destructive) {
        Task { await performDeletion() }
      }
      Button("Cancel", role: .cancel) {}
    } message: {
      Text(deleteMessage(entry: entry))
    }
    .alert(
      "Couldn’t Delete Entry",
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

  private func performDeletion() async {
    isDeleting = true
    defer { isDeleting = false }
    do {
      try await deleteEntry(entry)
      dismiss()
    } catch {
      deletionError = error.localizedDescription
    }
  }
}

private struct PlaceRow: View {
  let place: SavedEntry

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
    }
    .padding(.vertical, 6)
  }
}
