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
                SourcesStrip(status: status, now: model.now)
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

struct SourcesStrip: View {
    var status: Status
    var now: Date

    var body: some View {
        let sources = (status.health?.sources ?? [:]).sorted { $0.key < $1.key }
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                if let relay = status.health?.relay {
                    chip(symbol: "antenna.radiowaves.left.and.right", label: "ZimaBlade",
                         detail: relay.connected == true ? "połączony" : "rozłączony",
                         bad: relay.connected != true,
                         help: "Relay zdarzeń z ZimaBlade (Slack, GitHub, webhooki)")
                }
                ForEach(sources, id: \.key) { name, health in
                    let failing = health.retryAt.map { $0 > now.timeIntervalSince1970 } ?? false
                    chip(symbol: sourceSymbol(name), label: sourceLabel(name),
                         detail: failing ? "błąd odczytu" : Times.ago(health.lastPoll.map { Date(timeIntervalSince1970: $0) }, now: now),
                         bad: failing,
                         help: failing ? "Odczyt się nie udał; ponowienie samo, z rosnącym odstępem"
                             : "Ostatni udany odczyt źródła")
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
    }

    private func chip(symbol: String, label: String, detail: String, bad: Bool, help: String) -> some View {
        HStack(spacing: 4) {
            Image(systemName: symbol).imageScale(.small)
            Text(label).font(.caption.weight(.medium))
            Text(detail).font(.caption).foregroundStyle(bad ? .red : .secondary)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(bad ? AnyShapeStyle(.red.opacity(0.12)) : AnyShapeStyle(.quaternary.opacity(0.6)), in: Capsule())
        .help(help)
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
