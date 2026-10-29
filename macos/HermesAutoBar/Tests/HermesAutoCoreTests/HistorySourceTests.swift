import Foundation
import XCTest
@testable import HermesAutoCore

final class HistorySourceTests: XCTestCase {
    func testHistoricalSourcesAndSelectionStayAvailableWithoutHealthMetadata() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "Fixtures/Connections/events", withExtension: "json"))
        let events = try Decode.events(Data(contentsOf: url))
        let selected = HistorySource(source: "retired")
        let choices = HistorySource.choices(status: nil, events: events, including: selected)
        XCTAssertTrue(choices.contains(HistorySource(source: "gmail")))
        XCTAssertTrue(choices.contains(HistorySource(source: "matrix", channel: "WhatsApp")))
        XCTAssertTrue(choices.contains(HistorySource(source: "matrix", channel: "Messenger")))
        XCTAssertTrue(choices.contains(selected))
        XCTAssertEqual(choices.count, Set(choices).count)
        XCTAssertEqual(HistorySource.choices(status: nil, events: []), [.all])
    }

    func testChoicesIncludeConfiguredSourcesAndIndividualMessengers() throws {
        let status = try Decode.status(Data(#"{"health":{"now":1000,"sources":{"gmail":{"last_poll":990},"matrix":{"channels":["WhatsApp","Messenger","WhatsApp"]},"slack":{}}}}"#.utf8))
        let choices = HistorySource.choices(status: status, events: [])
        XCTAssertEqual(choices.first, .all)
        XCTAssertEqual(Set(choices.map(\.label)), ["All sources", "Gmail", "Matrix (all)", "Messenger", "Slack", "WhatsApp"])
        XCTAssertEqual(choices.first { $0.label == "WhatsApp" }, HistorySource(source: "matrix", channel: "WhatsApp"))
    }
}
