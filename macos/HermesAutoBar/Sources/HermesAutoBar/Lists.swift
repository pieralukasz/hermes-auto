import HermesAutoCore
import SwiftUI

struct EventsList: View {
    @Bindable var model: Model
    @Binding var filter: EventFilter

    var body: some View {
        let shown = SourceScope.events(model.events, filter: filter, source: model.sourceFilter)
        VStack(spacing: 0) {
            HStack {
                Picker("Pokaż", selection: $filter) {
                    ForEach(EventFilter.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.menu)
                .fixedSize()
                Spacer()
                SourceScopeBar(model: model)
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 4)
            if shown.isEmpty {
                Empty(text: model.sourceFilter.map { "Brak zdarzeń z \(sourceLabel($0)) w tym widoku" }
                      ?? "Brak zdarzeń w tym widoku")
            } else {
                List {
                    ForEach(Times.grouped(shown, now: model.now), id: \.0) { day, events in
                        Section(day) {
                            ForEach(events) { EventRow(event: $0, model: model) }
                        }
                    }
                }
                .listStyle(.inset)
            }
        }
    }
}

struct EventRow: View {
    var event: Event
    @Bindable var model: Model
    @State private var expanded: Bool

    init(event: Event, model: Model) {
        self.event = event
        self.model = model
        _expanded = State(initialValue: Preview.expand == event.itemId)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: sourceSymbol(event.source)).foregroundStyle(.secondary).frame(width: 16)
                Text(event.title.isEmpty ? "(bez tytułu)" : event.title).lineLimit(expanded ? 3 : 1)
                Spacer(minLength: 4)
                DecisionTag(decision: event.decision)
                Text(Times.ago(Times.parse(event.created), now: model.now))
                    .font(.caption).foregroundStyle(.secondary).monospacedDigit()
            }
            let who = [event.from, event.place].filter { !$0.isEmpty }.joined(separator: " · ")
            if !who.isEmpty {
                Text(who).font(.caption).foregroundStyle(.secondary).lineLimit(1).padding(.leading, 22)
            }
            if expanded {
                VStack(alignment: .leading, spacing: 4) {
                    if !event.summary.isEmpty { Text(event.summary).font(.callout) }
                    if !event.reason.isEmpty {
                        Text("Dlaczego: \(event.reason)").font(.caption).foregroundStyle(.secondary)
                    }
                    if let status = event.jobStatus {
                        Text("Sesja: \(jobStatusLabel(status))").font(.caption).foregroundStyle(.secondary)
                    }
                    HStack(spacing: 8) {
                        if event.sessionId != nil {
                            Button("Otwórz sesję") { model.openSession(event.sessionId) }
                        }
                        if sourceURL(event.url) != nil {
                            Button("Otwórz w \(sourceLabel(event.source))") { model.openSource(event.url) }
                        }
                        if event.canOpen {
                            Button("Daj temu sesję") {
                                model.perform(.openItem(account: event.account, itemId: event.itemId), label: event.id)
                            }
                            .help("Sortownik dał „\(decisionLabel(event.decision))”. Otwiera sesję tak, jakby wybrał „Sesja”.")
                            .disabled(model.busyAction != nil)
                        }
                    }
                    .controlSize(.small)
                    .padding(.top, 2)
                }
                .textSelection(.enabled)
                .padding(.leading, 22)
            }
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .onTapGesture { withAnimation(.snappy(duration: 0.2)) { expanded.toggle() } }
    }
}

struct DecisionTag: View {
    var decision: String

    var body: some View {
        Text(decisionLabel(decision))
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .foregroundStyle(color)
            .background(color.opacity(0.14), in: Capsule())
    }

    private var color: Color {
        switch decision {
        case "session", "followup": return .blue
        case "brief": return .purple
        case "skip", "read", "baseline": return .secondary
        case "needs_attention": return .orange
        default: return .teal
        }
    }
}

