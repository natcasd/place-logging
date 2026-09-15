import SwiftUI

enum SavedCategoryIcon: Hashable {
  case system(String)
  case asset(String)
}

struct SavedCategoryIconView: View {
  let icon: SavedCategoryIcon

  var body: some View {
    switch icon {
    case .system(let name):
      Image(systemName: name)
    case .asset(let name):
      Image(name)
        .resizable()
        .scaledToFit()
    }
  }
}

struct SavedCategory: Identifiable, Hashable {
  let type: String
  let icon: SavedCategoryIcon
  let artAssetName: String?
  let artTint: Color

  var id: String { type }
  var title: String { type }

  static func category(for type: String) -> SavedCategory {
    known.first { $0.type.caseInsensitiveCompare(type) == .orderedSame }
      ?? SavedCategory(
        type: type,
        icon: .system("questionmark.circle.fill"),
        artAssetName: "category-unknown",
        artTint: .gray
      )
  }

  static func categories(for entries: [SavedEntry]) -> [SavedCategory] {
    let presentTypes = Set(entries.map(\.displayType))
    return presentTypes
      .map(category(for:))
      .sorted { lhs, rhs in
        guard let left = known.firstIndex(of: lhs), let right = known.firstIndex(of: rhs) else {
          return lhs.title.localizedCaseInsensitiveCompare(rhs.title) == .orderedAscending
        }
        return left < right
      }
  }
}
