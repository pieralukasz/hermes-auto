import Foundation
@testable import HermesAutoCore
import XCTest

final class OverviewSnapshotTests: XCTestCase {
    private func ok(_ name: String) throws -> CommandResult {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "Fixtures/\(name)", withExtension: "json"))
        return CommandResult(exitCode: 0, stdout: try Data(contentsOf: url), stderr: "")
    }

    private let failed = CommandResult(exitCode: 1, stdout: Data(), stderr: "hermes-auto: baza zablokowana")
    private let garbage = CommandResult(exitCode: 0, stdout: Data("nie json".utf8), stderr: "")

    func testEventsFailureKeepsFreshStatusAndItsAttention() throws {
        for broken in [failed, garbage] {
            var snapshot = OverviewSnapshot()
            XCTAssertTrue(snapshot.acceptStatus(try ok("status")))
            snapshot.acceptEvents(broken)
            XCTAssertNotNil(snapshot.status)
            XCTAssertNil(snapshot.statusError)
            XCTAssertNotNil(snapshot.eventsError)
            XCTAssertEqual(snapshot.overall(dismissed: []), .attention(3))
        }
    }

    func testStatusFailureKeepsPreviousDataButIsUnavailable() throws {
        for broken in [failed, garbage] {
            var snapshot = OverviewSnapshot()
            snapshot.acceptStatus(try ok("status"))
            snapshot.acceptEvents(try ok("events"))
            let previous = snapshot
            XCTAssertFalse(snapshot.acceptStatus(broken))
            XCTAssertEqual(snapshot.status, previous.status)
            XCTAssertEqual(snapshot.events, previous.events)
            let error = try XCTUnwrap(snapshot.statusError)
            XCTAssertEqual(snapshot.overall(dismissed: []), .unavailable(error))
        }
        var snapshot = OverviewSnapshot()
        snapshot.acceptStatus(failed)
        XCTAssertEqual(snapshot.statusError, "baza zablokowana")
    }

    func testRefreshErrorsExplainWhichOutputFailed() throws {
        var snapshot = OverviewSnapshot()
        snapshot.acceptStatus(garbage)
        XCTAssertTrue(try XCTUnwrap(snapshot.statusError).hasPrefix("Cannot read the `hermes-auto status` output: "))
        snapshot.acceptEvents(failed)
        XCTAssertEqual(snapshot.eventsError, "Event history unavailable: baza zablokowana")
        snapshot.acceptEvents(garbage)
        XCTAssertTrue(try XCTUnwrap(snapshot.eventsError).hasPrefix("Cannot read the event history: "))
    }

    func testSuccessfulEventsClearsOnlyTheEventsError() throws {
        var snapshot = OverviewSnapshot()
        snapshot.acceptEvents(failed)
        snapshot.acceptStatus(failed)
        snapshot.acceptEvents(try ok("events"))
        XCTAssertNil(snapshot.eventsError)
        XCTAssertEqual(snapshot.events.count, 5)
        XCTAssertEqual(snapshot.statusError, "baza zablokowana")
    }
}
