import AppKit
import HermesAutoCore
import Observation

/// App state: the latest `status` and `events`, what the user put aside, and the action in flight.
@MainActor
@Observable
final class Model {
    var status: Status?
    var events: [Event] = []
    var loadError: String?
    var lastRefresh: Date?
    var busyAction: String?
    var notice: String?
    var noticeIsError = false
    var dismissed: Set<String>
    /// Source chip selected in the strip: only its events and sessions are listed (nil = all).
    var sourceFilter: String? = Preview.source

    let runner: CommandRunner
    let isFixture: Bool
    private var timer: Timer?
    private var refreshing = false

    init(runner: CommandRunner, isFixture: Bool) {
        self.runner = runner
        self.isFixture = isFixture
        dismissed = Set(UserDefaults.standard.stringArray(forKey: "dismissedProblems") ?? [])
    }

    var overall: Overall { Summary.overall(status, error: loadError, dismissed: dismissed) }
    var problems: [Problem] { status.map(Summary.problems) ?? [] }
    var openProblems: [Problem] { problems.filter { !dismissed.contains($0.key) } }
    var activeJobs: [Job] { status.map(Summary.active) ?? [] }
    var now: Date { Date(timeIntervalSince1970: status?.health?.now ?? Date().timeIntervalSince1970) }

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
        guard statusResult.exitCode == 0 else {
            loadError = statusResult.message
            return
        }
        do {
            status = try Decode.status(statusResult.stdout)
            loadError = nil
        } catch {
            loadError = "Nie rozumiem odpowiedzi `hermes-auto status`: \(error.localizedDescription)"
            return
        }
        let eventsResult = await runner.run(["events", "--limit", "300"])
        if eventsResult.exitCode == 0, let decoded = try? Decode.events(eventsResult.stdout) {
            events = decoded
        } else if eventsResult.exitCode != 0 {
            // An older CLI without `events`: the status part still works.
            loadError = "Oś zdarzeń niedostępna: \(eventsResult.message)"
        }
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
                notice = "Automat właśnie pracuje, ponawiam (\(attempts)/6)…"
                try? await Task.sleep(for: .seconds(5))
                result = await runner.run(action.arguments)
            }
            busyAction = nil
            if result.exitCode == 0 {
                show(isFixture ? "Podgląd: \(action.arguments.joined(separator: " "))" : action.done, error: false)
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
        UserDefaults.standard.set(Array(dismissed), forKey: "dismissedProblems")
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
        guard let sessionId, let url = sessionURL(sessionId) else { return }
        NSWorkspace.shared.open(url)
    }

    func openSource(_ value: String) {
        guard let url = sourceURL(value) else { return }
        NSWorkspace.shared.open(url)
    }

    func openLog() {
        let log = FileManager.default.homeDirectoryForCurrentUser.appending(path: ".config/hermes-auto/service.log")
        NSWorkspace.shared.open(log)
    }
}
