import Foundation

public struct HistorySource: Hashable, Sendable {
    public let source: String?
    public let channel: String?
    public static let all = HistorySource(source: nil)

    public init(source: String?, channel: String? = nil) {
        self.source = source
        self.channel = channel
    }

    public var label: String {
        guard let source else { return "All sources" }
        return channel ?? (source == "matrix" ? "Matrix (all)" : sourceLabel(source))
    }

    public static func choices(status: Status?, events: [Event], including selection: HistorySource = .all) -> [HistorySource] {
        let sources = status?.health?.sources ?? [:]
        var result = Set(sources.keys.map { HistorySource(source: $0) })
        result.insert(selection)
        for event in events where !event.source.isEmpty {
            result.insert(HistorySource(source: event.source))
            if event.source == "matrix", let channel = event.channel, !channel.isEmpty {
                result.insert(HistorySource(source: "matrix", channel: channel))
            }
        }
        for channel in sources["matrix"]?.channels ?? [] where !channel.isEmpty {
            result.insert(HistorySource(source: "matrix", channel: channel))
        }
        result.remove(.all)
        return [.all] + result.sorted { $0.label.localizedStandardCompare($1.label) == .orderedAscending }
    }
}