struct JobsList: View {
    @Bindable var model: Model

    var body: some View {
        let jobs = Array(SourceScope.jobs(model.recentJobs, source: model.sourceFilter).prefix(150))
        VStack(spacing: 0) {
            if model.sourceFilter != nil {
                SourceScopeBar(model: model)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 4)
            }
            sessions(jobs)
        }
    }

    @ViewBuilder
    private func sessions(_ jobs: [Job]) -> some View {
        if jobs.isEmpty {
            Empty(text: model.sourceFilter.map { "Brak sesji z \(sourceLabel($0))" }
                  ?? "Automat nie otworzył jeszcze żadnej sesji")
        } else {
            List(jobs) { job in
                HStack(spacing: 6) {
                    Image(systemName: sourceSymbol(job.source)).foregroundStyle(.secondary).frame(width: 16)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(job.title).lineLimit(1)
                        Text("\(sourceLabel(job.source)) · #\(job.id) · \(jobStatusLabel(job.status))")
                            .font(.caption).foregroundStyle(job.status == "needs_attention" ? .orange : .secondary)
                    }
                    Spacer(minLength: 4)
                    if Summary.activeStatuses.contains(job.status) { ProgressView().controlSize(.mini) }
                    Text(Times.ago(Times.parse(job.created), now: model.now))
                        .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                    Button { model.openSession(job.sessionId) } label: { Image(systemName: "arrow.up.forward.app") }
                        .buttonStyle(.borderless)
                        .help("Otwórz sesję w Hermesie")
                }
                .padding(.vertical, 1)
            }
            .listStyle(.inset)
        }
    }
}

struct ProblemsList: View {
    @Bindable var model: Model

    var body: some View {
        let problems = model.openProblems
        if problems.isEmpty {
            Empty(text: model.dismissed.isEmpty ? "Nic nie wymaga uwagi"
                  : "Nic nie wymaga uwagi (odłożone: \(model.dismissed.count))")
        } else {
            List(problems) { problem in
                VStack(alignment: .leading, spacing: 4) {
                    Text(problem.title.isEmpty ? "(bez tytułu)" : problem.title).lineLimit(2)
                    if !problem.detail.isEmpty {
                        Text(problem.detail).font(.caption).foregroundStyle(.secondary).lineLimit(4)
                            .textSelection(.enabled)
                    }
                    HStack(spacing: 8) {
                        actions(problem)
                        Spacer()
                        Button("Odłóż") { model.dismiss(problem) }
                            .help("Ukryj, dopóki ten problem się nie zmieni (tylko w tej aplikacji)")
                    }
                    .controlSize(.small)
                    .disabled(model.busyAction != nil)
                }
                .padding(.vertical, 3)
            }
            .listStyle(.inset)
        }
    }

    @ViewBuilder
    private func actions(_ problem: Problem) -> some View {
        switch problem.kind {
        case .job(let id):
            if problem.sessionId != nil { Button("Otwórz sesję") { model.openSession(problem.sessionId) } }
            Button("Ponów") { model.perform(.retry(job: id), label: problem.key) }
                .help("Kontynuuj w tej samej sesji (najpierw sprawdź, czy wynik już tam jest)")
            Button("Nowa sesja") { model.perform(.newSession(job: id), label: problem.key) }
                .help("Zacznij od nowa w osobnej rozmowie")
        case .followup(let id):
            if problem.sessionId != nil { Button("Otwórz sesję") { model.openSession(problem.sessionId) } }
            Button("Ponów odpowiedź") { model.perform(.retryFollowup(id), label: problem.key) }
                .help("Otwiera nową sesję „↩” z tą odpowiedzią")
        case .sourceError, .pending:
            Button("Log") { model.openLog() }
        case .triageIssue:
            EmptyView()
        }
    }
}

struct Empty: View {
    var text: String

    var body: some View {
        Text(text).foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity).padding(24)
    }
}
