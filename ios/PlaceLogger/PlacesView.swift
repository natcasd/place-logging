import Combine
import MapKit
import SwiftUI

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

  func deleteActivityEntry(id: Int) async throws {
    try await api.deleteEntry(id: id)
    places.removeAll { $0.id == id }
    activity = try await api.fetchActivity()
  }

  func confirmActivityLocation(
    ingestID: Int,
    entryID: Int,
    candidateID: String
  ) async throws {
    try await api.confirmActivityLocation(
      ingestID: ingestID,
      entryID: entryID,
      candidateID: candidateID
    )
    async let loadedEntries = api.fetchEntries()
    async let loadedActivity = api.fetchActivity()
    places = try await loadedEntries
    activity = try await loadedActivity
  }
}

struct PlacesView: View {
  @ObservedObject var router: PlaceLoggerRouter
  @StateObject private var model = PlacesModel()
  @Environment(\.scenePhase) private var scenePhase
  @State private var savedPath: [PlacesNavigation] = []
  @State private var activityPath: [PlacesNavigation] = []
  @State private var selectedTab: PlacesTab = .aroundMe
  @State private var requestedMapEntryID: Int?
  @State private var aroundMeFilterType: String?

  var body: some View {
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
            requestedEntryID: $requestedMapEntryID,
            selectedType: $aroundMeFilterType,
            deleteEntryCard: { entry in try await model.deleteEntryCard(entry) }
          )
            .tabItem {
              Label("Around Me", systemImage: "location")
            }
            .tag(PlacesTab.aroundMe)

          RootNavigationTab(
            path: $savedPath,
            title: "Saved",
            isRefreshing: model.isLoading,
            refresh: { await model.load() },
            content: {
              PlacesList(
                places: model.places,
                refresh: { await model.load() }
              )
            },
            destination: { navigationDestination(for: $0) }
          )
          .tabItem {
            Label("Saved", systemImage: "tray.full")
          }
          .tag(PlacesTab.saved)

          RootNavigationTab(
            path: $activityPath,
            title: "Activity",
            isRefreshing: model.isLoading,
            refresh: { await model.load() },
            content: {
              ActivityList(activity: model.activity)
            },
            destination: { navigationDestination(for: $0) }
          )
          .tabItem {
            Label("Activity", systemImage: "clock.arrow.circlepath")
          }
          .tag(PlacesTab.activity)
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
          savedPath = []
          activityPath = []
          selectedTab = .aroundMe
          aroundMeFilterType = nil
          requestedMapEntryID = entryID
        } else {
          activityPath = []
          selectedTab = .saved
          savedPath = [.entry(entryID)]
        }
      case .savedEntry(let entryID):
        activityPath = []
        selectedTab = .saved
        savedPath = [.entry(entryID)]
      case .activity(let ingestID):
        savedPath = []
        selectedTab = .activity
        activityPath = [.activity(ingestID)]
      case .legacyItem(let itemID):
        activityPath = []
        selectedTab = .saved
        savedPath = [.legacyItem(itemID)]
      }
      router.pendingDestination = nil
    }
    .onChange(of: scenePhase) { _, phase in
      guard phase == .active else { return }
      Task { await model.load() }
    }
  }

  @ViewBuilder
  private func navigationDestination(for destination: PlacesNavigation) -> some View {
    switch destination {
    case .entry(let entryID):
      SavedItemView(
        places: model.places.filter { $0.id == entryID },
        isLoading: model.isLoading,
        deleteEntry: { entry in try await model.deleteEntryCard(entry) }
      )
    case .activity(let ingestID):
      if let run = model.activity.first(where: { $0.id == ingestID }) {
        ActivityDetail(
          activity: run,
          deleteEntry: { entryID in
            try await model.deleteActivityEntry(id: entryID)
          },
          confirmLocation: { entryID, candidateID in
            try await model.confirmActivityLocation(
              ingestID: ingestID,
              entryID: entryID,
              candidateID: candidateID
            )
          }
        )
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
          savedPath = []
          selectedTab = .aroundMe
        }
      )
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

private struct RootNavigationTab<Content: View, Destination: View>: View {
  @Binding var path: [PlacesNavigation]
  let title: String
  let isRefreshing: Bool
  let refresh: () async -> Void
  let content: () -> Content
  let destination: (PlacesNavigation) -> Destination

  var body: some View {
    NavigationStack(path: $path) {
      content()
        .contentMargins(.top, 12, for: .scrollContent)
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.large)
        .toolbar {
          ToolbarItem(placement: .topBarTrailing) {
            if isRefreshing {
              ProgressView()
            } else {
              Button("Refresh", systemImage: "arrow.clockwise") {
                Task { await refresh() }
              }
            }
          }
        }
        .navigationDestination(for: PlacesNavigation.self) { route in
          destination(route)
            .toolbar(.hidden, for: .tabBar)
        }
    }
  }
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
      List {
        Section {
          ForEach(activity) { run in
            NavigationLink(value: PlacesNavigation.activity(run.id)) {
              HStack(alignment: .center, spacing: 12) {
                ActivitySourceIcon(activity: run)

                VStack(alignment: .leading, spacing: 4) {
                  Text(run.title)
                    .font(.headline)
                    .lineLimit(1)

                  HStack(spacing: 8) {
                    Text(run.recommendationCountText)
                      .font(.subheadline)
                      .foregroundStyle(.secondary)
                      .lineLimit(1)

                    Spacer(minLength: 8)

                    if run.needsReview {
                      HStack(spacing: 4) {
                        Image(systemName: "exclamationmark.circle.fill")
                        Text("Needs review")
                      }
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.yellow)
                        .lineLimit(1)
                    }
                  }
                }
              }
              .padding(.vertical, 7)
            }
          }
        }
        .listSectionSeparator(.hidden, edges: .top)
      }
      .listStyle(.plain)
    }
  }
}

