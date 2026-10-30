# Hermes Auto Bar

Menu bar app for `hermes-auto`: what the sorter decided and why, which sources are alive, what needs a look, and the
explicit corrections. It reads and changes the automation only through the installed CLI
(`~/.local/bin/hermes-auto`; another path: `defaults write io.github.pieralukasz.hermes-auto-bar cliPath <path>`).

| Tab | Shows | Actions |
|---|---|---|
| Status | Problems first, work in progress, then grouped connections | Expand a source to see its recent events inline; **Open in History** opens that source in the full history |
| History | Every sorter decision with its source, reason and resulting session status | Open the session in Hermes, open the source, **Create session** (`hermes-auto open-item`) |
| Sessions | Sessions the automation opened, with their status | Open in Hermes (`hermes://open/<id>`) |
| Issues | `needs_attention` jobs and replies, source read errors, items waiting for a classifier retry | `retry`, `new-session`, `retry-followup`; **Dismiss** hides a problem in the app until it changes |

Connections are grouped into **Messages**, **Mail**, **Tasks & projects** and, for other adapters,
**Other**. There is no horizontal source strip. A disclosure shows the latest five loaded events, with **Show more** to reveal more inline.
**Open in History** selects the source and messenger, resets the event type to All and opens the full history.
History has separate **Source** and **Show** pickers. Source choices include configured sources and sources
seen in loaded events, including individual Matrix messengers. Switching to Sessions leaves the History
selection intact; Sessions shows and applies only the source filter because jobs do not expose channels.
ZimaBlade lives in a separate **Infrastructure** section.

Matrix messenger names come from cached room metadata in `health.sources.matrix.channels`, not from a
hardcoded list. They are previously observed apps, not a verified list of currently connected bridges.
Their individual bridge state is explicitly unknown. A successful Matrix read does not prove a bridge is
healthy; old CLIs without channel metadata show a Matrix row instead. Listener timestamps and source reads
are distinct observations, not an end-to-end health guarantee. Failed refreshes retain old data with a warning.

Design: native system typography and disclosures, a vertical reading order, neutral surfaces and color for
state only. Problems are the focal point, group spacing separates domains, and source-to-result text connects
history entries to sessions (ENERGY 1 / RHYTHM 2 / MOTION 1). No custom fonts, artwork or decorative motion.
The interface is English; source messages and task titles retain their original language. Source icons align
with section headings and disclosure chevrons sit on the right. Expanded history uses the full section width,
without an extra container inset; event text keeps only the space needed for its own disclosure chevron.

The header pauses and resumes the automation. A check in progress holds the CLI lock; an action waits for it
(up to 120 s), then **Retry**, **New session** and **Create session** create the session at once (its run continues in
the background). A retried reply still starts on the next scheduled check (within a minute).

## Build and run

```bash
swift test
scripts/bundle-app.sh            # .build/Hermes Auto Bar.app
open ".build/Hermes Auto Bar.app" --args --window   # also opens the window, not only the menu bar item
```

Sample data without touching the automation (screenshots, UI checks):

```bash
open -n --env HERMES_AUTO_BAR_FIXTURE=$PWD/Tests/HermesAutoCoreTests/Fixtures \
  --env HERMES_AUTO_BAR_TAB=problems ".build/Hermes Auto Bar.app" --args --window
```

`HERMES_AUTO_BAR_EXPAND=<item id>` opens one timeline row, `HERMES_AUTO_BAR_SOURCE=gmail` expands that source and sets the initial history filter. Both only work with `HERMES_AUTO_BAR_FIXTURE`.

Pure logic (decoding, problems, filters, times, links) lives in `HermesAutoCore` with unit tests; the SwiftUI layer
has no tests and needs a manual check.
