import Foundation

/// A change the user asks for; each maps to one explicit `hermes-auto` command.
public enum Action: Equatable, Sendable {
    case pause, resume
    case retry(job: Int)
    case newSession(job: Int)
    case retryFollowup(Int)
    case openItem(account: String, itemId: String)

    public var arguments: [String] {
        switch self {
        case .pause: return ["pause"]
        case .resume: return ["resume"]
        case .retry(let job): return ["retry", String(job)]
        case .newSession(let job): return ["new-session", String(job)]
        case .retryFollowup(let id): return ["retry-followup", String(id)]
        case .openItem(let account, let itemId): return ["open-item", account, itemId]
        }
    }

    /// What the user reads after it worked.
    public var done: String {
        switch self {
        case .pause: return "Automat wstrzymany"
        case .resume: return "Automat wznowiony"
        case .retry, .newSession, .retryFollowup, .openItem: return "W kolejce: sesja powstanie w ciągu minuty"
        }
    }
}

public struct CommandResult: Sendable, Equatable {
    public var exitCode: Int32
    public var stdout: Data
    public var stderr: String

    public init(exitCode: Int32, stdout: Data, stderr: String) {
        self.exitCode = exitCode
        self.stdout = stdout
        self.stderr = stderr
    }

    /// The CLI holds a lock while a check runs; such a refusal is worth retrying in a few seconds.
    public var isBusy: Bool { exitCode != 0 && stderr.contains("Another hermes-auto operation is running") }

    public var message: String {
        let text = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleaned = text.hasPrefix("hermes-auto: ") ? String(text.dropFirst("hermes-auto: ".count)) : text
        return cleaned.isEmpty ? "hermes-auto zakończył się kodem \(exitCode)" : cleaned
    }
}

public protocol CommandRunner: Sendable {
    func run(_ arguments: [String]) async -> CommandResult
}

/// Runs the installed CLI. The app is started by launchd/Finder without the shell PATH, so the path is absolute.
public struct CLIRunner: CommandRunner {
    public var executable: URL
    public var timeout: TimeInterval

    public init(executable: URL, timeout: TimeInterval = 60) {
        self.executable = executable
        self.timeout = timeout
    }

    public static func defaultExecutable() -> URL {
        if let custom = UserDefaults.standard.string(forKey: "cliPath"), !custom.isEmpty {
            return URL(fileURLWithPath: (custom as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appending(path: ".local/bin/hermes-auto")
    }

    public func run(_ arguments: [String]) async -> CommandResult {
        let executable = self.executable
        let timeout = self.timeout
        return await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                continuation.resume(returning: CLIRunner.runSync(executable, arguments, timeout: timeout))
            }
        }
    }

    static func runSync(_ executable: URL, _ arguments: [String], timeout: TimeInterval) -> CommandResult {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        do {
            try process.run()
        } catch {
            return CommandResult(exitCode: 127, stdout: Data(), stderr: "Nie mogę uruchomić \(executable.path): \(error.localizedDescription)")
        }
        // Read before waiting: a full pipe would block the child forever.
        let group = DispatchGroup()
        let stdout = DataBox(), stderr = DataBox()
        group.enter()
        DispatchQueue.global().async { stdout.data = out.fileHandleForReading.readDataToEndOfFile(); group.leave() }
        group.enter()
        DispatchQueue.global().async { stderr.data = err.fileHandleForReading.readDataToEndOfFile(); group.leave() }
        if group.wait(timeout: .now() + timeout) == .timedOut {
            // One deadline for the whole run: SIGTERM, a short grace, then SIGKILL; a child that inherited
            // the pipe can keep it open, so the readers are not waited for without a limit either.
            process.terminate()
            if group.wait(timeout: .now() + 2) == .timedOut, process.isRunning {
                kill(process.processIdentifier, SIGKILL)
                _ = group.wait(timeout: .now() + 2)
            }
            return CommandResult(exitCode: 124, stdout: Data(), stderr: "hermes-auto nie odpowiedział w \(Int(timeout)) s")
        }
        // Both pipes closed; the process may still linger (closed its output but did not exit).
        let exited = DispatchSemaphore(value: 0)
        DispatchQueue.global().async { process.waitUntilExit(); exited.signal() }
        if exited.wait(timeout: .now() + 5) == .timedOut {
            process.terminate()
            return CommandResult(exitCode: 124, stdout: Data(), stderr: "hermes-auto nie zakończył się po wyjściu")
        }
        return CommandResult(exitCode: process.terminationStatus, stdout: stdout.data,
                             stderr: String(decoding: stderr.data, as: UTF8.self))
    }
}

/// Each pipe is read on its own queue and read back only after `group.wait()`.
private final class DataBox: @unchecked Sendable {
    var data = Data()
}

/// Sample data from a directory (`status.json`, `events.json`); actions only report what they would run.
/// Used for screenshots and UI checks without touching the real automation.
public struct FixtureRunner: CommandRunner {
    public var directory: URL

    public init(directory: URL) { self.directory = directory }

    public func run(_ arguments: [String]) async -> CommandResult {
        let name: String
        switch arguments.first {
        case "status": name = "status.json"
        case "events": name = "events.json"
        default:
            return CommandResult(exitCode: 0, stdout: Data("{}".utf8), stderr: "")
        }
        guard let data = try? Data(contentsOf: directory.appending(path: name)) else {
            return CommandResult(exitCode: 1, stdout: Data(), stderr: "Brak \(name) w \(directory.path)")
        }
        return CommandResult(exitCode: 0, stdout: data, stderr: "")
    }
}