private struct ActivitySourceIcon: View {
  let activity: IngestActivity

  private var brandAssetName: String? {
    let platform = activity.sourcePlatform.lowercased()
    let host = activity.sourceURL.host?.lowercased() ?? ""
    if platform.contains("instagram") || host.contains("instagram") {
      return "InstagramBrandIcon"
    }
    if platform.contains("youtube") || host.contains("youtube.com") || host.contains("youtu.be") {
      return "YouTubeBrandIcon"
    }
    return nil
  }

  private var fallbackSystemImage: String {
    let platform = activity.sourcePlatform.lowercased()
    let host = activity.sourceURL.host?.lowercased() ?? ""
    if platform.contains("tiktok") || host.contains("tiktok") { return "music.note" }
    return "link"
  }

  var body: some View {
    Group {
      if let brandAssetName {
        Image(brandAssetName)
          .resizable()
          .scaledToFit()
      } else {
        Image(systemName: fallbackSystemImage)
          .font(.subheadline.weight(.semibold))
          .foregroundStyle(.white)
          .frame(width: 32, height: 32)
          .background(.blue, in: RoundedRectangle(cornerRadius: 8))
      }
    }
    .frame(width: 32, height: 32)
    .accessibilityHidden(true)
  }
}

private struct ActivityDetail: View {
  @Environment(\.dismiss) private var dismiss
  let activity: IngestActivity
  let deleteEntry: (Int) async throws -> Void
  let confirmLocation: (Int, String) async throws -> Void
  @State private var pendingDeletion: SavedEntryOutcome?
  @State private var deletingEntryID: Int?
  @State private var actionError: String?

  private var sourceDescription: String? {
    let summary = activity.summary?.trimmingCharacters(in: .whitespacesAndNewlines)
    if let summary, !summary.isEmpty { return summary }
    let caption = activity.caption?.trimmingCharacters(in: .whitespacesAndNewlines)
    return caption.flatMap { $0.isEmpty ? nil : $0 }
  }

  private var sourceTitle: String? {
    guard let creator = activity.creator?.trimmingCharacters(in: .whitespacesAndNewlines),
          !creator.isEmpty else { return nil }
    let platform = activity.sourcePlatform.lowercased() == "youtube"
      ? "YouTube"
      : activity.sourcePlatform.capitalized
    return "\(creator) on \(platform)"
  }

