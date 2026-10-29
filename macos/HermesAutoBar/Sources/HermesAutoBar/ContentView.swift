import HermesAutoCore
import SwiftUI

enum Tab: String, CaseIterable, Identifiable {
    case overview = "Status", events = "History", sessions = "Sessions", problems = "Issues"
    var id: String { rawValue }
}

enum Preview {
    static let environment = ProcessInfo.processInfo.environment
    static let enabled = environment["HERMES_AUTO_BAR_FIXTURE"] != nil
    static let tab: Tab? = enabled ? environment["HERMES_AUTO_BAR_TAB"].flatMap { name in
        Tab.allCases.first { "\($0)" == name }
    } : nil
    static let expand: String? = enabled ? environment["HERMES_AUTO_BAR_EXPAND"] : nil
    static let source: String? = enabled ? environment["HERMES_AUTO_BAR_SOURCE"] : nil
}

struct ContentView: View {
    @Bindable var model: Model
    var inWindow: Bool
    @Environment(\.openWindow) private var openWindow
    @State private var tab: Tab = Preview.tab ?? .overview
    @State private var filter: EventFilter = .all

    var body: some View {
        VStack(spacing: 0) {
            header
            if model.isFixture {
                Text("Sample data. Your automation is unchanged.")
                    .font(.caption).foregroundStyle(Color.secondaryText).padding(.bottom, 8)
            }
            Divider()
            if let error = model.loadError, model.status != nil {
                Label("Status refresh failed. Showing the last known state.\n\(error)", systemImage: "exclamationmark.triangle")
                    .font(.callout).foregroundStyle(.primary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                    .background(Color.orange.opacity(0.1))
            }
            if let error = model.eventsError, model.status != nil, model.loadError == nil, tab == .events {
                Label("History refresh failed. Showing the last known events.\n\(error)", systemImage: "exclamationmark.triangle")
                    .font(.callout).foregroundStyle(.primary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                    .background(Color.orange.opacity(0.1))
            }
            if model.status != nil {
                Picker("View", selection: $tab) {
                    ForEach(Tab.allCases) { item in
                        Text(item == .problems && !model.openProblems.isEmpty
                             ? "Issues (\(model.openProblems.count))" : item.rawValue).tag(item)
                    }
                }
                .pickerStyle(.segmented).labelsHidden().padding(12)
                switch tab {
                case .overview:
                    OverviewView(model: model, tab: $tab, filter: $filter)
                case .events: EventsList(model: model, filter: $filter)
                case .sessions: JobsList(model: model)
                case .problems: ProblemsList(model: model)
                }
            } else if let error = model.loadError {
                Unavailable(message: error)
            } else {
                VStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text("Checking automation status…").foregroundStyle(Color.secondaryText)
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            if let notice = model.notice {
                Divider()
                Label(notice, systemImage: model.noticeIsError ? "exclamationmark.triangle" : "info.circle")
                    .font(.callout).frame(maxWidth: .infinity, alignment: .leading).padding(12)
            }
        }
        .frame(minWidth: 460, idealWidth: 480, minHeight: 580, idealHeight: 680)
        .task { await model.refresh() }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 10) {
            OverallBadge(overall: model.overall)
            VStack(alignment: .leading, spacing: 3) {
                Text("Hermes Auto").font(.headline)
                Text(subtitle).font(.callout)
                if let last = model.status?.health?.lastCheck {
                    Text("Last check: \(relativeTime(last, now: model.now))")
                        .font(.caption).foregroundStyle(Color.secondaryText)
                }
            }
            Spacer(minLength: 4)
            if model.busyAction != nil { ProgressView().controlSize(.small) }
            if let status = model.status {
                Button {
                    model.perform(status.paused ? .resume : .pause, label: "pause")
                } label: {
                    Image(systemName: status.paused ? "play" : "pause").frame(width: 24, height: 24)
                }
                .accessibilityLabel(status.paused ? "Resume automation" : "Pause automation")
                .help(status.paused ? "Resume automation" : "Pause new sessions. Existing sessions keep running.")
                .disabled(model.busyAction != nil)
            }
            Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise").frame(width: 24, height: 24) }
                .accessibilityLabel("Refresh status").help("Refresh status")
            Menu {
                if !inWindow {
                    Button("Open in Window") { openWindow(id: "main"); NSApp.activate() }
                }
                Button("Open Automation Log") { model.openLog() }
                if !model.dismissed.isEmpty {
                    Button("Restore Dismissed Issues (\(model.dismissed.count))") { model.restoreDismissed() }
                }
                Divider()
                Button("Quit Hermes Auto Bar") { NSApp.terminate(nil) }
            } label: { Image(systemName: "ellipsis").frame(width: 24, height: 24) }
                .menuIndicator(.hidden).fixedSize().accessibilityLabel("More options")
        }
        .buttonStyle(.borderless).controlSize(.regular).padding(16)
    }

    private var subtitle: String {
        guard model.status != nil || model.loadError != nil else { return "Connecting to automation" }
        switch model.overall {
        case .ok: return "No issues reported"
        case .paused: return "Automation paused"
        case .working(let count): return "In progress: \(count)"
        case .attention(let count): return "Needs attention: \(count)"
        case .unavailable: return "Status outdated or unavailable"
        }
    }
}

struct OverallBadge: View {
    var overall: Overall
    var body: some View {
        Image(systemName: symbol(overall)).font(.title2)
            .foregroundStyle(color).frame(width: 28).accessibilityHidden(true)
    }
    private var color: Color {
        switch overall {
        case .ok: return .secondary
        case .working: return .accentColor
        case .paused: return .secondary
        case .attention: return .orange
        case .unavailable: return .red
        }
    }
}

func symbol(_ overall: Overall) -> String {
    switch overall {
    case .ok: return "checkmark.circle"
    case .working: return "arrow.triangle.2.circlepath"
    case .paused: return "pause.circle"
    case .attention: return "exclamationmark.triangle"
    case .unavailable: return "questionmark.circle"
    }
}

func relativeTime(_ value: Double?, now: Date) -> String {
    guard let value else { return "not available" }
    let text = Times.ago(Date(timeIntervalSince1970: value), now: now)
    return text == "now" ? "just now" : "\(text) ago"
}

struct RelayDetails: View {
    var relay: Listener
    var now: Date
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            LabeledContent("Connection", value: relay.connected.map { $0 ? "connected" : "disconnected" } ?? "unknown")
            LabeledContent("Last event", value: relativeTime(relay.lastEventAt, now: now))
            LabeledContent("Last heartbeat", value: relativeTime(relay.heartbeat, now: now))
        }.font(.callout)
    }
}

struct SourceScopeBar: View {
    @Bindable var model: Model
    var includesChannel = true
    var body: some View {
        if let source = model.sourceFilter {
            HStack(spacing: 6) {
                Text("Only \((includesChannel ? model.channelFilter : nil) ?? sourceLabel(source))").font(.caption.weight(.medium))
                Button { model.sourceFilter = nil; model.channelFilter = nil } label: {
                    Image(systemName: "xmark.circle.fill")
                }
                .buttonStyle(.borderless).help("Show all sources")
                .accessibilityLabel("Clear source filter")
            }
        }
    }
}

struct Unavailable: View {
    var message: String
    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "questionmark.circle").font(.largeTitle).foregroundStyle(Color.secondaryText)
            Text("Automation status is unavailable").font(.headline)
            Text(message).font(.callout).foregroundStyle(Color.secondaryText).multilineTextAlignment(.center)
                .textSelection(.enabled)
            Text("Try the refresh button above.").font(.callout)
        }
        .padding(24).frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
