import Foundation

/// What the menu bar icon says at a glance.
public enum Overall: Equatable, Sendable {
    case unavailable(String)  // the CLI did not answer
    case paused
    case attention(Int)       // problems the user has not put aside
    case working(Int)         // jobs queued or running
    case ok
}

/// Something that needs a look, with the action that resolves it.
public struct Problem: Equatable, Sendable, Identifiable {
    public enum Kind: Equatable, Sendable {
        case job(Int)            // needs_attention: retry or a fresh session
        case followup(Int)       // reply that did not reach its session: retry-followup
        case sourceError         // reading a source failed
        case pending(String)     // a model route failed to classify; retried by itself
        case triageIssue(String) // a Todoist plan item the sorter stopped retrying
    }

    public var kind: Kind
    public var title: String
    public var detail: String
    public var sessionId: String?
    /// Stable while the problem stays the same; a new failure of the same job gets a new key.
    public var key: String
    public var id: String { key }
}

public enum Summary {
    public static let activeStatuses: Set<String> = ["pending", "created", "creating", "running", "retry_pending"]

    public static func problems(_ status: Status) -> [Problem] {
        var result: [Problem] = []
        for job in status.jobs where job.status == "needs_attention" {
            result.append(Problem(kind: .job(job.id), title: job.title, detail: job.error, sessionId: job.sessionId,
                                  key: "job:\(job.id):\(job.updated ?? "")"))
        }
        for followup in status.followups where followup.status == "needs_attention" {
            result.append(Problem(kind: .followup(followup.id), title: followup.title, detail: followup.error,
                                  sessionId: followup.sessionId, key: "followup:\(followup.id):\(followup.attempts)"))
        }
        for error in status.sourceErrors {
            result.append(Problem(kind: .sourceError, title: sourceOfError(error), detail: error, sessionId: nil,
                                  key: "source:\(error)"))
        }
        for pending in status.triagePending {
            result.append(Problem(kind: .pending(pending.source),
                                  title: "\(sourceLabel(pending.source)): do ponownej klasyfikacji \(pending.items)",
                                  detail: "Ponowienie \(pending.nextAt). \(pending.error)", sessionId: nil,
                                  key: "pending:\(pending.source):\(pending.account):\(pending.attempts)"))
        }
        for issue in status.triageIssues {
            result.append(Problem(kind: .triageIssue(issue.itemId), title: issue.content, detail: issue.error,
                                  sessionId: nil, key: "issue:\(issue.itemId):\(issue.attempts)"))
        }
        return result
    }

    public static func active(_ status: Status) -> [Job] {
        status.jobs.filter { activeStatuses.contains($0.status) }
    }

    public static func overall(_ status: Status?, error: String?, dismissed: Set<String>) -> Overall {
        guard let status else { return .unavailable(error ?? "Brak danych") }
        if status.paused { return .paused }
        let open = problems(status).filter { !dismissed.contains($0.key) }
        if !open.isEmpty { return .attention(open.count) }
        let running = active(status).count
        return running > 0 ? .working(running) : .ok
    }

    /// "triage github: Hermes classify failed ..." -> "GitHub"
    static func sourceOfError(_ error: String) -> String {
        let words = error.split(separator: " ")
        let name = words.first == "triage" && words.count > 1 ? words[1] : (words.first ?? "")
        return sourceLabel(String(name.trimmingCharacters(in: CharacterSet(charactersIn: ":"))))
    }
}

/// Timeline filters shown above the list.
public enum EventFilter: String, CaseIterable, Sendable, Identifiable {
    case all = "Wszystko", sessions = "Sesje", brief = "Brief", skipped = "Pominięte", todoist = "Todoist"
    public var id: String { rawValue }

    public func includes(_ event: Event) -> Bool {
        switch self {
        case .all: return true
        case .sessions: return event.kind != "task" && ["session", "followup"].contains(event.decision)
        case .brief: return event.decision == "brief"
        case .skipped: return event.kind != "task" && ["skip", "read"].contains(event.decision)
        case .todoist: return event.kind == "task"
        }
    }
}

/// What the lists show once a source chip is selected (nil = every source).
public enum SourceScope {
    public static func events(_ events: [Event], filter: EventFilter, source: String?) -> [Event] {
        events.filter { filter.includes($0) && (source == nil || $0.source == source) }
    }

    public static func jobs(_ jobs: [Job], source: String?) -> [Job] {
        guard let source else { return jobs }
        return jobs.filter { $0.source == source }
    }

    /// A second click on the selected chip clears the selection.
    public static func toggle(_ current: String?, _ tapped: String) -> String? {
        current == tapped ? nil : tapped
    }
}

