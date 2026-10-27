import AppKit
import HermesAutoCore
import SwiftUI

@main
struct HermesAutoBarApp: App {
    @State private var model: Model
    private let showWindow: Bool

    init() {
        let environment = ProcessInfo.processInfo.environment
        let fixture = environment["HERMES_AUTO_BAR_FIXTURE"].map { URL(fileURLWithPath: $0) }
        let runner: CommandRunner = fixture.map { FixtureRunner(directory: $0) } ?? CLIRunner(executable: CLIRunner.defaultExecutable())
        let model = Model(runner: runner, isFixture: fixture != nil)
        _model = State(initialValue: model)
        showWindow = CommandLine.arguments.contains("--window")
        model.start()
    }

    var body: some Scene {
        MenuBarExtra {
            ContentView(model: model, inWindow: false)
        } label: {
            MenuBarLabel(overall: model.overall)
        }
        .menuBarExtraStyle(.window)

        Window("Hermes Auto", id: "main") {
            ContentView(model: model, inWindow: true)
        }
        .defaultLaunchBehavior(showWindow ? .presented : .suppressed)
        .defaultSize(width: 480, height: 640)
        .windowResizability(.contentMinSize)
    }
}

/// Menu bar: one template symbol per state, plus the number of problems when there are any.
struct MenuBarLabel: View {
    var overall: Overall

    var body: some View {
        switch overall {
        case .attention(let count):
            Label("\(count)", systemImage: "exclamationmark.triangle")
                .labelStyle(.titleAndIcon)
        case .ok: Image(systemName: "bolt.horizontal.circle")
        case .working: Image(systemName: "bolt.horizontal.circle.fill")
        case .paused: Image(systemName: "pause.circle")
        case .unavailable: Image(systemName: "bolt.slash")
        }
    }
}
