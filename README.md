# Hermes Auto

Opt-in tasks and email replies become separate, resumable [Hermes Agent](https://github.com/NousResearch/hermes-agent) conversations.

Four independently installable Python distributions live in this repository:

| Package | Responsibility | Extra dependencies |
| --- | --- | --- |
| `hermes-auto` | Durable jobs, session IDs, preparation policy, CLI, scheduling | None |
| `hermes-auto-todoist` | Due tasks and recurring completion events through `td` | Core + authenticated Todoist CLI |
| `hermes-auto-gmail` | Replies in labelled Gmail threads | Core + Google auth + requests |
| `hermes-auto-proton` | Replies to labelled Proton conversations through Bridge | Core; Python's IMAP library |
| `hermes-auto-slack` | Sorter source: DMs, mentions, replies in followed threads and posts in watched channels (read-only) | Core; `requests` |
| `hermes-auto-github` | Sorter source: your GitHub notifications (read-only) | Core; `requests` |
| `hermes-auto-linear` | Sorter source: your Linear inbox (read-only besides marking shown notifications read) | Core + authenticated `linear` CLI |
| `hermes-auto-imessage` | Sorter source: new iMessage/SMS messages on this Mac (read-only `chat.db`) | Core; Full Disk Access for the service |
| `hermes-auto-matrix` | Sorter source: WhatsApp, Messenger, Instagram (later LinkedIn) chats bridged into your Matrix homeserver (read-only) | Core; a Matrix access token |

Installing Todoist does not install either mail adapter or their dependencies. The core discovers adapters through the `hermes_auto.sources` Python entry-point group. Adapters create durable events; they never start an agent themselves.

## Requirements

- Python 3.11+, macOS or Linux. The service installer currently supports macOS launchd. Windows is not supported in v0.1.
- A working local Hermes Agent source installation with the session registry API and single-query JSON output. Run `hermes-auto doctor` after installing or updating Hermes. The bridge uses Hermes' Python runtime and Python session API; it never changes Hermes source files. These APIs are version-sensitive. An incompatible version must pass the doctor and contract checks before use.
- Each chosen adapter's existing authentication. Credentials stay on your machine; this project does not operate a hosted service.

## Install only what you need

From a checkout (the core and adapter resolve together):

```sh
git clone https://github.com/pieralukasz/hermes-auto.git
cd hermes-auto
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./packages/hermes-auto ./packages/hermes-auto-todoist
hermes-auto init
hermes-auto source enable todoist
hermes-auto doctor
```

Install Gmail and Proton separately if wanted:

```sh
python -m pip install ./packages/hermes-auto-gmail ./packages/hermes-auto-proton
hermes-auto source enable gmail
hermes-auto source enable proton
```

GitHub releases include a wheel and source archive for **each** distribution. Download the core wheel and your chosen adapter wheel(s), then install those files together with `pip install`. Version 0.1 is distributed through GitHub; it is not advertised as published to PyPI.

Configuration and durable state live in `~/.config/hermes-auto/`. `--home PATH` or `HERMES_AUTO_HOME` selects another installation. Do not commit this directory. `config.json` contains paths and credential retrieval commands, never copied OAuth tokens or Bridge passwords.

## Todoist

Authenticate the [official Todoist CLI](https://github.com/Doist/todoist-cli), then:

```sh
hermes-auto source setup todoist
```

This creates the `hermes` label only; it does not select or edit any task. All projects are supported; only today's and overdue tasks qualify. A task with a due **time** waits until that time (floating times use the Mac's local clock; fixed-timezone times are compared in UTC); date-only tasks qualify all day.

Add **`hermes`** to a task and the run behaves like opening a new Hermes Desktop session and typing the task yourself:

- The first message is the task title and description, plus one note that you are away: no questions, and nothing is sent, paid, ordered or changed; it is prepared for your approval instead.
- The agent gets your normal Hermes setup: all configured toolsets, skills, memory, project rules and subagents, with the Desktop platform prompt (Markdown, tables, links). Runs go through the Hermes gateway as Desktop (`gui`) sessions, so the prompt and limits match an interactive session.
- Budget: 900 s / 60 turns by default (`agent_run_budget_seconds`, `agent_max_turns`). Commands that would need approval are denied, because nobody can approve them.
- One profile, no bots: every automatic session lives in the default Hermes profile (Dolores), the old `inbox`/`tapin`/`ecmr` bots and the subagent hand-off are retired. The account comes from `routes` (e.g. a company's Slack, GitHub org, Linear and Todoist project on its work provider, a Slack DM before the general Slack rule so it stays private). Someone else's content (mail, also agent-labelled threads, Slack, GitHub, Linear, `↩` replies) opens with the default profile's normal toolsets, but its first, automatic turn may only read: the `hermes-auto-guard` plugin (`first_turn` mode, `hermes-auto install-guard`) locks a session the bridge registered (`<home>/hermes-auto-guard/sessions/<id>`, prompt marked) until you write in it. Until then its turns, including the background review fork, get `hermes_auto_read` (one grammar-checked read command), raw skill reads and `session_search` in this profile; `delegate_task`, terminal, files, web, memory and `tool_search` (it may query remote connectors) are refused. What it reads depends on the session's account: config.json `first_turn` has default `reads` and per-provider entries (e.g. the work account reads GitHub, Linear, Slack and Todoist, but no calendar, Gmail or earlier conversations). The prompt names only those reads and asks for one to three sentences (who wants what, with context), no draft. A session the guard cannot classify (registry or state.db unreadable, a compression lineage with a cycle or a dangling parent) gets no tool at all. Other sessions are untouched, and after your first message the session has every tool. Todoist tasks run with full tools from the start. Readiness (`bridge.require_ready`, `hermes-auto doctor`): the guard enabled with the scope config.json asks for and loaded by the running gateway in the expected version, `hermes-auto` in `platform_toolsets.cli`, approvals other than `off`; otherwise third-party sessions are refused before anything is sent. Older jobs whose sessions are in a retired bot's profile keep it for archive detection and opening; they are not continued automatically (`new-session` opens a fresh one in the default profile), and a profile that is gone answers "missing".

Write the goal, the skill to use and the limits ("don't send without my OK") in the description. `language` in `config.json` sets the answer language. Legacy mode labels (`hermes-open`, `hermes-draft`, `hermes-research`, `hermes-agent`) are still recognised for existing tasks; `hermes-open` creates the conversation without a model call. They are no longer created.

One-off tasks keep one event regardless of rescheduling. A recurring task starts a new occurrence only after a Todoist **completion event**, not because its due date changed. Activity pages are fetched completely with an overlap window. Failed reads do not advance the cursor. Tasks removed from the due/opt-in filter have unstarted work deferred; when eligible again, the same reserved job can proceed. Already prepared tasks never automatically repeat. Explicit `new-session` is available when you want another preparation.

**Archive = done.** When you archive a prepared session in Hermes, the next check completes its Todoist task (`td task complete`). Only an archive that happens after the session was seen open counts: sessions already archived when this check first saw them never complete anything. Already completed or deleted tasks are left alone. A recurring task is completed only while Todoist still shows the occurrence that session prepared (same due day) and has logged no completion of it since the job was queued, so archiving an old briefing, or a session whose agent already ticked the task, never skips a newer occurrence. The Todoist prompt lets the agent complete its own task once the work is actually done; sending, paying, publishing and other records stay gated, and work awaiting approval stays open. Superseded attempts (`new-session`) do not count. Disable per source with `"complete_on_archive": false`. Unarchiving does not reopen the task.

The Todoist activity API's available history limits recovery after a long period offline. This version does not backfill every missed occurrence: it prepares the current eligible occurrence and relies on available completion history. It will not promise historical reconstruction beyond the provider's retention.

## Gmail

Configure `sources.gmail.token_file` to an existing Google authorized-user OAuth JSON file. Polling requires `gmail.readonly`; the optional label-creation command needs a write-capable scope. You may instead create **`Hermes/Watch`** in Gmail yourself.

```sh
hermes-auto source setup gmail
```

Apply `Hermes/Watch` to a conversation you want to follow. Mail is written by third parties, so its session's first, automatic turn only reads (see the guard under Todoist): it presents who wants what in one to three sentences, without a draft. `Hermes/Agent` (`agent_label`) no longer gives a thread full tools on its first turn: the thread still holds other people's replies, so it is a third-party session like any other; you continue it with every tool. The first poll records a baseline without creating sessions for old mail. Each later incoming reply creates one event keyed by account and Gmail message ID. Reading/unreading messages does not retrigger it. Own sent messages and drafts are excluded; configure `own_addresses` for additional aliases.

The adapter reads all messages of each labelled thread: [new Gmail replies do not inherit the thread's old labels](https://developers.google.com/workspace/gmail/api/guides/labels). Removing the label stops watching and cancels pending work. Re-enabling establishes a fresh baseline. A reply arriving before the first poll after labelling is part of that baseline; use `hermes-auto scan` immediately after labelling to arm the watch.

No mail is marked as read, sent, moved or saved as a draft. Message bodies in selected events are supplied to your configured Hermes model for preparation. Attachments are not downloaded. Each distinct reply gets a conversation; this version does not group multiple new replies into one session.

## Proton

Requires a logged-in local [Proton Mail Bridge](https://proton.me/support/bridge-for-linux), its IMAP credentials, and its trusted TLS certificate. Proton exposes [labels as IMAP folders](https://proton.me/support/labels-in-bridge).

Example `sources.proton` configuration (fill in your own values):

```json
{
  "enabled": true,
  "host": "127.0.0.1",
  "port": 1143,
  "username": "you@proton.me",
  "certificate": "/absolute/path/to/bridge-certificate.pem",
  "password_command": ["security", "find-generic-password", "-s", "proton-bridge", "-w"],
  "own_addresses": [],
  "watch_mailbox": "Labels/Hermes Watch",
  "agent_mailbox": "Labels/Hermes Agent",
  "all_mailbox": "All Mail",
  "mode": "draft"
}
```

`password_command` is a local argument array, never a shell string. On Linux use your own secret-manager command. Only loopback Bridge hosts are accepted. TLS verifies the explicitly trusted certificate; its hostname is not required to match the loopback IP.

```sh
hermes-auto source setup proton
```

Sending (optional): `hermes-auto-proton-send --to a@b.test --subject "..." --body-file body.txt [--from addr] [--cc ...] [--attach file] [--in-reply-to "<id>"]` sends one mail through Bridge SMTP (`smtp_port`, default 1025, same login and certificate) and prints `{"status": "sent", "id": "<Message-ID>", "threadId": "<root>"}`. Agents should send Proton mail this way: the sorter recognises the command and receipt, so a reply goes back to the Hermes session that sent it.

Instant mail (optional): `hermes-auto-proton-listen` keeps an IMAP IDLE session on INBOX (read-only, through the same Bridge login). On new mail it records only the time in `proton-listener.json` and runs `hermes-auto tick` after `listen_quiet_seconds` (15); the Proton source's `due` makes that a Proton-only check. `hermes-auto-proton-listen check` tests Bridge and IDLE, `install-launchd` installs `io.github.hermes-auto.proton-listener` (KeepAlive). Regular checks stay as the fallback.

Apply **`Hermes Watch`** to a message in a conversation (or **`Hermes Agent`**, `agent_mailbox`, for the full agent, as with Gmail). Replies are matched through RFC `Message-ID`, `In-Reply-To` and `References`, not subject similarity. As with Gmail, the first poll establishes a baseline. Missing/broken threading headers cannot be reliably matched; those messages will not trigger automatically. The adapter reads up to 256 KiB of raw MIME per reply and supplies at most 20,000 text characters to Hermes. MIME may contain attachment bytes, but attachments are not extracted or supplied to the model. Read-only mailboxes and `BODY.PEEK` preserve read flags.

## Slack

A sorter source (needs `triage.enabled`). It reads Slack with your user token (the Slack MCP OAuth token Hermes stores in `~/.hermes/mcp-tokens/slack.json`; scopes: history and read for channels, groups, im, mpim, `users:read`, and the MCP search scopes) and never posts.

- **What counts:** DMs and group DMs, `@you` in channels, replies in threads you follow (Slack subscribes you to threads you started, wrote in or were mentioned in), and every new top-level post in the channels listed in `sources.slack.watch_channels` (channel ids, e.g. a bug-report channel; each post is its own conversation, `why: channel`; the server-side listener needs the same list). Bots, Slackbot, your own messages, edits and `@here`/`@channel` alone do not.
- **Bursts:** messages of one conversation (a DM or a thread) are handed over together once nobody wrote for `quiet_seconds` (90). If you answered it in Slack before that, nothing opens. Reading it there does not stop the session (`skip_read: true` restores that).
- **Decisions:** the classifier uses `triage.slack_criteria`: *session*, *brief* (a `## Slack` section) or *skip*. A later burst in the same conversation opens a `↩` session with the earlier one's context.
- **Session:** the automatic step researches with reads only (Slack MCP reads, `git`/`gh`/`linear` reads, `web_search`/`web_extract`, calendar, Todoist, past sessions) and ends with who wants what, the findings and a reply draft. Sending, writing files and every other tool get an error result; the session keeps its full toolset for your next turn, so sending happens only after you say so there.
- **Private channels:** `private_channels: "link_only"` (default) passes only author, place and link; `"full"` passes the text.
- **Instant (Socket Mode, optional):** `hermes-auto-slack-listen` keeps a Socket Mode connection with an app-level token (`xapp-`, scope `connections:write`; read by `app_token_command`, default: macOS keychain item `hermes-auto-slack-app`). The Slack app needs Socket Mode on and user event subscriptions `message.im`, `message.mpim`, `message.channels`, `message.groups`. On a relevant message it records ids only (`slack-listener.json`: hints, `nudge_at`, heartbeat, event count; no text) and runs `hermes-auto tick` after `quiet_seconds`; the source's `due` then makes it a Slack-only check. `hermes-auto-slack-listen check` tests the token, `install-launchd` installs `io.github.hermes-auto.slack-listener` (KeepAlive).
- **Cadence:** `sources.slack.poll_seconds` (120) runs Slack-only checks between the regular `poll_seconds` checks; once the listener is connected and has received an event, `listener_poll_seconds` (900) applies instead. Slack messages join the waiting-task check like mail; private-channel items (redacted, no content) stay out.

```bash
python -m pip install ./packages/hermes-auto-slack
hermes-auto source enable slack
```

The first check records a baseline; only later messages open sessions.

Unattended Slack/GitHub research uses explicit read-command grammars for trusted Slack, GitHub and Linear tools, plus the existing calendar/Todoist reads. Todoist accepts only explicit read options (no progress-output files or account switching); Linear issue reads require `--no-download`. It cannot execute local git or arbitrary web tools: repository configuration can run programs, and web URLs can transmit private context. Redacted private Slack items use the `private` profile with no executable tools. These restrictions apply only to automatic preparation; the user's later turn keeps the normal toolset. The session prompt names only the reads its read-only bot grants on that session's account (`bots.read_only` plus the `routes` provider), so a work-account session is not told to search Todoist, the calendar or past conversations; it reports findings and proposed points and names the bot to hand the matter to, which writes the draft. With Dolores delegating (bots), the read list goes into the subagent's `goal`: its session row lives in Dolores' database, so inbox's guard cannot tell its account and, with per-account entries in `bots.read_only.inbox.providers`, grants only the reads every account shares and no `session_search`.

## Push gateway (Gmail, Todoist)

`gateway/` is a Cloudflare Worker for webhooks that need a public address. Todoist: an App Management app ("Hermes Auto") with a webhook to `/todoist` for `item:*` events; the Worker checks `X-Todoist-Hmac-SHA256` against the app's client secret (`wrangler secret put TODOIST_CLIENT_SECRET`). Webhooks fire only for users who authorized the app. Gmail push: `users.watch` publishes to a Pub/Sub topic, a push subscription posts to the Worker, the Worker hands an ids-only event to `hermes-auto-hooks-listen` (core, extra `hooks`) over an outbound WebSocket, which nudges a Gmail-only check. Set `sources.gmail.push_topic`; the Gmail adapter renews the watch daily in `poll` (configuration only, no mail is changed). The regular checks stay as the fallback.

## GitHub

A read-only sorter source for your notifications (`GET /notifications` with the `gh` token). With `mark_read` it marks the shown thread read only when the thread has no newer update (see below). Each notification thread is one conversation: review requests, mentions, comments, assignments, CI results. `reason: subscribed` (repositories you only watch) is skipped (`skip_reasons`). Already read on GitHub, or the latest comment or review is yours: no session. A later update in a thread that already has a session opens nothing unless it carries a new comment or review from a person (a push, an edit, a reviewer change, the same comment again or a bot is recorded as `skip`); that comment goes through the classifier marked `followup`, and only a `session` verdict opens a `↩` session. A `↩` title keeps one arrow however long the chain. Pull request reviews are read too (`/pulls/{n}/reviews`), as notifications carry no link to them. Decisions use `triage.github_criteria`; sessions get the same read-only research profile as Slack and never comment, review, merge or push on their own.

GitHub has no push for account notifications; `hermes-auto-github-listen` polls with `If-Modified-Since` at GitHub's `X-Poll-Interval` (60 s; a 304 is free) and nudges a GitHub-only check. Without it, `poll_seconds` (300) applies.

## Linear

A sorter source for your Linear inbox (`notifications`), read through the [`linear` CLI](https://github.com/schpet/linear-cli) (`linear api`), so the API key stays in the CLI's keyring. Assignments, mentions, comments, status changes, unblocked issues and document changes are candidates; Linear's own reminders and digests (`projectUpdatePrompt`, `feedSummaryGenerated`) and reactions are skipped (`skip_types`), as are tickets marked done or canceled and unblocks of such tickets (`skip_closed`) and comments on other people's tickets that do not mention you (`only_my_comments`). Notifications about one issue form one conversation, so a later one opens a `↩` session. A notification already read in Linear still reaches the sorter (`skip_read: true` changes that). Decisions use `triage.linear_criteria` and a `## Linear` brief section; sessions get the read-only research profile and never comment on, move or assign issues on their own. Linear has no push for a member's inbox (webhooks need a workspace admin), so the source is polled every `poll_seconds` (120); with `mark_read` the shown notification is marked read in Linear once its session or brief is ready.

## iMessage

A sorter source for new iMessage and SMS messages on this Mac, off by default (`sources.imessage.enabled`). It reads Messages' own database `~/Library/Messages/chat.db` opened read-only (SQLite `mode=ro`) instead of the `imsg` CLI: one indexed query per check from a stored message rowid finds every new message across all chats (`imsg` would need one `history` call per chat, and `watch` is a stream for one process), the adapter cannot send or change anything, and tests run on a synthetic SQLite file. Only messages from other people count (DMs and groups; a named group shows its name, an unnamed one its members); tapbacks, group events, empty messages and anything older than `max_age_hours` (24, iCloud sync) are skipped. Messages of one chat are handed over as one burst after `quiet_seconds` (90) of silence; the chat is the conversation, so a later burst follows as a `↩` session. An answer you wrote after the burst marks it read (no session); a burst read on the phone still reaches the sorter unless `skip_read: true`. Sender names come from Contacts (read-only) when `contacts` is on. Nothing is marked read in Messages (no API; the read marks are `unsupported`). Messages has no push: the source is polled every `poll_seconds` (60, one local query).

The process that reads `chat.db` needs **Full Disk Access**: for the launchd service that is the real Python binary of the service venv (`readlink -f ~/.local/share/hermes-auto/venv/bin/python`), because a launchd job has no app to inherit it from. Without it the read fails with an error naming Full Disk Access and the source retries with backoff. The first automatic turn can read more of its own chat with `imsg history --chat-id N` (guard 3.0.2, below); that runs inside the Hermes gateway, so Hermes.app needs Full Disk Access for it too, else the read returns a note and the session works from the card's context.

## Matrix (WhatsApp, Messenger, Instagram, LinkedIn)

A sorter source for chats bridged into your own Matrix homeserver by mautrix bridges, off by default (`sources.matrix.enabled`). It reads with your access token, GET only: an incremental `/sync` from a stored `since` token (the baseline sync fetches no messages) finds rooms with new messages, and `/rooms/{id}/messages` reads the chat once it has been quiet for `quiet_seconds` (90). Room state (`m.bridge`: protocol, `com.beeper.room_type`) gives the messenger (WhatsApp, Messenger, Instagram, LinkedIn; else the puppet prefix such as `@whatsapp_`) and DM or group; it is cached for a day. Never counted: your own messages (your Matrix user, which double puppeting writes as, and any puppet in `own_puppets`), the bridge bots (`bridge_bots`) and their management rooms, spaces, notices, edits, redactions and anything older than `max_age_hours`. The room is the conversation, so a later burst follows as a `↩` session. Nothing is marked read: a Matrix read receipt travels through the bridge to the sender (a "read" tick in WhatsApp or Messenger), so the source has no `mark_read`. Every `/sync` sends `set_presence=offline`, so polling never shows you online; the client follows no redirect (urllib would carry the token to another origin) and an error shows only the HTTP status and a well-formed `M_` code. A message is new by its event id (handed-over ids are kept for `max_age_hours`), not by its time: a bridge catching up keeps each message's original time, so it can arrive after a later one. Polled every `poll_seconds` (60, one `/sync`).

The token comes from `token_file` (default `~/.config/hermes-auto/secrets/matrix.token`, must be mode 0600) or `token_keychain` (a Keychain service name read with `security find-generic-password -w`); it is never logged or put in an error. `hermes-auto-matrix check` verifies it (user and room count). `hermes-auto-matrix history ROOM --limit N` prints a room's latest messages; the first automatic turn of a Matrix session may run it for its own room only.

Messages from iMessage and Matrix go through the sorter like Slack (`triage.chat_criteria`, a `## Komunikatory` brief section) and stay on the default (private) account unless a `routes` rule says otherwise.

## Relay queue and push mode (always-on server)

With an always-on machine (e.g. a home server on Tailscale), the listeners that need a permanent connection run there and the Mac only reads a queue:

- On the server: `hermes-auto-relay serve` keeps a SQLite queue (`relay-queue.sqlite3`, ids only, 7 days) on `relay.host:relay.port` with a bearer token (`relay.token_command`). The Slack, GitHub and gateway listeners run there with `relay.publish_url` set, so they publish to the queue instead of running a tick. Example systemd user units: `hermes-auto-relay.service`, `hermes-auto-slack.service`, `hermes-auto-github.service`, `hermes-auto-hooks.service`.
- On the Mac: `hermes-auto-relay client` (`install-launchd`: `io.github.hermes-auto.relay-client`) long-polls `/events?since=<seq>`; after sleep it receives everything queued, in order, and nudges the matching sources. `relay.again_after` asks for one more check later (Todoist: 960 s, after `settle_minutes`). If the relay stays unreachable for `relay.fallback_seconds` (1800), it nudges all `relay.sources` so the Mac checks them itself.
- `"poll_sources": false` ends periodic source checks: a source is read only when its listener, webhook or timer says so (`due`). The periodic tick keeps local upkeep only (finished runs, archive → done, briefs). Todoist also checks at a new day and when a timed task becomes due; Gmail renews its watch daily. Proton stays on the Mac: its listener checks once after every (re)connect, so mail that came during sleep is read on wake.

Slack persists failed hints before acknowledging them and retries them after restart. Every Socket Mode reconnect schedules a rate-limited catch-up, including outages shorter than the stale-listener threshold. Each listener reports its source health through `/heartbeat`. The Mac checks a source when its listener has been stale for `relay.stale_seconds` (600), at most once per `fallback_seconds`; an idle healthy listener does not trigger checks. Queue epochs and retention gaps trigger catch-up. Gateway sequence numbers are acknowledged only after the relay accepts an event. Events arriving during a tick remain due; source errors retain the event and retry with backoff (60 seconds to 30 minutes). `source_backlog:<name>` drains remaining Slack, GitHub and Proton batches without waiting for another event.

## Sorter (opt-in)

Instead of labelling threads and tasks yourself, let the sorter look at everything new:

```json
"triage": {
  "enabled": true,
  "model": "claude-sonnet-5-5",
  "brief_times": ["07:45", "16:30"],
  "todoist": {"apply": true, "settle_minutes": 15}
}
```

`brief_times` is a list for every day, or per day / day range: `{"mon-fri": ["06:00", "10:00", "14:00", "18:00"], "sat-sun": ["08:00"]}` (a day matching no key has no brief). A section source's own `times` must name slots that exist.

- **Mail (Gmail, Proton):** every new inbox message gets one decision. *Session*: a person expects your answer or action, so a conversation opens with a one- or two-sentence notice of who wrote and what they want. *Brief*: worth knowing, listed in the next brief at `brief_times`. *Skip*: marketing and noise. Adjust `mail_criteria` in plain language. Message notifications from LinkedIn and similar services count as mail from that person.
- **Replies go back to their conversation.** When someone answers mail you sent from a Hermes session, the reply opens a new session ("↩ <title>") carrying that conversation's context; the earlier conversation is never written to or unarchived.
- **Third-party mail runs read-only.** Both cases say what arrived, with context from reads: calendar list, Gmail search/get, Todoist list/view, past sessions, skills, and `read_file`/`search_files` on output Hermes saved from those reads (`~/.hermes/cache/terminal-output`). Every other tool call and every terminal command that is not one plain read (no pipes, redirects or substitutions) is refused per call: the reads of the same batch still run, and the refused call gets a neutral "not run" result (no `error`, so Desktop shows no red failure). No draft; drafting happens when you reply in the session, with full tools. Nothing is sent or changed.
- **Clean first message.** A sorted session shows a short card (source, who, what, where, why) and a Todoist session shows the task as you would type it; the model gets the full prompt with the source text. Hermes keeps that prompt as the message's `api_content`, so your later turns in the session still see it.
- **Short names (`title_model`, off by default):** e.g. `"title_model": "claude-haiku-5-5"` names every classified item and reply in one extra call per batch and model route, on the route's account. Sessions are titled `<Source> · <name>` ("GitHub · Aviad: review sports stat cards", replies `↩ GitHub · …`); without a name the subject is used. Hermes keeps titles unique, so a clash gets " (2)".
- **Todoist Inbox:** a task that has sat in the Inbox for `settle_minutes` gets a full plan following your method (`triage.todoist.guidance`): list, priority (always), `hermes` when the agent can do it, `@czekam` when it waits on access, a mail or a person. Context comes from your recent Hermes conversations about the same matter (`context_days`, read-only). The plan is applied at once and listed in one "Inbox Todoist: posortowane" session with a one-line reason per task; reply "ok" or corrections. Nothing starts on its own (`auto_start: false`): tasks the agent could begin now are listed and start after your OK; a dated task gets the `hermes` label only then, since the due-date poller would start it.
- **Do organizacji and current work:** tasks in the saved filters `review_filters` (default "Do organizacji") get the same plan once. Tasks with `hermes` in `proposal_filters` (the four "Bieżące" views, Bonus included) and no due date are offered once in an "Agent może to zrobić" session; OK sets the due date to today and the poller opens one session per task. `@czekam`/`@zablokowane` never start.
- **Waiting tasks:** with new mail, GitHub or Linear notifications or Slack messages (not redacted private channels), one model call checks whether a message delivers what a top-level `@czekam`/`@zablokowane` task waits for (title, description, last comments). Ready: one "Odblokowane: …" session offering to drop `@czekam` (and start it if it has `hermes`). Partly: a Todoist comment on the task, no question. Each mail speaks up at most once per task. Mail, Slack and GitHub sessions also look up a related Todoist task (`td task list --filter 'search: …'`) and name it first, without changing Todoist.
- **Brief sections from local commands:** `triage.brief_sources` (e.g. a finance review) run at their brief time with `SINCE` set to their last success and print one JSON object: `title`, `summary` ("" = nothing new), `lines`, `questions` (`text`, `reason`, `proposal`), optional `propose_prompt`/`categories`/`context` (missing proposals are filled by one model call without tools), `evidence` (checked against `@czekam` tasks like mail) and `agent_note` (hidden instructions for the session). A failed run (server asleep) retries each check and posts on its own. You answer the questions in the brief session; the agent there applies them with full tools.
- `hermes-auto triage-preview --days 14` shows what the sorter would decide for recent mail and the current Inbox, without changing anything (it does spend model tokens). The report is written to `triage-preview.md` in the automation home.
- **Read at the source (`mark_read`, default on):** Gmail removes `UNREAD` from the specific shown message; Proton uses its captured INBOX UID and UIDVALIDITY (legacy marks without this identity stay `unsupported`). Slack marks DMs/group DMs only across a contiguous range of shown messages, never past an earlier item awaiting its session or brief. GitHub marks the notification thread unless it was updated after the shown update (then `unsupported`; the newer update is its own item); an update landing between that check and the mark is read with the thread but still reaches the sorter, because a read less than a minute after the update does not count as read by the user. Linear marks the single shown notification. Channels and thread replies stay `unsupported` rather than hiding other unread content. Slack has no compare-and-set: a concurrent manual read may be moved backwards, making later messages unread again. Skipped noise is eligible immediately subject to the same ordering guard; failed sessions stay unread. Per-item results isolate failures and retry at most five times. Turning off marking or disabling a source cancels every pending mark (`disabled`), including marks waiting for unfinished jobs; re-enabling does not revive them.
- **Separate accounts per organisation (`routes`):** the first matching rule picks the Hermes provider (and optionally model) for both the classifier and the session runs; everything unmatched keeps the sorter model and Hermes' default. `source` is the adapter, other keys compare with the event payload (case-insensitive). Replies in a thread ("↩") carry the same payload, so they stay on the same account.

  ```json
  "routes": [
    {"match": {"source": "slack"}, "provider": "work-account"},
    {"match": {"source": "github", "owner": "work-org"}, "provider": "work-account"}
  ]
  ```
- Sessions appear in the normal Sessions list. Optional: `"hide_sessions": true` plus the Desktop plugin in `desktop-plugin/hermes-auto` (copy to `~/.hermes/desktop-plugins/hermes-auto`) moves them to a separate **Auto** page.

## Operate

```sh
hermes-auto scan --dry-run  # temporary copy of state; no sessions, no baseline changes
hermes-auto scan            # record opt-ins/baselines and enqueue; no model calls
hermes-auto run             # scan and prepare eligible pending jobs now
hermes-auto status          # works while the service is running; includes source errors and source health
hermes-auto events          # read-only: the sorter's latest decisions with reasons, newest first (no bodies)
hermes-auto pause
hermes-auto resume
hermes-auto retry 12        # explicitly continue needs_attention job in its existing session
hermes-auto new-session 12  # explicitly allocate a DIFFERENT conversation
hermes-auto open-item gmail:me@example.test MSG_ID  # override the sorter: give a skipped/briefed message its session
hermes-auto backup
```

`retry` and `new-session` queue work; run it explicitly or wait for the scheduler. Automatic retries are deliberately disabled after an uncertain outcome: first inspect the existing conversation. Deleting a conversation does not erase the event record and does not automatically recreate it. `new-session` is the explicit escape hatch.

`open-item` takes the account and item id shown by `events` and queues the same notice-only session a `session` verdict would; later messages of that conversation follow into it. Todoist tasks are refused (they go through their plan session), and so is an item that already has a job.

**Menu bar app (macOS):** `macos/HermesAutoBar` shows these decisions, source health and problems, and runs the commands above from buttons. See its README.

On macOS:

```sh
hermes-auto install-launchd
```

By default, the service checks for the `Hermes` desktop process every 60 seconds. Opening the app triggers a scan within that interval; in polling mode it then reads sources every `poll_seconds` (300) while open. With `"poll_sources": false` (see Relay queue and push mode) there is no periodic source check: webhooks and listeners trigger reads within seconds, and the timer only handles local upkeep and fallbacks. Closing it stops new scheduled scans (it does not interrupt an already running preparation). Change `desktop_process` if your app uses another process name. Set `desktop_only: false` for background polling independent of the app. `run` is always an explicit immediate run. `max_jobs_per_run` bounds each run; `agent_run_budget_seconds`/`agent_max_turns` bound full-agent jobs and `run_budget_seconds`/`max_turns` restricted ones.

Linux users can schedule `hermes-auto tick` through a user timer; set `desktop_only: false` on headless machines. Machines asleep/offline cannot process events until they resume.

Uninstall the macOS service with `launchctl bootout gui/$(id -u)/io.github.hermes-auto` and remove `~/Library/LaunchAgents/io.github.hermes-auto.plist`. Keep the state directory if you might reinstall: deleting it forfeits deduplication history. Backups contain private task/mail context and need the same care as the live database.

## Reliability and preparation policy

- SQLite transactions reserve an event and its random session ID **before** Hermes starts. No session lookup by title. Permanent event records have no age-based eviction.
- Session creation uses Hermes' session API and is separate from agent execution. Results use structured JSON. Timeout does not lose the reserved session ID.
- A stopped `creating`/`running` job becomes `needs_attention`. It is never silently delivered again. This is conservative crash recovery, not a claim of exactly-once model execution.
- All state-changing commands share a process lock. Source cursors and their enqueued events commit together. Missing or damaged state stops execution; it is not silently replaced. A deleted whole configuration directory cannot be distinguished from a new installation.
- Two execution profiles. **Full agent** (Todoist tasks): your normal Hermes toolsets, skills and rules; the only guards are the prompt's "prepare, don't act" rule and Hermes' approval gate in deny mode. **First turn** (every third-party matter: mail, also agent-labelled threads, Slack, GitHub, Linear, replies): the same session toolsets, but until you write in the session the `hermes-auto-guard` hook allows only grammar-checked reads within the session account's scope, skill reads and `session_search` in this profile; no terminal, code execution, delegation, web, memory, mailbox-writing or task-writing tools.
- Mail content is untrusted data in both profiles. A full-agent mail thread can reach your files and terminal, so use the agent label only for senders you trust. Trusted local Hermes plugins and the operating system remain outside this guard's threat model.
- State access is profile-specific. The package needs access to your configured Hermes installation; it is not a hosted connector and does not copy or distribute authentication.

## Development and tests

```sh
python -m pip install -e packages/hermes-auto -e packages/hermes-auto-todoist \
  -e packages/hermes-auto-gmail -e packages/hermes-auto-proton pytest ruff build
pytest -q
ruff check .
```

For the actual Hermes session API contract test, set `HERMES_AUTO_TEST_PYTHON` to Hermes' Python and `HERMES_AUTO_TEST_SOURCE` to its source directory, then run `pytest tests/test_real_hermes.py`. It exercises A→B→A profile isolation and idempotent session creation in temporary stores, without model calls. Normal CI does not pretend to test an installed Hermes instance it does not have.

Build each distribution using `python -m build packages/PACKAGE --outdir dist`. Releases attach all eight archives. See [architecture](docs/architecture.md) and [Polish quick start](docs/quickstart-pl.md).
