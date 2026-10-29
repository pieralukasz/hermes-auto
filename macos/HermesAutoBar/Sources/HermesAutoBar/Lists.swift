import HermesAutoCore
import SwiftUI

struct EventsList: View {
    @Bindable var model: Model
    @Binding var filter: EventFilter

    var body: some View {
        let shown = SourceScope.events(model.events, filter: filter, source: model.sourceFilter, channel: model.channelFilter)
        VStack(spacing: 0) {
            HStack {
                Picker("Show", selection: $filter) {
                    ForEach(EventFilter.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.menu)
                .frame(maxWidth: .infinity, alignment: .leading)
                HistorySourcePicker(model: model)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 4)
            if shown.isEmpty {
                Empty(text: model.sourceFilter.map { "No events from \(model.channelFilter ?? sourceLabel($0)) match this filter" }
                      ?? "No events match this filter")
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
            Button { expanded.toggle() } label: {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.caption2).foregroundStyle(Color.secondaryText).frame(width: 12)
                    Text(event.title.isEmpty ? "(untitled)" : event.title).lineLimit(expanded ? 3 : 1)
                    Spacer(minLength: 4)
                    DecisionTag(decision: event.decision)
                    Text(Times.ago(Times.parse(event.created), now: model.now))
                        .font(.caption).foregroundStyle(Color.secondaryText).monospacedDigit()
                }.contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(expanded ? "Collapse" : "Expand"): \(event.title)")
            Text("\(event.sourceName) → \(decisionLabel(event.decision))"
                 + (event.jobStatus.map { " · \(jobStatusLabel($0))" } ?? ""))
                .font(.caption).foregroundStyle(Color.secondaryText).padding(.leading, 18)
            let who = [event.from, event.place].filter { !$0.isEmpty }.joined(separator: " · ")
            if !who.isEmpty {
                Text(who).font(.caption).foregroundStyle(Color.secondaryText).lineLimit(1).padding(.leading, 18)
            }
            if expanded {
                VStack(alignment: .leading, spacing: 4) {
                    if !event.summary.isEmpty { Text(event.summary).font(.callout) }
                    if !event.reason.isEmpty {
                        Text("Reason: \(event.reason)").font(.caption).foregroundStyle(Color.secondaryText)
                    }
                    if let status = event.jobStatus {
                        Text("Session: \(jobStatusLabel(status))").font(.caption).foregroundStyle(Color.secondaryText)
                    }
                    HStack(spacing: 8) {
                        if event.sessionId != nil {
                            Button("Open session") { model.openSession(event.sessionId) }
                        }
                        if sourceURL(event.url) != nil {
                            Button("Open in \(event.sourceName)") { model.openSource(event.url) }
                        }
                        if event.canOpen {
                            Button("Create session") {
                                model.perform(.openItem(account: event.account, itemId: event.itemId), label: event.id)
                            }
                            .help("Override the \(decisionLabel(event.decision)) decision and request a session.")
                            .disabled(model.busyAction != nil)
                        }
                    }
                    .controlSize(.small)
                    .padding(.top, 2)
                }
                .textSelection(.enabled)
                .padding(.leading, 18)
            }
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())

    }
}

struct DecisionTag: View {
    var decision: String

    var body: some View {
        Text(decisionLabel(decision))
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .foregroundStyle(.primary)
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
                SourceScopeBar(model: model, includesChannel: false)
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
            Empty(text: model.sourceFilter.map { "No sessions from \(sourceLabel($0))" }
                  ?? "The automation has not opened any sessions yet")
        } else {
            List(jobs) { job in
                HStack(spacing: 6) {
                    Image(systemName: sourceSymbol(job.source)).foregroundStyle(Color.secondaryText).frame(width: 16)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(job.title).lineLimit(1)
                        Text("\(sourceLabel(job.source)) · #\(job.id) · \(jobStatusLabel(job.status))")
                            .font(.caption).foregroundStyle(Color.secondaryText)
                    }
                    Spacer(minLength: 4)
                    if Summary.activeStatuses.contains(job.status) { ProgressView().controlSize(.mini) }
                    Text(Times.ago(Times.parse(job.created), now: model.now))
                        .font(.caption).foregroundStyle(Color.secondaryText).monospacedDigit()
                    Button { model.openSession(job.sessionId) } label: { Image(systemName: "arrow.up.forward.app") }
                        .buttonStyle(.borderless)
                        .help("Open session in Hermes")
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
            Empty(text: model.dismissed.isEmpty ? "No issues to review"
                  : "No issues to review (dismissed: \(model.dismissed.count))")
        } else {
            List(problems) { problem in
                VStack(alignment: .leading, spacing: 4) {
                    Text(problem.title.isEmpty ? "(untitled)" : problem.title).lineLimit(2)
                    if !problem.detail.isEmpty {
                        Text(problem.detail).font(.caption).foregroundStyle(Color.secondaryText).lineLimit(4)
                            .textSelection(.enabled)
                    }
                    HStack(spacing: 8) {
                        actions(problem)
                        Spacer()
                        Button("Dismiss") { model.dismiss(problem) }
                            .help("Hide until this issue changes (only in this app)")
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
            if problem.sessionId != nil { Button("Open session") { model.openSession(problem.sessionId) } }
            Button("Retry") { model.perform(.retry(job: id), label: problem.key) }
                .help("Continue in the same session. Check for an existing result before retrying.")
            Button("New session") { model.perform(.newSession(job: id), label: problem.key) }
                .help("Start again in a separate conversation")
        case .followup(let id):
            if problem.sessionId != nil { Button("Open session") { model.openSession(problem.sessionId) } }
            Button("Retry follow-up") { model.perform(.retryFollowup(id), label: problem.key) }
                .help("Open a new follow-up session for this reply")
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
        Text(text).foregroundStyle(Color.secondaryText).frame(maxWidth: .infinity, maxHeight: .infinity).padding(24)
    }
}