  private var hasMappedLocations: Bool {
    activity.results.contains(where: \.hasLocation)
  }

  var body: some View {
    ZStack(alignment: .topLeading) {
      GeometryReader { geometry in
        ScrollViewReader { scrollProxy in
          ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
              if hasMappedLocations {
                ActivityLocationsMap(results: activity.results)
                  .frame(height: max(230, geometry.size.height * 0.29))
              }

              LazyVStack(alignment: .leading, spacing: 16) {
                SourceMetadataCard(
                  sourceURL: activity.sourceURL,
                  sourcePlatform: activity.sourcePlatform,
                  creator: activity.creator,
                  primaryText: sourceTitle,
                  detailText: sourceDescription,
                  mediaReferenceText: nil
                )

                if activity.status == "processing" {
                  ActivityStatePanel(
                    title: activity.statusText,
                    message: activity.events.last?.message,
                    systemImage: "arrow.triangle.2.circlepath",
                    color: .blue,
                    showsProgress: true
                  )
                } else if activity.status == "failed" {
                  ActivityStatePanel(
                    title: "Processing failed",
                    message: activity.errorMessage ?? activity.events.last?.message,
                    systemImage: "xmark.circle.fill",
                    color: .red,
                    showsProgress: false
                  )
                }

                if !activity.results.isEmpty {
                  Text("Recommendations")
                    .font(.title2.bold())
                    .padding(.top, 4)

                  ForEach(activity.results.sorted { $0.ordinal < $1.ordinal }) { result in
                    ActivityRecommendationCard(
                      result: result,
                      isDeleting: deletingEntryID == result.entryID,
                      requestDeletion: { pendingDeletion = result },
                      scrollToExpandedReview: {
                        withAnimation(.easeInOut(duration: 0.25)) {
                          scrollProxy.scrollTo(result.reviewPanelAnchorID, anchor: .center)
                        }
                      },
                      scrollToConfirmation: {
                        withAnimation(.easeInOut(duration: 0.25)) {
                          scrollProxy.scrollTo(result.confirmationAnchorID, anchor: .bottom)
                        }
                      },
                      confirmLocation: { candidateID in
                        try await confirmLocation(result.entryID, candidateID)
                      }
                    )
                  }
                } else if activity.status != "processing" && activity.status != "failed" {
                  ContentUnavailableView(
                    "No Recommendations Kept",
                    systemImage: "tray",
                    description: Text("The original source post is still saved in Activity.")
                  )
                  .frame(maxWidth: .infinity)
                  .padding(.vertical, 32)
                }
              }
              .padding(.horizontal)
              .padding(.top, 16)
              .padding(.bottom, 30)
            }
          }
        }
      }
      .ignoresSafeArea(edges: hasMappedLocations ? .top : [])

      if hasMappedLocations {
        Button {
          dismiss()
        } label: {
          Image(systemName: "chevron.left")
            .font(.headline.weight(.semibold))
            .frame(width: 42, height: 42)
            .background(.regularMaterial, in: Circle())
        }
        .buttonStyle(.plain)
        .padding(.top, 8)
        .padding(.leading, 12)
        .accessibilityLabel("Back")
      }
    }
    .navigationTitle("")
    .navigationBarTitleDisplayMode(.inline)
    .toolbar(hasMappedLocations ? .hidden : .visible, for: .navigationBar)
    .confirmationDialog(
      pendingDeletion.map { "Delete \($0.name)?" } ?? "Delete Recommendation?",
      isPresented: Binding(
        get: { pendingDeletion != nil },
        set: { if !$0 { pendingDeletion = nil } }
      ),
      titleVisibility: .visible
    ) {
      if let result = pendingDeletion {
        Button("Delete Recommendation", role: .destructive) {
          pendingDeletion = nil
          Task { await performDeletion(result) }
        }
      }
      Button("Cancel", role: .cancel) { pendingDeletion = nil }
    } message: {
      if let result = pendingDeletion {
        Text(activityDeleteMessage(result))
      }
    }
    .alert(
      "Couldn’t Complete Review",
      isPresented: Binding(
        get: { actionError != nil },
        set: { if !$0 { actionError = nil } }
      )
    ) {
      Button("OK", role: .cancel) { actionError = nil }
    } message: {
      Text(actionError ?? "Please try again.")
    }
  }

  private func performDeletion(_ result: SavedEntryOutcome) async {
    deletingEntryID = result.entryID
    defer { deletingEntryID = nil }
    do {
      try await deleteEntry(result.entryID)
    } catch {
      actionError = error.localizedDescription
    }
  }
}

