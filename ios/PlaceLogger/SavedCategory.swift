import SwiftUI

struct SavedCategory: Identifiable, Hashable {
  let type: String
  let title: String
  let icon: String
  let artAssetName: String?
  let isPlaceBased: Bool

  var id: String { type }

  var artTint: Color {
    switch type {
    case "Restaurant", "Pop-up": return .pink
    case "Café", "Bakery": return .orange
    case "Bar", "Concert", "Song": return .purple
    case "Park", "Hiking Trail": return .green
    case "Bike Route", "Fitness": return .blue
    case "Museum", "Art Gallery", "Exhibit": return .indigo
    case "Store", "Product": return .teal
    case "Spa": return .mint
    case "Book", "Article", "Movie": return .red
    default: return .gray
    }
  }

  static func category(for type: String) -> SavedCategory {
    known.first { $0.type.caseInsensitiveCompare(type) == .orderedSame }
      ?? SavedCategory(
        type: type,
        title: type,
        icon: "questionmark.circle",
        artAssetName: "category-unknown",
        isPlaceBased: false
      )
  }

  static func categories(for things: [SavedPlace]) -> [SavedCategory] {
    let presentTypes = Set(things.map(\.displayType))
    return presentTypes
      .map(category(for:))
      .sorted { lhs, rhs in
        guard let left = known.firstIndex(of: lhs), let right = known.firstIndex(of: rhs) else {
          return lhs.title.localizedCaseInsensitiveCompare(rhs.title) == .orderedAscending
        }
        return left < right
      }
  }

  private static let known: [SavedCategory] = [
    .init(type: "Restaurant", title: "Restaurants", icon: "fork.knife", artAssetName: "category-restaurant", isPlaceBased: true),
    .init(type: "Café", title: "Cafés", icon: "cup.and.saucer.fill", artAssetName: "category-cafe", isPlaceBased: true),
    .init(type: "Bar", title: "Bars", icon: "wineglass.fill", artAssetName: "category-bar", isPlaceBased: true),
    .init(type: "Bakery", title: "Bakeries", icon: "birthday.cake.fill", artAssetName: "category-bakery", isPlaceBased: true),
    .init(type: "Park", title: "Parks", icon: "tree.fill", artAssetName: "category-park", isPlaceBased: true),
    .init(type: "Hiking Trail", title: "Hiking Trails", icon: "figure.hiking", artAssetName: "category-hiking-trail", isPlaceBased: true),
    .init(type: "Bike Route", title: "Bike Routes", icon: "bicycle", artAssetName: "category-bike-route", isPlaceBased: true),
    .init(type: "Museum", title: "Museums", icon: "building.columns.fill", artAssetName: "category-museum", isPlaceBased: true),
    .init(type: "Art Gallery", title: "Art Galleries", icon: "photo.artframe", artAssetName: "category-art-gallery", isPlaceBased: true),
    .init(type: "Store", title: "Stores", icon: "bag.fill", artAssetName: "category-store", isPlaceBased: true),
    .init(type: "Spa", title: "Spas", icon: "sparkles", artAssetName: "category-spa", isPlaceBased: true),
    .init(type: "Fitness", title: "Fitness", icon: "dumbbell.fill", artAssetName: "category-fitness", isPlaceBased: true),
    .init(type: "Concert", title: "Concerts", icon: "music.mic", artAssetName: "category-concert", isPlaceBased: true),
    .init(type: "Pop-up", title: "Pop-ups", icon: "tent.fill", artAssetName: "category-popup", isPlaceBased: true),
    .init(type: "Exhibit", title: "Exhibits", icon: "rectangle.3.group.fill", artAssetName: "category-exhibit", isPlaceBased: true),
    .init(type: "Book", title: "Books", icon: "book.fill", artAssetName: "category-book", isPlaceBased: false),
    .init(type: "Movie", title: "Movies", icon: "film.fill", artAssetName: "category-movie", isPlaceBased: false),
    .init(type: "Article", title: "Articles", icon: "newspaper.fill", artAssetName: "category-article", isPlaceBased: false),
    .init(type: "Song", title: "Songs", icon: "music.note", artAssetName: "category-song", isPlaceBased: false),
    .init(type: "Product", title: "Products", icon: "shippingbox.fill", artAssetName: "category-product", isPlaceBased: false),
    .init(type: "Unknown", title: "Unknown", icon: "questionmark.circle.fill", artAssetName: "category-unknown", isPlaceBased: false),
  ]
}
