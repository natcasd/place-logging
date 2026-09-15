import Foundation
import XCTest
@testable import JotClientCore

final class ResponseContractTests: XCTestCase {
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
