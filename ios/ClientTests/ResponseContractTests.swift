import Foundation
import XCTest
@testable import JotClientCore

final class ResponseContractTests: XCTestCase {
  func testSaveNotificationsWaitForThisOperationsActualOutcome() throws {
    for status in ["queued", "processing"] {
      let result = try JSONDecoder().decode(IngestResponse.self, from: Data("{\"ingest_id\":42,\"status\":\"\(status)\",\"saved_entries\":[]}".utf8))
      XCTAssertFalse(result.hasNotificationOutcome)
    }
    let result = try JSONDecoder().decode(IngestResponse.self, from: Data(#"{"ingest_id":42,"item_id":7,"status":"completed","saved_entries":[{"entry_id":8,"name":"Cafe","type":"Restaurant","resolution_status":"resolved","is_new":true,"source_count":1}]}"#.utf8))
    XCTAssertTrue(result.hasNotificationOutcome)
    XCTAssertEqual(result.notificationTitle, "Logged Restaurant · Cafe")

  }

  func testFailureNotificationUsesTheActualProcessingError() throws {
    let result = try JSONDecoder().decode(IngestResponse.self, from: Data(#"{"ingest_id":42,"status":"failed","saved_entries":[],"failure_kind":"media_fetch_failed","error_message":"This post could not be downloaded."}"#.utf8))
    XCTAssertEqual(result.notificationTitle, "Download failed")
    XCTAssertEqual(result.notificationBody, "This post could not be downloaded.")
  }

  func testPrivateRecommendationCanHaveNoSocialURL() throws {
    let data = Data(#"{"id":1,"item_id":2,"source_url":null,"source_platform":"other","description":"Try this cafe","why_its_cool":"Personal note"}"#.utf8)
    let source = try JSONDecoder().decode(SavedEntrySource.self, from: data)
    XCTAssertNil(source.sourceURL)
    XCTAssertNil(source.linkedSourceURL)
    XCTAssertEqual(source.sourceLinkText, "Your recommendation")
  }

  func testPrivateActivityAndQueuedAcceptanceDecodeWithoutInventedPost() throws {
    let data = Data(#"{"id":4,"item_id":2,"source_url":null,"source_platform":"other","status":"queued","stage":"accepted","results":[],"events":[]}"#.utf8)
    let activity = try JSONDecoder().decode(IngestActivity.self, from: data)
    XCTAssertNil(activity.sourceURL)
    XCTAssertEqual(activity.statusText, "Waiting to process")
  }

  func testTwoMentionsOfSameRecommendationHaveDistinctUIIdentities() throws {
    let data = Data(#"[{"entry_id":1,"source_connection_id":10,"name":"Cafe","type":"Restaurant","resolution_status":"resolved","is_new":false,"source_count":1},{"entry_id":1,"source_connection_id":11,"name":"Cafe","type":"Restaurant","resolution_status":"resolved","is_new":false,"source_count":1}]"#.utf8)
    let results = try JSONDecoder().decode([SavedEntryOutcome].self, from: data)
    XCTAssertEqual(results[0].entryID, results[1].entryID)
    XCTAssertNotEqual(results[0].id, results[1].id)
  }
}