private struct ActivityLocationsMap: View {
  let results: [SavedEntryOutcome]
  @State private var showsFullMap = false

  private var locatedResults: [SavedEntryOutcome] {
    results.filter(\.hasLocation)
  }

  var body: some View {
    ActivityResultsMap(results: locatedResults, allowsInteraction: false)
      .overlay(alignment: .bottomTrailing) {
        Image(systemName: "arrow.up.left.and.arrow.down.right")
          .font(.subheadline.weight(.semibold))
          .padding(10)
          .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 11))
          .padding(10)
      }
      .contentShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
      .onTapGesture { showsFullMap = true }
      .accessibilityAddTraits(.isButton)
      .accessibilityLabel("Show \(locatedResults.count) locations on full-screen map")
      .fullScreenCover(isPresented: $showsFullMap) {
        NavigationStack {
          ActivityResultsMap(results: locatedResults, allowsInteraction: true)
            .ignoresSafeArea(edges: .bottom)
            .navigationTitle("Locations from this post")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
              ToolbarItem(placement: .topBarLeading) {
                Button("Back", systemImage: "chevron.left") {
                  showsFullMap = false
                }
              }
            }
        }
      }
  }
}

private struct ActivityResultsMap: View {
  let results: [SavedEntryOutcome]
  let allowsInteraction: Bool

  var body: some View {
    Map(initialPosition: .automatic, interactionModes: allowsInteraction ? .all : []) {
      ForEach(results) { result in
        if let latitude = result.latitude, let longitude = result.longitude {
          Marker(
            result.locationName ?? result.name,
            systemImage: SavedCategory.category(for: result.type).icon,
            coordinate: CLLocationCoordinate2D(
              latitude: latitude,
              longitude: longitude
            )
          )
          .tint(SavedCategory.category(for: result.type).artTint)
        }
      }
    }
  }
}

private struct ActivityStatePanel: View {
  let title: String
  let message: String?
  let systemImage: String
  let color: Color
  let showsProgress: Bool

  var body: some View {
    HStack(alignment: .top, spacing: 12) {
      if showsProgress {
        ProgressView()
          .tint(color)
      } else {
        Image(systemName: systemImage)
          .foregroundStyle(color)
      }
      VStack(alignment: .leading, spacing: 4) {
        Text(title)
          .font(.headline)
        if let message, !message.isEmpty {
          Text(message)
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
      }
    }
    .padding(14)
    .frame(maxWidth: .infinity, alignment: .leading)
    .background(color.opacity(0.1), in: RoundedRectangle(cornerRadius: 14))
  }
}

private struct ActivityRecommendationCard: View {
  let result: SavedEntryOutcome
  let isDeleting: Bool
  let requestDeletion: () -> Void
  let scrollToExpandedReview: () -> Void
  let scrollToConfirmation: () -> Void
  let confirmLocation: (String) async throws -> Void
  @State private var isExpanded = false
  @State private var selectedCandidateID: String?
  @State private var isConfirming = false
  @State private var confirmationError: String?

  private var canReviewCandidates: Bool {
    result.resolutionStatus == "needs_review" && result.reviewCandidates.count > 1
  }

  private var showsMissingLocation: Bool {
    result.resolutionStatus == "unresolved"
      || (result.resolutionStatus == "needs_review" && !canReviewCandidates)
  }

