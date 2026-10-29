import Foundation

public struct Connection: Identifiable, Equatable, Sendable {
    public enum State: Sendable { case read, warning, unknown }
    public let source: String
    public let channel: String?
    public let health: SourceHealth
    public var id: String { source + ":" + (channel ?? "") }
    public var title: String { channel ?? sourceLabel(source) }
    public var state: State {
        if health.retryAt != nil || health.listener?.connected == false { return .warning }
        return channel != nil || health.lastPoll == nil ? .unknown : .read
    }
    public var statusText: String {
        if health.retryAt != nil { return source == "matrix" ? "Matrix read error" : "Read error" }
        if health.listener?.connected == false { return "Listener disconnected" }
        return channel != nil ? "Bridge status unknown" : health.lastPoll == nil ? "Not read yet" : "Read confirmed"
    }
    public var path: [String] {
        if let channel { return [channel, "bridge", "Matrix", "Hermes Auto"] }
        return [title, "Hermes Auto"]
    }
}

public struct ConnectionGroup: Identifiable, Sendable {
    public let title: String
    public let items: [Connection]
    public var id: String { title }
}

public enum Connections {
    public static func groups(_ status: Status) -> [ConnectionGroup] {
        let sources = status.health?.sources ?? [:]
        let definitions: [(String, Set<String>)] = [
            ("Messages", ["imessage", "matrix", "slack"]),
            ("Mail", ["gmail", "proton"]),
            ("Tasks & projects", ["todoist", "github", "linear"])
        ]
        let known = definitions.reduce(into: Set<String>()) { $0.formUnion($1.1) }
        return (definitions + [("Other", Set(sources.keys).subtracting(known))]).compactMap { title, names in
            let items = sources.filter { names.contains($0.key) }.flatMap { source, health -> [Connection] in
                let channels = source == "matrix" ? Set(health.channels ?? []).filter { !$0.isEmpty }.sorted() : []
                return channels.isEmpty ? [Connection(source: source, channel: nil, health: health)]
                    : channels.map { Connection(source: source, channel: $0, health: health) }
            }.sorted { $0.title.localizedStandardCompare($1.title) == .orderedAscending }
            return items.isEmpty ? nil : ConnectionGroup(title: title, items: items)
        }
    }
}
