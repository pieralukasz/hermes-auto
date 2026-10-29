import Foundation
@testable import HermesAutoCore
import XCTest

final class HermesAutoCoreTests: XCTestCase {
    private func fixture(_ name: String) throws -> Data {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "Fixtures/\(name)", withExtension: "json"))
        return try Data(contentsOf: url)
    }

    func testChatSourcesHaveTheirOwnLabelsAndSymbols() {
        XCTAssertEqual(sourceLabel("imessage"), "iMessage")
        XCTAssertEqual(sourceLabel("matrix"), "Komunikatory")
        XCTAssertEqual(sourceSymbol("imessage"), "message")
        XCTAssertEqual(sourceSymbol("matrix"), "bubble.left.and.bubble.right")
    }

    func testStatusDecodesAndListsEveryProblem() throws {
        let status = try Decode.status(fixture("status"))
        XCTAssertEqual(status.jobs.count, 4)
        let kinds = Summary.problems(status).map(\.kind)
        XCTAssertEqual(kinds, [.job(42), .sourceError, .pending("slack")])
        XCTAssertEqual(Summary.problems(status)[1].title, "Slack")
        XCTAssertEqual(Summary.active(status).map(\.id), [44])
    }

    func testOverallPrefersPauseThenUndismissedProblemsThenWork() throws {
        var status = try Decode.status(fixture("status"))
        XCTAssertEqual(Summary.overall(status, error: nil, dismissed: []), .attention(3))
        let keys = Set(Summary.problems(status).map(\.key))
        XCTAssertEqual(Summary.overall(status, error: nil, dismissed: keys), .working(1))
        status.paused = true
        XCTAssertEqual(Summary.overall(status, error: nil, dismissed: []), .paused)
        XCTAssertEqual(Summary.overall(nil, error: "brak CLI", dismissed: []), .unavailable("brak CLI"))
    }

    func testADismissedJobReturnsWhenItFailsAgain() throws {
        var status = try Decode.status(fixture("status"))
        let key = Summary.problems(status)[0].key
        status.jobs[1].updated = "2026-10-27 06:40:00"  // retried and failed again
        XCTAssertNotEqual(Summary.problems(status)[0].key, key)
    }

    func testOlderStatusWithoutHealthOrTimesStillDecodes() throws {
        let data = Data(#"{"paused": false, "source_errors": [], "jobs": [{"id": 1, "source": "todoist", "title": "x", "mode": "agent", "status": "ready", "session_id": "s", "error": ""}]}"#.utf8)
        let status = try Decode.status(data)
        XCTAssertNil(status.health)
        XCTAssertNil(status.jobs[0].created)
        XCTAssertEqual(Summary.overall(status, error: nil, dismissed: []), .ok)
    }

    func testFiltersSplitSessionsBriefSkippedAndTodoist() throws {
        let events = try Decode.events(fixture("events"))
        func ids(_ filter: EventFilter) -> [String] { events.filter(filter.includes).map(\.itemId) }
        XCTAssertEqual(ids(.all).count, 5)
        XCTAssertEqual(ids(.sessions), ["9001@2026-10-27T06:29:00Z", "m-200"])
        XCTAssertEqual(ids(.brief), ["C1:1793000000.1"])
        XCTAssertEqual(ids(.skipped), ["m-201"])
        XCTAssertEqual(ids(.todoist), ["t-77"])
    }

    func testSourceChipNarrowsEventsAndSessionsAndTogglesOff() throws {
        let events = try Decode.events(fixture("events"))
        XCTAssertEqual(SourceScope.events(events, filter: .all, source: "gmail").map(\.itemId), ["m-201", "m-200"])
        XCTAssertEqual(SourceScope.events(events, filter: .skipped, source: "gmail").map(\.itemId), ["m-201"])
        XCTAssertEqual(SourceScope.events(events, filter: .all, source: nil).count, 5)
        XCTAssertTrue(SourceScope.events(events, filter: .all, source: "linear").isEmpty)
        let jobs = try Decode.status(fixture("status")).jobs
        XCTAssertEqual(SourceScope.jobs(jobs, source: "github").map(\.id), [44])
        XCTAssertEqual(SourceScope.jobs(jobs, source: nil).count, 4)
        XCTAssertEqual(SourceScope.toggle(nil, "github"), "github")
        XCTAssertEqual(SourceScope.toggle("gmail", "github"), "github")
        XCTAssertNil(SourceScope.toggle("github", "github"))
    }

    func testTimesAreUTCAndGroupedByLocalDay() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try XCTUnwrap(TimeZone(identifier: "Europe/Warsaw"))
        let now = try XCTUnwrap(Times.parse("2026-10-27 06:40:00"))
        XCTAssertEqual(Times.ago(Times.parse("2026-10-27 06:29:30"), now: now), "10 min")
        XCTAssertEqual(Times.ago(Times.parse("2026-10-26 17:05:00"), now: now), "13 h")
        XCTAssertEqual(Times.ago(nil, now: now), "–")
        // 23:30 UTC on the 26th is already the 27th in Warsaw.
        XCTAssertEqual(Times.day(Times.parse("2026-10-26 23:30:00"), now: now, calendar: calendar), "Dziś")
        XCTAssertEqual(Times.day(Times.parse("2026-10-26 17:05:00"), now: now, calendar: calendar), "Wczoraj")
        let groups = Times.grouped(try Decode.events(fixture("events")), now: now, calendar: calendar)
        XCTAssertEqual(groups.map(\.0), ["Dziś", "Wczoraj"])
        XCTAssertEqual(groups[0].1.count, 3)
    }

    func testLinksOnlyOpenSessionIdsAndHttps() {
        XCTAssertEqual(sessionURL("20261027_063000_dddddddddddd")?.absoluteString, "hermes://open/20261027_063000_dddddddddddd")
        XCTAssertNil(sessionURL("../settings"))
        XCTAssertNil(sessionURL(""))
        XCTAssertNotNil(sourceURL("https://github.com/example/app/pull/12"))
        XCTAssertNil(sourceURL("file:///etc/passwd"))
        XCTAssertNil(sourceURL("javascript:alert(1)"))
    }

    func testActionsMapToExplicitCommands() {
        XCTAssertEqual(Action.openItem(account: "gmail:me@example.test", itemId: "m-201").arguments,
                       ["open-item", "gmail:me@example.test", "m-201"])
        XCTAssertEqual(Action.retry(job: 42).arguments, ["retry", "42"])
        XCTAssertEqual(Action.newSession(job: 42).arguments, ["new-session", "42"])
        XCTAssertEqual(Action.retryFollowup(3).arguments, ["retry-followup", "3"])
        XCTAssertEqual(Action.pause.arguments, ["pause"])
    }

    func testBusyLockIsRecognisedAndMessagesLoseThePrefix() {
        let busy = CommandResult(exitCode: 1, stdout: Data(),
                                 stderr: "hermes-auto: Another hermes-auto operation is running; try again later\n")
        XCTAssertTrue(busy.isBusy)
        XCTAssertEqual(busy.message, "Another hermes-auto operation is running; try again later")
        let other = CommandResult(exitCode: 1, stdout: Data(), stderr: "hermes-auto: No job 9\n")
        XCTAssertFalse(other.isBusy)
    }

    func testCLIRunnerReportsExitCodeOutputAndMissingBinary() async throws {
        let ok = await CLIRunner(executable: URL(fileURLWithPath: "/bin/echo")).run(["status"])
        XCTAssertEqual(ok.exitCode, 0)
        XCTAssertEqual(String(decoding: ok.stdout, as: UTF8.self), "status\n")
        let missing = await CLIRunner(executable: URL(fileURLWithPath: "/nonexistent/hermes-auto")).run(["status"])
        XCTAssertEqual(missing.exitCode, 127)
        let slow = await CLIRunner(executable: URL(fileURLWithPath: "/bin/sleep"), timeout: 0.5).run(["5"])
        XCTAssertEqual(slow.exitCode, 124)
    }

    func testCLIRunnerKillsAProcessThatIgnoresSIGTERMWithinTheDeadline() async {
        let started = Date()
        let stubborn = await CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"), timeout: 0.5)
            .run(["-c", "trap '' TERM; sleep 30"])
        XCTAssertEqual(stubborn.exitCode, 124)
        XCTAssertLessThan(Date().timeIntervalSince(started), 6)
    }
}