  var body: some View {
    VStack(spacing: 0) {
      HStack(alignment: .center, spacing: 12) {
        VStack(alignment: .leading, spacing: 5) {
          HStack(spacing: 5) {
            Text(result.type)
            if let mediaReference = result.mediaReferenceText {
              Text("·")
              Text(mediaReference)
            }
          }
          .font(.caption2.weight(.semibold))
          .foregroundStyle(.secondary)
          .textCase(.uppercase)

          Text(result.name)
            .font(.headline)

          let description = result.description.trimmingCharacters(in: .whitespacesAndNewlines)
          if !description.isEmpty {
            Text(description)
              .font(.subheadline)
              .foregroundStyle(.secondary)
              .fixedSize(horizontal: false, vertical: true)
          }

          if let address = result.formattedAddress, !address.isEmpty {
            Text(compactActivityLocation(address))
              .font(.subheadline)
              .foregroundStyle(.secondary)
          } else if showsMissingLocation {
            Label("No location matched", systemImage: "exclamationmark.triangle.fill")
              .font(.subheadline.weight(.semibold))
              .foregroundStyle(.yellow)
          }
        }

        Spacer(minLength: 6)

        if isDeleting || isConfirming {
          ProgressView()
            .controlSize(.small)
            .frame(width: 34, height: 34)
        } else if canReviewCandidates {
          HStack(spacing: 5) {
            Text("Needs review")
            Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
              .font(.caption2.weight(.bold))
          }
            .font(.caption.weight(.semibold))
            .foregroundStyle(.yellow)
        } else {
          Button("Delete Recommendation", systemImage: "trash", role: .destructive) {
            requestDeletion()
          }
          .labelStyle(.iconOnly)
          .buttonStyle(.plain)
          .frame(width: 34, height: 34)
        }
      }
      .padding(14)
      .contentShape(Rectangle())
      .onTapGesture {
        toggleReview()
      }
      .accessibilityAction(named: isExpanded ? "Collapse locations" : "Review locations") {
        toggleReview()
      }

      if canReviewCandidates && isExpanded {
        Divider()
        VStack(alignment: .leading, spacing: 12) {
          HStack {
            Text("Select the location")
              .font(.headline)

            Spacer()

            Button("Delete Recommendation", systemImage: "trash", role: .destructive) {
              requestDeletion()
            }
            .labelStyle(.iconOnly)
            .buttonStyle(.plain)
            .frame(width: 34, height: 34)
          }

          VStack(spacing: 0) {
            ForEach(Array(result.reviewCandidates.enumerated()), id: \.element.id) { index, candidate in
              Button {
                selectedCandidateID = candidate.id
              } label: {
                HStack(spacing: 11) {
                  Image(
                    systemName: selectedCandidateID == candidate.id
                      ? "checkmark.circle.fill"
                      : "circle"
                  )
                  .font(.title3)
                  .foregroundStyle(selectedCandidateID == candidate.id ? .blue : .secondary)

                  VStack(alignment: .leading, spacing: 3) {
                    Text(candidate.name)
                      .font(.subheadline.weight(.semibold))
                      .foregroundStyle(.primary)
                    if let address = candidate.formattedAddress, !address.isEmpty {
                      Text(compactActivityLocation(address))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                  }
                  Spacer()
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 11)
                .contentShape(Rectangle())
              }
              .buttonStyle(.plain)

              if index < result.reviewCandidates.count - 1 {
                Divider().padding(.leading, 43)
              }
            }
          }
          .background(.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 12))

          if selectedCandidateID != nil {
            Button("Confirm Location", systemImage: "checkmark") {
              Task { await performConfirmation() }
            }
            .buttonStyle(.borderedProminent)
            .frame(maxWidth: .infinity)
            .id(result.confirmationAnchorID)
          }
        }
        .padding(14)
        .id(result.reviewPanelAnchorID)
        .transition(.opacity)
      }
    }
    .background(.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 16))
    .clipShape(RoundedRectangle(cornerRadius: 16))
    .alert(
      "Couldn’t Confirm Location",
      isPresented: Binding(
        get: { confirmationError != nil },
        set: { if !$0 { confirmationError = nil } }
      )
    ) {
      Button("OK", role: .cancel) { confirmationError = nil }
    } message: {
      Text(confirmationError ?? "Please try again.")
    }
    .onChange(of: selectedCandidateID) { _, candidateID in
      guard candidateID != nil else { return }
      DispatchQueue.main.async { scrollToConfirmation() }
    }
  }

