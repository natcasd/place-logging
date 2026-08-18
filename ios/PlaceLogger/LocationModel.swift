import CoreLocation

@MainActor
final class LocationModel: NSObject, ObservableObject {
  @Published private(set) var location: CLLocation?
  @Published private(set) var authorizationStatus: CLAuthorizationStatus

  private let manager: CLLocationManager

  override init() {
    let manager = CLLocationManager()
    self.manager = manager
    authorizationStatus = manager.authorizationStatus
    super.init()
    manager.delegate = self
    manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
  }

  func requestCurrentLocation() {
    switch authorizationStatus {
    case .notDetermined:
      manager.requestWhenInUseAuthorization()
    case .authorizedAlways, .authorizedWhenInUse:
      manager.requestLocation()
    case .denied, .restricted:
      break
    @unknown default:
      break
    }
  }
}

extension LocationModel: @preconcurrency CLLocationManagerDelegate {
  func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
    authorizationStatus = manager.authorizationStatus
    if authorizationStatus == .authorizedAlways || authorizationStatus == .authorizedWhenInUse {
      manager.requestLocation()
    }
  }

  func locationManager(
    _ manager: CLLocationManager,
    didUpdateLocations locations: [CLLocation]
  ) {
    location = locations.last
  }

  func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
    // The map's automatic framing remains the fallback when location is unavailable.
  }
}