public func sourceLabel(_ source: String) -> String {
    switch source {
    case "gmail": return "Gmail"
    case "proton": return "Proton"
    case "slack": return "Slack"
    case "github": return "GitHub"
    case "linear": return "Linear"
    case "todoist": return "Todoist"
    case "brief": return "Brief"
    case "questions": return "Plan Todoist"
    case "proposals": return "Propozycje"
    case "unblocked": return "Odblokowane"
    default: return source.isEmpty ? "?" : source.prefix(1).uppercased() + source.dropFirst()
    }
}

public func sourceSymbol(_ source: String) -> String {
    switch source {
    case "gmail": return "envelope"
    case "proton": return "envelope.badge.shield.half.filled"
    case "slack": return "number"
    case "github": return "chevron.left.forwardslash.chevron.right"
    case "linear": return "point.3.connected.trianglepath.dotted"
    case "todoist": return "checklist"
    case "brief": return "sun.horizon"
    case "questions": return "list.bullet.clipboard"
    case "proposals": return "lightbulb"
    case "unblocked": return "lock.open"
    default: return "circle.dashed"
    }
}

public func decisionLabel(_ decision: String) -> String {
    switch decision {
    case "session": return "Sesja"
    case "followup": return "Odpowiedź ↩"
    case "brief": return "Brief"
    case "skip": return "Pominięte"
    case "read": return "Przeczytane"
    case "planned": return "Plan"
    case "moved": return "Przeniesione"
    case "asked": return "Pytanie"
    case "baseline": return "Stan początkowy"
    case "needs_attention": return "Wymaga uwagi"
    case "user_changed": return "Zmienione przez Ciebie"
    default: return decision
    }
}

public func jobStatusLabel(_ status: String) -> String {
    switch status {
    case "pending", "created": return "W kolejce"
    case "creating", "running": return "Pracuje"
    case "retry_pending": return "Ponowienie w kolejce"
    case "ready": return "Gotowa"
    case "done": return "Zrobione"
    case "needs_attention": return "Wymaga uwagi"
    case "deferred": return "Odłożone"
    case "deleted": return "Usunięta"
    case "superseded": return "Zastąpiona nową sesją"
    default: return status
    }
}

public enum Times {
    /// SQLite CURRENT_TIMESTAMP: "YYYY-MM-DD HH:MM:SS" in UTC.
    public static func parse(_ value: String?) -> Date? {
        guard let value, !value.isEmpty else { return nil }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter.date(from: value)
    }

    /// "teraz", "5 min", "3 h", "2 dni"
    public static func ago(_ date: Date?, now: Date) -> String {
        guard let date else { return "–" }
        let seconds = max(0, now.timeIntervalSince(date))
        if seconds < 60 { return "teraz" }
        if seconds < 3600 { return "\(Int(seconds / 60)) min" }
        if seconds < 86400 { return "\(Int(seconds / 3600)) h" }
        let days = Int(seconds / 86400)
        return days == 1 ? "1 dzień" : "\(days) dni"
    }

    /// Section titles of the timeline: "Dziś", "Wczoraj", then the date.
    public static func day(_ date: Date?, now: Date, calendar: Calendar = .current) -> String {
        guard let date else { return "Bez daty" }
        if calendar.isDate(date, inSameDayAs: now) { return "Dziś" }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now),
           calendar.isDate(date, inSameDayAs: yesterday) { return "Wczoraj" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "pl_PL")
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = "EEEE, d MMMM"
        return formatter.string(from: date)
    }

    /// Events grouped by day, newest day first, keeping the given (newest first) order inside a day.
    public static func grouped(_ events: [Event], now: Date, calendar: Calendar = .current) -> [(String, [Event])] {
        var order: [String] = []
        var groups: [String: [Event]] = [:]
        for event in events {
            let title = day(parse(event.created), now: now, calendar: calendar)
            if groups[title] == nil { order.append(title) }
            groups[title, default: []].append(event)
        }
        return order.map { ($0, groups[$0] ?? []) }
    }
}

/// Deep link into Hermes Desktop: `hermes://open/<session id>` routes to that conversation.
public func sessionURL(_ sessionId: String) -> URL? {
    let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "_-"))
    guard !sessionId.isEmpty, sessionId.unicodeScalars.allSatisfy({ allowed.contains($0) }) else { return nil }
    return URL(string: "hermes://open/\(sessionId)")
}

/// Only https links from the sorter's payload are opened.
public func sourceURL(_ value: String) -> URL? {
    guard let url = URL(string: value), url.scheme == "https", url.host != nil else { return nil }
    return url
}