  private func toggleReview() {
    guard canReviewCandidates, !isDeleting, !isConfirming else { return }
    let willExpand = !isExpanded
    withAnimation(.easeInOut(duration: 0.2)) { isExpanded = willExpand }
    if willExpand {
      DispatchQueue.main.async { scrollToExpandedReview() }
    }
  }

  private func performConfirmation() async {
    guard let selectedCandidateID else { return }
    isConfirming = true
    defer { isConfirming = false }
    do {
      try await confirmLocation(selectedCandidateID)
    } catch {
      confirmationError = error.localizedDescription
    }
  }
}

private func activityDeleteMessage(_ result: SavedEntryOutcome) -> String {
  let references = result.sourceCount == 1
    ? "its saved reference"
    : "its \(result.sourceCount) saved references"
  return "This removes \(result.name) and \(references). Original source posts stay saved."
}

private func compactActivityLocation(_ formattedAddress: String) -> String {
  let components = formattedAddress
    .split(separator: ",")
    .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
    .filter { !$0.isEmpty }

  guard components.count > 2 else { return formattedAddress }

  let componentCount = components.count >= 4 ? 3 : 2
  let compactComponents = components.suffix(componentCount).map { component in
    let words = component.split(separator: " ")
    guard let postalCodeStart = words.firstIndex(where: { word in
      word.contains(where: \Character.isNumber)
    }) else { return component }
    return words[..<postalCodeStart].joined(separator: " ")
  }
  .filter { !$0.isEmpty }

  return compactComponents.isEmpty
    ? formattedAddress
    : compactComponents.joined(separator: ", ")
}

private extension SavedEntryOutcome {
  var reviewPanelAnchorID: String {
    "activity-review-panel-\(entryID)"
  }

  var confirmationAnchorID: String {
    "activity-confirmation-\(entryID)"
  }
}

private extension IngestActivity {
  var recommendationCountText: String {
    "\(results.count) rec\(results.count == 1 ? "" : "s")"
  }

  var needsReview: Bool {
    status == "partial"
  }
}

