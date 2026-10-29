import Foundation
import XCTest
@testable import HermesAutoCore

final class ConnectionsTests: XCTestCase {
    func testConnectionFailuresReachTheTopLevelAttentionState() throws {
        let status = try Decode.status(Data(#"{"health":{"now":1000,"sources":{"matrix":{"last_poll":800,"retry_at":900,"channels":["Signal","WhatsApp"]},"proton":{"last_poll":800,"listener":{"connected":false}}},"relay":{"connected":false}}}"#.utf8))
        XCTAssertEqual(Summary.problems(status).count, 3)
        XCTAssertEqual(Summary.overall(status, error: nil, dismissed: []), .attention(3))
    }

    func testRetryRemainsVisibleUntilSuccessfulReadEvenWhenDeadlinePassed() throws {
        let status = try Decode.status(Data(#"{"health":{"now":1000,"sources":{"matrix":{"last_poll":800,"retry_at":900,"channels":["Signal"]},"proton":{"last_poll":800,"listener":{"connected":false}},"imessage":{}}}}"#.utf8))
        let items = Connections.groups(status).flatMap(\.items)
        XCTAssertEqual(items.first { $0.title == "Signal" }?.state, .warning)
        XCTAssertEqual(items.first { $0.title == "Signal" }?.statusText, "Matrix read error")
        XCTAssertEqual(items.first { $0.title == "Proton" }?.statusText, "Listener disconnected")
        XCTAssertEqual(items.first { $0.title == "iMessage" }?.state, .unknown)
        XCTAssertEqual(items.first { $0.title == "iMessage" }?.statusText, "Not read yet")
    }

    func testNonMatrixRetryHasAGenericReadError() throws {
        let status = try Decode.status(Data(#"{"health":{"now":1000,"sources":{"gmail":{"last_poll":800,"retry_at":900}}}}"#.utf8))
        XCTAssertEqual(Connections.groups(status).first?.items.first?.statusText, "Read error")
    }

    func testOlderCLIAndUnknownSourcesRemainVisible() throws {
        let status = try Decode.status(Data(#"{"health":{"now":1000,"sources":{"matrix":{"last_poll":990},"custom":{"last_poll":980}}}}"#.utf8))
        let groups = Connections.groups(status)
        XCTAssertEqual(groups.map(\.title), ["Messages", "Other"])
        XCTAssertEqual(groups.first?.items.map(\.title), ["Matrix"])
        XCTAssertEqual(groups.first?.items.first?.statusText, "Read confirmed")
        XCTAssertEqual(groups.last?.items.map(\.title), ["Custom"])
    }

    func testEmptyChannelFallsBackToSourceName() throws {
        let data = Data(#"[{"account":"gmail:test","item_id":"1","source":"gmail","channel":"","kind":"mail","decision":"skip","reason":"test","summary":"","created":"2026-10-27 06:00:00","title":"Test","from":"","place":"","url":"","can_open":true}]"#.utf8)
        XCTAssertEqual(try Decode.events(data).first?.sourceName, "Gmail")
    }

    func testChannelFilterKeepsTheDecisionAndItsSessionTogether() throws {
        let data = Data(#"[{"account":"matrix:test","item_id":"1","source":"matrix","channel":"WhatsApp","kind":"matrix","decision":"session","reason":"test","summary":"","created":"2026-10-27 06:00:00","title":"Test","from":"","place":"","url":"","session_id":"s1","can_open":false},{"account":"matrix:test","item_id":"2","source":"matrix","channel":"Signal","kind":"matrix","decision":"skip","reason":"test","summary":"","created":"2026-10-27 06:00:00","title":"Test","from":"","place":"","url":"","can_open":true}]"#.utf8)
        let events = try Decode.events(data)
        XCTAssertEqual(events.first?.channel, "WhatsApp")
        XCTAssertEqual(SourceScope.events(events, filter: .all, source: "matrix", channel: "WhatsApp").map(\.itemId), ["1"])
        XCTAssertTrue(SourceScope.events(events, filter: .skipped, source: "matrix", channel: "WhatsApp").isEmpty)
        XCTAssertEqual(SourceScope.events(events, filter: .all, source: "matrix", channel: nil).count, 2)
    }

    func testMessagesGroupUsesObservedAppsWithoutInventingBridgeHealth() throws {
        let status = try Decode.status(Data(#"{"paused":false,"health":{"now":1000,"sources":{"matrix":{"last_poll":990,"channels":["WhatsApp","Messenger","WhatsApp"]},"imessage":{"last_poll":980},"slack":{"last_poll":970},"gmail":{"last_poll":960},"todoist":{"last_poll":950}}}}"#.utf8))
        let groups = Connections.groups(status)
        XCTAssertEqual(groups.map(\.title), ["Messages", "Mail", "Tasks & projects"])
        let messages = try XCTUnwrap(groups.first)
        XCTAssertEqual(messages.items.map(\.title), ["iMessage", "Messenger", "Slack", "WhatsApp"])
        let whatsapp = try XCTUnwrap(messages.items.first { $0.title == "WhatsApp" })
        XCTAssertEqual(whatsapp.source, "matrix")
        XCTAssertEqual(whatsapp.channel, "WhatsApp")
        XCTAssertEqual(whatsapp.state, .unknown)
        XCTAssertEqual(whatsapp.statusText, "Bridge status unknown")
        XCTAssertEqual(whatsapp.path, ["WhatsApp", "bridge", "Matrix", "Hermes Auto"])
        XCTAssertEqual(whatsapp.health.lastPoll, 990)
    }
}
