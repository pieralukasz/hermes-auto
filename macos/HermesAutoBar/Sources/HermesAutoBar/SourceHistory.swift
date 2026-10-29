import HermesAutoCore
import SwiftUI

struct HistorySourcePicker: View {
    @Bindable var model: Model

    private var selection: Binding<HistorySource> {
        Binding(
            get: { HistorySource(source: model.sourceFilter, channel: model.channelFilter) },
            set: { model.sourceFilter = $0.source; model.channelFilter = $0.channel }
        )
    }

    var body: some View {
        Picker("Source", selection: selection) {
            ForEach(HistorySource.choices(status: model.status, events: model.events,
                                          including: selection.wrappedValue), id: \.self) { source in
                Text(source.label).tag(source)
            }
        }
        .pickerStyle(.menu)
        .accessibilityLabel("History source")
    }
}

struct InlineSourceHistory: View {
    let connection: Connection
    @Bindable var model: Model
    var openHistory: () -> Void
    @State private var visibleLimit = 5

    var body: some View {
        let events = SourceScope.events(model.events, filter: .all,
                                        source: connection.source, channel: connection.channel)
        let visible = Array(events.prefix(visibleLimit))
        VStack(alignment: .leading, spacing: 10) {
            Text("Recent activity").font(.caption.weight(.medium)).foregroundStyle(Color.secondaryText)
            if let error = model.eventsError {
                Label("History could not refresh. \(error)", systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(Color.secondaryText)
            }
            if model.lastRefresh == nil && model.eventsError == nil {
                ProgressView("Loading history…").controlSize(.small)
            } else if events.isEmpty && model.eventsError == nil {
                Text("No events from \(connection.title) in the loaded history.")
                    .font(.callout).foregroundStyle(Color.secondaryText)
            }
            ForEach(visible) { event in
                EventRow(event: event, model: model)
                if event.id != visible.last?.id { Divider() }
            }
            HStack {
                if events.count > visibleLimit {
                    Button("Show more") { visibleLimit += 5 }
                }
                Spacer()
                Button("Open in History", action: openHistory)
            }
            .controlSize(.small)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