private struct PlacesMap: View {
  let places: [SavedEntry]
  @Binding var requestedEntryID: Int?
  @Binding var selectedType: String?
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
  @State private var shouldCenterOnNextLocation = false
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
        MapCompass()
      }
      .onMapCameraChange(frequency: .onEnd) { context in
        visibleRegion = context.region
        if cameraPosition.positionedByUser {
          hasChosenInitialCamera = true
        }
      }
      .onChange(of: selectedGroupID) { _, groupID in
        traceMapDetailTiming("map selection -> \(groupID ?? "nil")")
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
        guard shouldCenterOnNextLocation || !hasChosenInitialCamera else { return }
        shouldCenterOnNextLocation = false
        hasChosenInitialCamera = true
        centerMap(on: location)
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

            Button("Current Location", systemImage: "location.fill") {
              recenterOnCurrentLocation()
            }
            .labelStyle(.iconOnly)
            .buttonStyle(.plain)
            .font(.headline)
            .frame(width: 46, height: 46)
            .background(.regularMaterial, in: Circle())
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
      .sheet(item: detailSheetBinding, onDismiss: {
        traceMapDetailTiming("sheet onDismiss")
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
        .onAppear {
          traceMapDetailTiming("sheet content onAppear")
        }
        .onDisappear {
          traceMapDetailTiming("sheet content onDisappear")
        }
      }
    }
  }

  private func recenterOnCurrentLocation() {
    shouldCenterOnNextLocation = true
    if let location = locationModel.location {
      hasChosenInitialCamera = true
      centerMap(on: location)
    }
    locationModel.requestCurrentLocation()
  }

  private func centerMap(on location: CLLocation) {
    withAnimation(.easeInOut(duration: 0.25)) {
      cameraPosition = .camera(
        MapCamera(
          centerCoordinate: location.coordinate,
          distance: 4_000,
          heading: 0,
          pitch: 0
        )
      )
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

  private var detailSheetBinding: Binding<MappedPlaceGroup?> {
    Binding(
      get: { detailGroup },
      set: { group in
        traceMapDetailTiming("sheet binding -> \(group?.id ?? "nil")")
        detailGroup = group
        if group == nil {
          clearSelectedPlace()
        }
      }
    )
  }

  private func clearSelectedPlace() {
    traceMapDetailTiming("clearSelectedPlace")
    selectedGroupID = nil
    dismissSelectedPlace()
  }

  private func dismissSelectedPlace() {
    traceMapDetailTiming("dismissSelectedPlace")
    detailGroup = nil
    preferredDetailEntryID = nil
  }

  private func traceMapDetailTiming(_ event: String) {
    let line = "[MapDetailTiming] \(ProcessInfo.processInfo.systemUptime) \(event)\n"
    print(line, terminator: "")

    guard let data = line.data(using: .utf8),
          let documentsURL = FileManager.default.urls(
            for: .documentDirectory,
            in: .userDomainMask
          ).first
    else { return }

    let logURL = documentsURL.appendingPathComponent("map-detail-timing.log")
    if let handle = try? FileHandle(forWritingTo: logURL) {
      defer { try? handle.close() }
      try? handle.seekToEnd()
      try? handle.write(contentsOf: data)
    } else {
      try? data.write(to: logURL, options: .atomic)
    }
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
        ZStack {
          HStack(spacing: 5) {
            Image(systemName: "map")
            Image(systemName: "arrow.up.right")
              .font(.caption.weight(.bold))
          }
          .opacity(isOpening ? 0 : 1)

          if isOpening {
            ProgressView()
              .controlSize(.mini)
              .tint(.secondary)
          }
        }
        .font(.subheadline.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 12)
        .frame(height: 36)
        .background(.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 11))
      }
      .buttonStyle(.plain)
      .disabled(isOpening)
      .accessibilityLabel(isOpening ? "Opening Apple Maps" : "Open in Apple Maps")
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

private struct SourceMetadataCard: View {
  let sourceURL: URL
  let sourcePlatform: String
  let creator: String?
  let primaryText: String?
  let detailText: String?
  let mediaReferenceText: String?

  private var platformName: String {
    let platform = sourcePlatform.trimmingCharacters(in: .whitespacesAndNewlines)
    if platform.caseInsensitiveCompare("youtube") == .orderedSame { return "YouTube" }
    if platform.caseInsensitiveCompare("instagram") == .orderedSame { return "Instagram" }
    return platform.isEmpty ? "Original post" : platform.capitalized
  }

  private var displayTitle: String {
    if let primaryText, !primaryText.isEmpty { return primaryText }
    if let creator, !creator.isEmpty { return creator }
    return "\(platformName) post"
  }

  private var brandAssetName: String? {
    let platform = sourcePlatform.lowercased()
    let host = sourceURL.host?.lowercased() ?? ""
    if platform.contains("instagram") || host.contains("instagram") {
      return "InstagramBrandIcon"
    }
    if platform.contains("youtube") || host.contains("youtube.com") || host.contains("youtu.be") {
      return "YouTubeBrandIcon"
    }
    return nil
  }

  private var fallbackSystemImage: String {
    let host = sourceURL.host?.lowercased() ?? ""
    if host.contains("instagram") { return "camera" }
    if host.contains("youtube.com") || host.contains("youtu.be") {
      return "play.rectangle.fill"
    }
    if host.contains("tiktok") { return "music.note" }
    return "link"
  }

  var body: some View {
    VStack(alignment: .leading, spacing: 10) {
      Link(destination: sourceURL) {
        HStack(spacing: 10) {
          if let brandAssetName {
            Image(brandAssetName)
              .resizable()
              .scaledToFit()
              .frame(width: 30, height: 30)
              .accessibilityHidden(true)
          } else {
            Image(systemName: fallbackSystemImage)
              .font(.subheadline.weight(.semibold))
              .foregroundStyle(.white)
              .frame(width: 30, height: 30)
              .background(.blue, in: RoundedRectangle(cornerRadius: 8))
              .accessibilityHidden(true)
          }

          Text(displayTitle)
            .font(.headline)

          Spacer(minLength: 8)

          Image(systemName: "arrow.up.right")
            .font(.caption.weight(.bold))
            .foregroundStyle(.blue)
        }
        .contentShape(Rectangle())
      }
      .buttonStyle(.plain)

      if let mediaReferenceText {
        Link(destination: sourceURL) {
          Label(mediaReferenceText, systemImage: "play.rectangle")
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .buttonStyle(.plain)
      }

      if let detailText, !detailText.isEmpty {
        ExpandableDetailText(detailText)
      }
    }
    .padding(14)
    .frame(maxWidth: .infinity, alignment: .leading)
    .background(.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
    .clipShape(RoundedRectangle(cornerRadius: 14))
    .accessibilityHint("Opens the original post")
  }
}

private struct CollapsedDetailHeightKey: PreferenceKey {
  static let defaultValue: CGFloat = 0

  static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
    value = max(value, nextValue())
  }
}

private struct FullDetailHeightKey: PreferenceKey {
  static let defaultValue: CGFloat = 0

  static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
    value = max(value, nextValue())
  }
}

private struct ExpandableDetailText: View {
  let text: String
  @State private var isExpanded = false
  @State private var collapsedHeight: CGFloat = 0
  @State private var fullHeight: CGFloat = 0

  init(_ text: String) {
    self.text = text
  }

  private var isTruncated: Bool {
    fullHeight > collapsedHeight + 0.5
  }

  var body: some View {
    VStack(alignment: .leading, spacing: 6) {
      Text(text)
        .font(.subheadline)
        .lineLimit(isExpanded ? nil : 3)
        .animation(.easeInOut(duration: 0.2), value: isExpanded)
        .background {
          measurementText(lineLimit: 3)
            .hidden()
            .background {
              GeometryReader { geometry in
                Color.clear.preference(
                  key: CollapsedDetailHeightKey.self,
                  value: geometry.size.height
                )
              }
            }
        }
        .background {
          measurementText(lineLimit: nil)
            .hidden()
            .background {
              GeometryReader { geometry in
                Color.clear.preference(
                  key: FullDetailHeightKey.self,
                  value: geometry.size.height
                )
              }
            }
        }

      if isTruncated {
        Button(isExpanded ? "Less" : "More") {
          withAnimation(.easeInOut(duration: 0.2)) {
            isExpanded.toggle()
          }
        }
        .font(.caption.weight(.semibold))
        .buttonStyle(.plain)
        .foregroundStyle(.blue)
      }
    }
    .onPreferenceChange(CollapsedDetailHeightKey.self) { collapsedHeight = $0 }
    .onPreferenceChange(FullDetailHeightKey.self) { fullHeight = $0 }
    .onChange(of: text) { _, _ in isExpanded = false }
  }

  private func measurementText(lineLimit: Int?) -> some View {
    Text(text)
      .font(.subheadline)
      .lineLimit(lineLimit)
      .fixedSize(horizontal: false, vertical: true)
  }
}

private struct EntrySourceCard: View {
  let source: SavedEntrySource

  private var description: String {
    let detailed = source.description.trimmingCharacters(in: .whitespacesAndNewlines)
    if !detailed.isEmpty { return detailed }
    return source.whyItsCool.trimmingCharacters(in: .whitespacesAndNewlines)
  }

  var body: some View {
    SourceMetadataCard(
      sourceURL: source.linkedSourceURL,
      sourcePlatform: source.sourcePlatform,
      creator: source.creator,
      primaryText: source.sourceLinkText,
      detailText: description,
      mediaReferenceText: source.mediaReferenceText
    )
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
