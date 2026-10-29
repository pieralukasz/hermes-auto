import Foundation

/// The last good `status` and `events`, each with its own refresh error.
public struct OverviewSnapshot: Equatable, Sendable {
    public private(set) var status: Status?
    public private(set) var events: [Event] = []
    public private(set) var statusError: String?
    public private(set) var eventsError: String?

    public init() {}

    @discardableResult
    public mutating func acceptStatus(_ result: CommandResult) -> Bool {
        guard result.exitCode == 0 else {
            statusError = result.message
            return false
        }
        do { status = try Decode.status(result.stdout) }
        catch {
            statusError = "Cannot read the `hermes-auto status` output: \(error.localizedDescription)"
            return false
        }
        statusError = nil
        return true
    }

    public mutating func acceptEvents(_ result: CommandResult) {
        guard result.exitCode == 0 else {
            eventsError = "Event history unavailable: \(result.message)"
            return
        }
        do {
            events = try Decode.events(result.stdout)
            eventsError = nil
        } catch { eventsError = "Cannot read the event history: \(error.localizedDescription)" }
    }

    public func overall(dismissed: Set<String>) -> Overall {
        Summary.overall(status, error: statusError, dismissed: dismissed)
    }
}
