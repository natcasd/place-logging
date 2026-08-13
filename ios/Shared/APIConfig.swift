import Foundation

enum APIConfig {
  static var baseURL: URL {
    guard
      let value = Bundle.main.object(forInfoDictionaryKey: "APIBaseURL") as? String,
      let url = URL(string: value)
    else {
      fatalError("APIBaseURL is missing from Info.plist")
    }
    return url
  }

  static var token: String {
    (Bundle.main.object(forInfoDictionaryKey: "APIToken") as? String) ?? ""
  }
}
