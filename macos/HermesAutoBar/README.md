# Hermes Auto Bar

Menu bar app for `hermes-auto`: what the sorter decided and why, which sources are alive, what needs a look, and the
explicit corrections. It reads and changes the automation only through the installed CLI
(`~/.local/bin/hermes-auto`; another path: `defaults write io.github.pieralukasz.hermes-auto-bar cliPath <path>`).

| Tab | Shows | Actions |
|---|---|---|
| Zdarzenia | Every sorter decision (session, brief, skipped, read, reply, Todoist plan) with the model's reason | Open the session in Hermes, open the source, **Daj temu sesję** (`hermes-auto open-item`) |
| Sesje | Sessions the automation opened, with their status | Open in Hermes (`hermes://open/<id>`) |
| Problemy | `needs_attention` jobs and replies, source read errors, items waiting for a classifier retry | `retry`, `new-session`, `retry-followup`; **Odłóż** hides a problem in the app until it changes |

The header pauses and resumes the automation. A check in progress holds the CLI lock; actions retry for 30 s.
Queued sessions start on the next scheduled check (within a minute).

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

`HERMES_AUTO_BAR_EXPAND=<item id>` opens one timeline row. Both only work with `HERMES_AUTO_BAR_FIXTURE`.

Pure logic (decoding, problems, filters, times, links) lives in `HermesAutoCore` with unit tests; the SwiftUI layer
has no tests and needs a manual check.
