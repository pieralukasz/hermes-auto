import AppKit
import HermesAutoCore
import Observation

/// App state: the latest `status` and `events`, what the user put aside, and the action in flight.
@MainActor
@Observable
final class Model {
    private(set) var snapshot = OverviewSnapshot()
    var lastRefresh: Date?
    var busyAction: String?
    var notice: String?
    var noticeIsError = false
    var dismissed: Set<String>
    /// History source selection also scopes the sessions list (nil = all).
    var sourceFilter: String? = Preview.source
    var channelFilter: String?

    let runner: CommandRunner
    let isFixture: Bool
    private var timer: Timer?
    private var refreshing = false

    init(runner: CommandRunner, isFixture: Bool) {
        self.runner = runner
        self.isFixture = isFixture
        dismissed = isFixture ? [] : Set(UserDefaults.standard.stringArray(forKey: "dismissedProblems") ?? [])
    }

    var status: Status? { snapshot.status }
    var events: [Event] { snapshot.events }
    /// Only a failed `status` read; it makes the whole state stale.
    var loadError: String? { snapshot.statusError }
    var eventsError: String? { snapshot.eventsError }
    var overall: Overall { snapshot.overall(dismissed: dismissed) }
    var problems: [Problem] { status.map(Summary.problems) ?? [] }
    var openProblems: [Problem] { problems.filter { !dismissed.contains($0.key) } }
    var activeJobs: [Job] { status.map(Summary.active) ?? [] }
    /// Fixtures carry their own clock so screenshots stay stable; live data is measured against the real time.
    var now: Date {
        guard isFixture, let fixed = status?.health?.now else { return Date() }
        return Date(timeIntervalSince1970: fixed)
    }

    /// Newest automation sessions first (Todoist runs, briefs, mail, replies).
    var recentJobs: [Job] {
        (status?.jobs ?? []).sorted { ($0.created ?? "", $0.id) > ($1.created ?? "", $1.id) }
    }

    func start() {
        Task { await refresh() }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
    }

    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        let statusResult = await runner.run(["status"])
        guard snapshot.acceptStatus(statusResult) else { return }
        let eventsResult = await runner.run(["events", "--limit", "300"])
        snapshot.acceptEvents(eventsResult)
        lastRefresh = Date()
    }

    /// Runs one command; a check in progress holds the lock, so it is retried a few times.
    func perform(_ action: Action, label: String) {
        guard busyAction == nil else { return }
        busyAction = label
        notice = nil
        Task {
            var result = await runner.run(action.arguments)
            var attempts = 0
            while result.isBusy && attempts < 6 {
                attempts += 1
                notice = "Automation is busy, retrying (\(attempts)/6)…"
                try? await Task.sleep(for: .seconds(5))
                result = await runner.run(action.arguments)
            }
            busyAction = nil
            if result.exitCode == 0 {
                show(isFixture ? "Preview: \(action.arguments.joined(separator: " "))" : action.done, error: false)
            } else {
                show(result.message, error: true)
            }
            await refresh()
        }
    }

    func dismiss(_ problem: Problem) {
        dismissed.insert(problem.key)
        saveDismissed()
    }

    func restoreDismissed() {
        dismissed.removeAll()
        saveDismissed()
    }

    private func saveDismissed() {
        // Keep only keys that still describe a current problem, so the list does not grow forever.
        let current = Set(problems.map(\.key))
        dismissed = dismissed.intersection(current)
        if !isFixture { UserDefaults.standard.set(Array(dismissed), forKey: "dismissedProblems") }
    }

    private func show(_ text: String, error: Bool) {
        notice = text
        noticeIsError = error
        let shown = text
        Task {
            try? await Task.sleep(for: .seconds(error ? 12 : 5))
            if notice == shown { notice = nil }
        }
    }

    func openSession(_ sessionId: String?) {
        if isFixture { show("Preview: open session in Hermes", error: false); return }
        guard let sessionId, let url = sessionURL(sessionId) else { return }
        NSWorkspace.shared.open(url)
    }

    func openSource(_ value: String) {
        if isFixture { show("Preview: open message in its source", error: false); return }
        guard let url = sourceURL(value) else { return }
        NSWorkspace.shared.open(url)
    }

    func openLog() {
        if isFixture { show("Preview: open automation log", error: false); return }
        let log = FileManager.default.homeDirectoryForCurrentUser.appending(path: ".config/hermes-auto/service.log")
        NSWorkspace.shared.open(log)
    }
}
