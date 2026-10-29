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
        XCTAssertEqual(sourceLabel("matrix"), "Matrix")
        XCTAssertEqual(sourceSymbol("imessage"), "message")
        XCTAssertEqual(sourceSymbol("matrix"), "bubble.left.and.bubble.right")
    }

    func testLabelsAreEnglish() {
        XCTAssertEqual(EventFilter.allCases.map(\.rawValue), ["All", "Sessions", "Brief", "Skipped", "Todoist"])
        XCTAssertEqual(["questions", "proposals", "unblocked", "custom", ""].map(sourceLabel),
                       ["Todoist plan", "Proposals", "Unblocked", "Custom", "?"])
        XCTAssertEqual(["session", "followup", "brief", "skip", "read", "planned", "moved", "asked", "baseline",
                        "needs_attention", "user_changed", "other"].map(decisionLabel),
                       ["Session", "Reply ↩", "Brief", "Skipped", "Read", "Plan", "Moved", "Question", "Baseline",
                        "Needs attention", "Changed by you", "other"])
        XCTAssertEqual(["pending", "created", "creating", "running", "retry_pending", "ready", "done", "needs_attention",
                        "deferred", "deleted", "superseded", "other"].map(jobStatusLabel),
                       ["Queued", "Queued", "Working", "Working", "Retry queued", "Ready", "Done", "Needs attention",
                        "Deferred", "Deleted", "Replaced by a new session", "other"])
    }

    func testProblemCopyIsEnglishAndKeepsRawErrors() throws {
        let status = try Decode.status(Data(#"{"triage_pending":[{"source":"slack","account":"slack:t","items":2,"attempts":1,"error":"model timeout","next_at":"2026-10-27 07:00:00"}],"health":{"now":1000,"sources":{"matrix":{"last_poll":800,"retry_at":900},"proton":{"last_poll":800,"listener":{"connected":false}}},"relay":{"connected":false}}}"#.utf8))
        let problems = Summary.problems(status)
        XCTAssertEqual(problems.map(\.title), ["Matrix", "Proton", "ZimaBlade: not connected", "Slack: 2 to reclassify"])
        XCTAssertEqual(problems.map(\.detail), [
            "The last read failed and is not confirmed yet. Hermes Auto will retry.",
            "The new-event listener reports a disconnect. The last read does not confirm it is listening.",
            "The relay does not confirm its connection. New signals may not reach this Mac.",
            "Next retry 2026-10-27 07:00:00. model timeout",
        ])
        XCTAssertEqual(Summary.overall(nil, error: nil, dismissed: []), .unavailable("No data"))
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

    func testRefreshFailureDoesNotPresentCachedStatusAsHealthy() throws {
        var status = try Decode.status(fixture("status"))
        status.jobs = []
        status.sourceErrors = []
        status.triagePending = []
        XCTAssertEqual(Summary.overall(status, error: "CLI nie odpowiada", dismissed: []),
                       .unavailable("CLI nie odpowiada"))
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
        XCTAssertEqual(Times.ago(Times.parse("2026-10-27 06:39:30"), now: now), "now")
        XCTAssertEqual(Times.ago(Times.parse("2026-10-26 06:00:00"), now: now), "1 day")
        XCTAssertEqual(Times.ago(Times.parse("2026-10-24 06:00:00"), now: now), "3 days")
        XCTAssertEqual(Times.ago(nil, now: now), "–")
        // 23:30 UTC on the 26th is already the 27th in Warsaw.
        XCTAssertEqual(Times.day(Times.parse("2026-10-26 23:30:00"), now: now, calendar: calendar), "Today")
        XCTAssertEqual(Times.day(Times.parse("2026-10-26 17:05:00"), now: now, calendar: calendar), "Yesterday")
        XCTAssertEqual(Times.day(Times.parse("2026-10-24 12:00:00"), now: now, calendar: calendar), "Saturday, October 24")
        XCTAssertEqual(Times.day(nil, now: now, calendar: calendar), "No date")
        let groups = Times.grouped(try Decode.events(fixture("events")), now: now, calendar: calendar)
        XCTAssertEqual(groups.map(\.0), ["Today", "Yesterday"])
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

    func testActionAcknowledgementsDoNotClaimMoreThanTheCommandDid() {
        XCTAssertEqual(Action.pause.done, "Hermes Auto paused")
        XCTAssertEqual(Action.resume.done, "Hermes Auto resumed")
        for action in [Action.retry(job: 1), .newSession(job: 1), .openItem(account: "gmail:me@example.test", itemId: "m-1")] {
            XCTAssertEqual(action.done, "Session requested")
        }
        XCTAssertEqual(Action.retryFollowup(3).done, "Reply retry queued")
    }

    func testBusyLockIsRecognisedAndMessagesLoseThePrefix() {
        let busy = CommandResult(exitCode: 1, stdout: Data(),
                                 stderr: "hermes-auto: Another hermes-auto operation is running; try again later\n")
        XCTAssertTrue(busy.isBusy)
        XCTAssertEqual(busy.message, "Another hermes-auto operation is running; try again later")
        let other = CommandResult(exitCode: 1, stdout: Data(), stderr: "hermes-auto: No job 9\n")
        XCTAssertFalse(other.isBusy)
        XCTAssertEqual(other.message, "No job 9")
        XCTAssertEqual(CommandResult(exitCode: 2, stdout: Data(), stderr: " \n").message, "hermes-auto exited with code 2")
    }

    func testCLIRunnerReportsExitCodeOutputAndMissingBinary() async throws {
        let ok = await CLIRunner(executable: URL(fileURLWithPath: "/bin/echo")).run(["status"])
        XCTAssertEqual(ok.exitCode, 0)
        XCTAssertEqual(String(decoding: ok.stdout, as: UTF8.self), "status\n")
        let missing = await CLIRunner(executable: URL(fileURLWithPath: "/nonexistent/hermes-auto")).run(["status"])
        XCTAssertEqual(missing.exitCode, 127)
        XCTAssertTrue(missing.stderr.hasPrefix("Cannot start /nonexistent/hermes-auto: "), missing.stderr)
        let slow = await CLIRunner(executable: URL(fileURLWithPath: "/bin/sleep"), timeout: 1).run(["5"])
        XCTAssertEqual(slow.exitCode, 124)
        XCTAssertEqual(slow.stderr, "hermes-auto did not respond within 1 s")
    }

    func testCLIRunnerReportsAProcessThatClosedItsOutputButDidNotExit() async {
        let lingering = await CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"), timeout: 10)
            .run(["-c", "exec >&- 2>&-; exec sleep 30"])
        XCTAssertEqual(lingering.exitCode, 124)
        XCTAssertEqual(lingering.stderr, "hermes-auto did not exit after closing its output")
    }

    func testFixtureRunnerNamesTheMissingFile() async {
        let result = await FixtureRunner(directory: URL(fileURLWithPath: "/nonexistent")).run(["status"])
        XCTAssertEqual(result.exitCode, 1)
        XCTAssertEqual(result.stderr, "status.json not found in /nonexistent")
    }

    func testCLIRunnerKillsAProcessThatIgnoresSIGTERMWithinTheDeadline() async {
        let started = Date()
        let stubborn = await CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"), timeout: 0.5)
            .run(["-c", "trap '' TERM; sleep 30"])
        XCTAssertEqual(stubborn.exitCode, 124)
        XCTAssertLessThan(Date().timeIntervalSince(started), 6)
    }
}
