import HermesAutoCore
import SwiftUI

struct ConnectionDisclosureStyle: DisclosureGroupStyle {
    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { configuration.isExpanded.toggle() } label: {
                HStack(spacing: 12) {
                    configuration.label
                    Image(systemName: configuration.isExpanded ? "chevron.down" : "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color.secondaryText)
                        .frame(width: 12)
                }
                .frame(minHeight: 40)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(configuration.isExpanded ? "Expanded" : "Collapsed")
            if configuration.isExpanded {
                configuration.content
                    .padding(.top, 4)
                    .padding(.bottom, 14)
            }
        }
    }
}

struct OverviewView: View {
    @Bindable var model: Model
    @Binding var tab: Tab
    @Binding var filter: EventFilter

    var body: some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 24) {
                activity
                if let status = model.status {
                    ForEach(Connections.groups(status)) { group in
                        VStack(alignment: .leading, spacing: 0) {
                            Text(group.title).font(.headline).padding(.bottom, 6)
                            ForEach(group.items) { connection in
                                ConnectionRow(connection: connection, model: model) {
                                    model.sourceFilter = connection.source
                                    model.channelFilter = connection.channel
                                    filter = .all
                                    tab = .events
                                }
                                if connection.id != group.items.last?.id {
                                    Divider().padding(.leading, 28)
                                }
                            }
                        }
                    }
                    if status.health == nil {
                        Text("This CLI does not provide source health. History and sessions are still available.")
                            .font(.callout).foregroundStyle(Color.secondaryText)
                    }
                    if let relay = status.health?.relay {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Infrastructure").font(.headline)
                            DisclosureGroup {
                                VStack(alignment: .leading, spacing: 12) {
                                    Text("ZimaBlade forwards event signals to the automation on your Mac. It is a transport, not a message source.")
                                        .foregroundStyle(Color.secondaryText)
                                    RelayDetails(relay: relay, now: model.now)
                                }.font(.callout)
                            } label: {
                                HStack(spacing: 10) {
                                    Image(systemName: "server.rack")
                                        .foregroundStyle(Color.secondaryText).frame(width: 18)
                                    Text("ZimaBlade").font(.body.weight(.medium))
                                    Spacer(minLength: 8)
                                    Text(relay.connected.map { $0 ? "Connected" : "Disconnected" } ?? "Unknown")
                                        .font(.caption).foregroundStyle(Color.secondaryText)
                                }
                            }
                        }
                    }
                }
            }
            .disclosureGroupStyle(ConnectionDisclosureStyle())
            .padding(.horizontal, 16).padding(.top, 4).padding(.bottom, 20)
        }
    }

    private var activity: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !model.openProblems.isEmpty {
                Button { tab = .problems } label: {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange)
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Needs attention: \(model.openProblems.count)").font(.headline)
                            Text(model.openProblems.first?.title ?? "")
                                .font(.callout).foregroundStyle(Color.secondaryText).lineLimit(2)
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.caption).foregroundStyle(Color.secondaryText)
                    }.padding(12).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 10))
                .accessibilityLabel("Show issues: \(model.openProblems.count)")
            }
            if !model.activeJobs.isEmpty {
                Button {
                    model.sourceFilter = nil
                    model.channelFilter = nil
                    tab = .sessions
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "arrow.triangle.2.circlepath")
                        Text("In progress: \(model.activeJobs.count)")
                        Spacer()
                        Text("Show sessions").font(.callout)
                        Image(systemName: "chevron.right").font(.caption)
                    }.padding(.vertical, 4).contentShape(Rectangle())
                }.buttonStyle(.plain)
            }
            Text("Expand a source to see its recent activity.")
                .font(.callout).foregroundStyle(Color.secondaryText)
        }
    }
}

struct ConnectionRow: View {
    let connection: Connection
    @Bindable var model: Model
    var showHistory: () -> Void
    @State private var expanded: Bool

    init(connection: Connection, model: Model, showHistory: @escaping () -> Void) {
        self.connection = connection
        self.model = model
        self.showHistory = showHistory
        _expanded = State(initialValue: Preview.source == connection.source)
    }

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            InlineSourceHistory(connection: connection, model: model, openHistory: showHistory)
        } label: {
            HStack(spacing: 10) {
                Image(systemName: sourceSymbol(connection.source))
                    .foregroundStyle(Color.secondaryText).frame(width: 18)
                Text(connection.title).font(.body.weight(.medium))
                Spacer(minLength: 8)
                Text(connection.state == .read
                     ? "Read \(relativeTime(connection.health.lastPoll, now: model.now))" : connection.statusText)
                    .font(.caption).foregroundStyle(Color.secondaryText)
                if connection.state == .warning {
                    Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange)
                        .accessibilityLabel("Needs attention")
                }
            }
        }
    }
}
