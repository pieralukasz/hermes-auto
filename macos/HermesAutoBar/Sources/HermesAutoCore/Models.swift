import Foundation

// Shapes of `hermes-auto status` and `hermes-auto events` (JSON). Optional fields keep older CLIs readable.

public struct Status: Decodable, Sendable, Equatable {
    public var paused: Bool
    public var sourceErrors: [String]
    public var jobs: [Job]
    public var followups: [Followup]
    public var triageIssues: [TriageIssue]
    public var triagePending: [Pending]
    public var health: Health?

    enum CodingKeys: String, CodingKey {
        case paused, jobs, followups, health
        case sourceErrors = "source_errors", triageIssues = "triage_issues", triagePending = "triage_pending"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        paused = (try? c.decode(Bool.self, forKey: .paused)) ?? false
        sourceErrors = (try? c.decode([String].self, forKey: .sourceErrors)) ?? []
        jobs = try c.decodeIfPresent([Job].self, forKey: .jobs) ?? []
        followups = try c.decodeIfPresent([Followup].self, forKey: .followups) ?? []
        triageIssues = try c.decodeIfPresent([TriageIssue].self, forKey: .triageIssues) ?? []
        triagePending = try c.decodeIfPresent([Pending].self, forKey: .triagePending) ?? []
        health = try c.decodeIfPresent(Health.self, forKey: .health)
    }
}

public struct Job: Decodable, Sendable, Equatable, Identifiable {
    public var id: Int
    public var source: String
    public var title: String
    public var mode: String
    public var status: String
    public var sessionId: String
    public var error: String
    public var created: String?
    public var updated: String?

    enum CodingKeys: String, CodingKey {
        case id, source, title, mode, status, error, created, updated
        case sessionId = "session_id"
    }
}

public struct Followup: Decodable, Sendable, Equatable, Identifiable {
    public var id: Int
    public var source: String
    public var title: String
    public var status: String
    public var sessionId: String
    public var attempts: Int
    public var error: String

    enum CodingKeys: String, CodingKey {
        case id, source, title, status, attempts, error
        case sessionId = "session_id"
    }
}

public struct TriageIssue: Decodable, Sendable, Equatable {
    public var itemId: String
    public var decision: String
    public var content: String
    public var attempts: Int
    public var error: String

    enum CodingKeys: String, CodingKey {
        case decision, content, attempts, error
        case itemId = "item_id"
    }
}

public struct Pending: Decodable, Sendable, Equatable {
    public var source: String
    public var account: String
    public var items: Int
    public var attempts: Int
    public var error: String
    public var nextAt: String

    enum CodingKeys: String, CodingKey {
        case source, account, items, attempts, error
        case nextAt = "next_at"
    }
}

public struct Health: Decodable, Sendable, Equatable {
    public var sources: [String: SourceHealth]
    public var relay: Listener?
    public var lastCheck: Double?
    public var now: Double

    enum CodingKeys: String, CodingKey {
        case sources, relay, now
        case lastCheck = "last_check"
    }
}

public struct SourceHealth: Decodable, Sendable, Equatable {
    public var lastPoll: Double?
    public var retryAt: Double?
    public var listener: Listener?

    enum CodingKeys: String, CodingKey {
        case listener
        case lastPoll = "last_poll", retryAt = "retry_at"
    }
}

public struct Listener: Decodable, Sendable, Equatable {
    public var connected: Bool?
    public var heartbeat: Double?
    public var lastEventAt: Double?

    enum CodingKeys: String, CodingKey {
        case connected, heartbeat
        case lastEventAt = "last_event_at"
    }
}

public struct Event: Decodable, Sendable, Equatable, Identifiable {
    public var account: String
    public var itemId: String
    public var source: String
    public var kind: String
    public var decision: String
    public var reason: String
    public var summary: String
    public var created: String
    public var title: String
    public var from: String
    public var place: String
    public var url: String
    public var jobId: Int?
    public var jobStatus: String?
    public var sessionId: String?
    public var canOpen: Bool

    public var id: String { account + "\u{1}" + itemId }

    enum CodingKeys: String, CodingKey {
        case account, source, kind, decision, reason, summary, created, title, from, place, url
        case itemId = "item_id", jobId = "job_id", jobStatus = "job_status", sessionId = "session_id"
        case canOpen = "can_open"
    }
}

public enum Decode {
    public static func status(_ data: Data) throws -> Status { try JSONDecoder().decode(Status.self, from: data) }
    public static func events(_ data: Data) throws -> [Event] { try JSONDecoder().decode([Event].self, from: data) }
}
