import HermesAutoCore
import SwiftUI

enum Tab: String, CaseIterable, Identifiable {
    case events = "Zdarzenia", sessions = "Sesje", problems = "Problemy"
    var id: String { rawValue }
}

/// Screenshot scenes, honoured only with sample data (`HERMES_AUTO_BAR_FIXTURE`):
/// `HERMES_AUTO_BAR_TAB=events|sessions|problems`, `HERMES_AUTO_BAR_EXPAND=<item id>`.
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
    @State private var tab: Tab = Preview.tab ?? .events
    @State private var filter: EventFilter = .all

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if let status = model.status {
                SourcesStrip(status: status, now: model.now, model: model)
                Divider()
                Picker("", selection: $tab) {
                    ForEach(Tab.allCases) { tab in
                        Text(tab == .problems && !model.openProblems.isEmpty
                             ? "Problemy (\(model.openProblems.count))" : tab.rawValue).tag(tab)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                switch tab {
                case .events: EventsList(model: model, filter: $filter)
                case .sessions: JobsList(model: model)
                case .problems: ProblemsList(model: model)
                }
            } else {
                Unavailable(message: model.loadError ?? "Wczytuję…")
            }
            if let notice = model.notice {
                Divider()
                Text(notice)
                    .font(.callout)
                    .foregroundStyle(model.noticeIsError ? .red : .secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
            }
        }
        .frame(minWidth: 440, idealWidth: 460, minHeight: 520, idealHeight: 620)
        .onChange(of: model.sourceFilter) { _, source in
            // Problems are not per source; a chip click means "show me what came from there".
            if source != nil && tab == .problems { tab = .events }
        }
        .task { await model.refresh() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            OverallBadge(overall: model.overall)
            VStack(alignment: .leading, spacing: 1) {
                Text("Hermes Auto").font(.headline)
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if model.busyAction != nil { ProgressView().controlSize(.small) }
            if let status = model.status {
                Button {
                    model.perform(status.paused ? .resume : .pause, label: "pause")
                } label: {
                    Label(status.paused ? "Wznów" : "Wstrzymaj", systemImage: status.paused ? "play.fill" : "pause.fill")
                }
                .help(status.paused ? "Wznów sprawdzanie źródeł i przygotowanie sesji"
                      : "Wstrzymaj automat: bez nowych sesji, do wznowienia")
                .disabled(model.busyAction != nil)
            }
            Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                .help("Odśwież")
            Menu {
                if !inWindow {
                    Button("Otwórz w oknie") {
                        openWindow(id: "main")
                        NSApp.activate()
                    }
                }
                Button("Pokaż log automatu") { model.openLog() }
                if !model.dismissed.isEmpty {
                    Button("Przywróć odłożone problemy (\(model.dismissed.count))") { model.restoreDismissed() }
                }
                Divider()
                Button("Zakończ Hermes Auto Bar") { NSApp.terminate(nil) }
            } label: { Image(systemName: "ellipsis.circle") }
                .menuIndicator(.hidden)
                .fixedSize()
        }
        .buttonStyle(.borderless)
        .padding(12)
    }

    private var subtitle: String {
        var parts: [String] = []
        switch model.overall {
        case .ok: parts.append("Wszystko działa")
        case .paused: parts.append("Wstrzymany")
        case .working(let n): parts.append("Pracuje: \(n)")
        case .attention(let n): parts.append("Wymaga uwagi: \(n)")
        case .unavailable: parts.append("Brak połączenia z CLI")
        }
        if let last = model.status?.health?.lastCheck {
            parts.append("ostatnie sprawdzenie \(Times.ago(Date(timeIntervalSince1970: last), now: model.now)) temu"
                .replacingOccurrences(of: "teraz temu", with: "przed chwilą"))
        }
        if model.isFixture { parts.append("dane przykładowe") }
        return parts.joined(separator: " · ")
    }
}

struct OverallBadge: View {
    var overall: Overall

    var body: some View {
        Image(systemName: symbol(overall))
            .font(.title2)
            .foregroundStyle(color)
            .frame(width: 28)
    }

    private var color: Color {
        switch overall {
        case .ok: return .green
        case .working: return .blue
        case .paused: return .secondary
        case .attention: return .orange
        case .unavailable: return .red
        }
    }
}

func symbol(_ overall: Overall) -> String {
    switch overall {
    case .ok: return "checkmark.circle.fill"
    case .working: return "arrow.triangle.2.circlepath.circle.fill"
    case .paused: return "pause.circle.fill"
    case .attention: return "exclamationmark.triangle.fill"
    case .unavailable: return "xmark.octagon.fill"
    }
}

/// Source chips: last successful read per source. A click shows only that source's events and sessions;
/// a second click (or the chip in the list header) shows everything again. ZimaBlade opens relay details.
struct SourcesStrip: View {
    var status: Status
    var now: Date
    @Bindable var model: Model
    @State private var relayDetails = false

    var body: some View {
        let sources = (status.health?.sources ?? [:]).sorted { $0.key < $1.key }
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                if let relay = status.health?.relay {
                    Button { relayDetails.toggle() } label: {
                        chip(symbol: "antenna.radiowaves.left.and.right", label: "ZimaBlade",
                             detail: relay.connected == true ? "połączony" : "rozłączony",
                             bad: relay.connected != true, selected: false)
                    }
                    .help("Relay zdarzeń z ZimaBlade (Slack, GitHub, webhooki). Kliknij po szczegóły.")
                    .popover(isPresented: $relayDetails, arrowEdge: .bottom) { RelayDetails(relay: relay, now: now) }
                }
                ForEach(sources, id: \.key) { name, health in
                    let failing = health.retryAt.map { $0 > now.timeIntervalSince1970 } ?? false
                    let selected = model.sourceFilter == name
                    Button {
                        withAnimation(.snappy(duration: 0.15)) {
                            model.sourceFilter = SourceScope.toggle(model.sourceFilter, name)
                        }
                    } label: {
                        chip(symbol: sourceSymbol(name), label: sourceLabel(name),
                             detail: failing ? "błąd odczytu"
                                 : Times.ago(health.lastPoll.map { Date(timeIntervalSince1970: $0) }, now: now),
                             bad: failing, selected: selected)
                    }
                    .help(selected ? "Pokaż wszystkie źródła"
                          : "Pokaż tylko \(sourceLabel(name))" + (failing
                              ? ". Odczyt się nie udał; ponowienie samo, z rosnącym odstępem" : ""))
                    .accessibilityAddTraits(selected ? .isSelected : [])
                }
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
    }

    private func chip(symbol: String, label: String, detail: String, bad: Bool, selected: Bool) -> some View {
        HStack(spacing: 4) {
            Image(systemName: symbol).imageScale(.small)
            Text(label).font(.caption.weight(.medium))
            Text(detail).font(.caption)
                .foregroundStyle(selected ? AnyShapeStyle(.white.opacity(0.85)) : bad ? AnyShapeStyle(.red)
                                 : AnyShapeStyle(.secondary))
        }
        .foregroundStyle(selected ? AnyShapeStyle(.white) : AnyShapeStyle(.primary))
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(selected ? AnyShapeStyle(Color.accentColor)
                    : bad ? AnyShapeStyle(.red.opacity(0.12)) : AnyShapeStyle(.quaternary.opacity(0.6)),
                    in: Capsule())
        .contentShape(Capsule())
    }
}

struct RelayDetails: View {
    var relay: Listener
    var now: Date

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
            GridRow { Text("Połączenie").foregroundStyle(.secondary)
                Text(relay.connected == true ? "połączony" : "rozłączony") }
            GridRow { Text("Ostatnie zdarzenie").foregroundStyle(.secondary)
                Text(ago(relay.lastEventAt)) }
            GridRow { Text("Sygnał życia").foregroundStyle(.secondary)
                Text(ago(relay.heartbeat)) }
        }
        .font(.callout)
        .padding(12)
    }

    private func ago(_ value: Double?) -> String {
        guard let value else { return "–" }
        let text = Times.ago(Date(timeIntervalSince1970: value), now: now)
        return text == "teraz" ? "przed chwilą" : "\(text) temu"
    }
}

/// Shown above a list while a source chip is selected; the x clears it.
struct SourceScopeBar: View {
    @Bindable var model: Model

    var body: some View {
        if let source = model.sourceFilter {
            HStack(spacing: 6) {
                Image(systemName: sourceSymbol(source)).imageScale(.small)
                Text("Tylko \(sourceLabel(source))").font(.caption.weight(.medium))
                Button { withAnimation(.snappy(duration: 0.15)) { model.sourceFilter = nil } } label: {
                    Image(systemName: "xmark.circle.fill")
                }
                .buttonStyle(.borderless)
                .help("Pokaż wszystkie źródła")
            }
            .foregroundStyle(Color.accentColor)
        }
    }
}

struct Unavailable: View {
    var message: String

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "bolt.horizontal.circle").font(.largeTitle).foregroundStyle(.secondary)
            Text("Nie mam danych z hermes-auto").font(.headline)
            Text(message).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .textSelection(.enabled)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
