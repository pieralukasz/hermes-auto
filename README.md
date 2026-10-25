# Hermes Auto

Opt-in tasks and email replies become separate, resumable [Hermes Agent](https://github.com/NousResearch/hermes-agent) conversations.

Four independently installable Python distributions live in this repository:

| Package | Responsibility | Extra dependencies |
| --- | --- | --- |
| `hermes-auto` | Durable jobs, session IDs, preparation policy, CLI, scheduling | None |
| `hermes-auto-todoist` | Due tasks and recurring completion events through `td` | Core + authenticated Todoist CLI |
| `hermes-auto-gmail` | Replies in labelled Gmail threads | Core + Google auth + requests |
| `hermes-auto-proton` | Replies to labelled Proton conversations through Bridge | Core; Python's IMAP library |
| `hermes-auto-slack` | Sorter source: DMs, mentions and replies in followed threads (read-only) | Core; `requests` |

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
- The agent gets your normal Hermes setup: all configured toolsets, skills, memory, project rules and subagents, with the Desktop platform prompt (Markdown, tables, links). Hermes' one-shot limits (plain-terminal prompt, reduced skill guidance, `delegation.oneshot_max_children`) are lifted for these runs.
- Budget: 900 s / 60 turns by default (`agent_run_budget_seconds`, `agent_max_turns`). Commands that would need approval are denied, because nobody can approve them.

Write the goal, the skill to use and the limits ("don't send without my OK") in the description. `language` in `config.json` sets the answer language. Legacy mode labels (`hermes-open`, `hermes-draft`, `hermes-research`, `hermes-agent`) are still recognised for existing tasks; `hermes-open` creates the conversation without a model call. They are no longer created.

One-off tasks keep one event regardless of rescheduling. A recurring task starts a new occurrence only after a Todoist **completion event**, not because its due date changed. Activity pages are fetched completely with an overlap window. Failed reads do not advance the cursor. Tasks removed from the due/opt-in filter have unstarted work deferred; when eligible again, the same reserved job can proceed. Already prepared tasks never automatically repeat. Explicit `new-session` is available when you want another preparation.

**Archive = done.** When you archive a prepared session in Hermes, the next check completes its Todoist task (`td task complete`). Only an archive that happens after the session was seen open counts: sessions already archived when this check first saw them never complete anything. Already completed or deleted tasks are left alone. A recurring task is completed only while Todoist still shows the occurrence that session prepared (same due day) and has logged no completion of it since the job was queued, so archiving an old briefing, or a session whose agent already ticked the task, never skips a newer occurrence. The Todoist prompt lets the agent complete its own task once the work is actually done; sending, paying, publishing and other records stay gated, and work awaiting approval stays open. Superseded attempts (`new-session`) do not count. Disable per source with `"complete_on_archive": false`. Unarchiving does not reopen the task.

The Todoist activity API's available history limits recovery after a long period offline. This version does not backfill every missed occurrence: it prepares the current eligible occurrence and relies on available completion history. It will not promise historical reconstruction beyond the provider's retention.

## Gmail

Configure `sources.gmail.token_file` to an existing Google authorized-user OAuth JSON file. Polling requires `gmail.readonly`; the optional label-creation command needs a write-capable scope. You may instead create **`Hermes/Watch`** in Gmail yourself.

```sh
hermes-auto source setup gmail
```

Apply `Hermes/Watch` to a conversation you want to follow. Mail is written by third parties, so these runs stay restricted: the configured `mode` (`draft` by default: no tools; `research`: only `web_search`/`web_extract`), 180 s / 8 turns, no memory or project rules. To give one thread the full agent described under Todoist, also apply **`Hermes/Agent`** (`agent_label`); the message body is still marked as third-party content, never as instructions. The first poll records a baseline without creating sessions for old mail. Each later incoming reply creates one event keyed by account and Gmail message ID. Reading/unreading messages does not retrigger it. Own sent messages and drafts are excluded; configure `own_addresses` for additional aliases.

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

Apply **`Hermes Watch`** to a message in a conversation (or **`Hermes Agent`**, `agent_mailbox`, for the full agent, as with Gmail). Replies are matched through RFC `Message-ID`, `In-Reply-To` and `References`, not subject similarity. As with Gmail, the first poll establishes a baseline. Missing/broken threading headers cannot be reliably matched; those messages will not trigger automatically. The adapter reads up to 256 KiB of raw MIME per reply and supplies at most 20,000 text characters to Hermes. MIME may contain attachment bytes, but attachments are not extracted or supplied to the model. Read-only mailboxes and `BODY.PEEK` preserve read flags.

## Slack

A sorter source (needs `triage.enabled`). It reads Slack with your user token (the Slack MCP OAuth token Hermes stores in `~/.hermes/mcp-tokens/slack.json`; scopes: history and read for channels, groups, im, mpim, `users:read`, and the MCP search scopes) and never posts.

- **What counts:** DMs and group DMs, `@you` in channels, and replies in threads you follow (Slack subscribes you to threads you started, wrote in or were mentioned in). Bots, Slackbot, your own messages, edits and `@here`/`@channel` alone do not.
- **Bursts:** messages of one conversation (a DM or a thread) are handed over together once nobody wrote for `quiet_seconds` (90). If you read or answered it in Slack before that, nothing opens.
- **Decisions:** the classifier uses `triage.slack_criteria`: *session*, *brief* (a `## Slack` section) or *skip*. A later burst in the same conversation opens a `↩` session with the earlier one's context.
- **Session:** the automatic step researches with reads only (Slack MCP reads, `git`/`gh`/`linear` reads, `web_search`/`web_extract`, calendar, Todoist, past sessions) and ends with who wants what, the findings and a reply draft. Sending, writing files and every other tool get an error result; the session keeps its full toolset for your next turn, so sending happens only after you say so there.
- **Private channels:** `private_channels: "link_only"` (default) passes only author, place and link; `"full"` passes the text.
- **Instant (Socket Mode, optional):** `hermes-auto-slack-listen` keeps a Socket Mode connection with an app-level token (`xapp-`, scope `connections:write`; read by `app_token_command`, default: macOS keychain item `hermes-auto-slack-app`). The Slack app needs Socket Mode on and user event subscriptions `message.im`, `message.mpim`, `message.channels`, `message.groups`. On a relevant message it records ids only (`slack-listener.json`: hints, `nudge_at`, heartbeat, event count; no text) and runs `hermes-auto tick` after `quiet_seconds`; the source's `due` then makes it a Slack-only check. `hermes-auto-slack-listen check` tests the token, `install-launchd` installs `io.github.hermes-auto.slack-listener` (KeepAlive).
- **Cadence:** `sources.slack.poll_seconds` (120) runs Slack-only checks between the regular `poll_seconds` checks; once the listener is connected and has received an event, `listener_poll_seconds` (900) applies instead. Slack text never reaches the waiting-task check (no Todoist comments from Slack).

```bash
python -m pip install ./packages/hermes-auto-slack
hermes-auto source enable slack
```

The first check records a baseline; only later messages open sessions.

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

- **Mail (Gmail, Proton):** every new inbox message gets one decision. *Session*: a person expects your answer or action, so a conversation opens with a one- or two-sentence notice of who wrote and what they want. *Brief*: worth knowing, listed in the next brief at `brief_times`. *Skip*: marketing and noise. Adjust `mail_criteria` in plain language. Message notifications from LinkedIn and similar services count as mail from that person.
- **Replies go back to their conversation.** When someone answers mail you sent from a Hermes session, the reply opens a new session ("↩ <title>") carrying that conversation's context; the earlier conversation is never written to or unarchived.
- **Third-party mail runs read-only.** Both cases say what arrived, with context from reads: calendar list, Gmail search/get, Todoist list/view, past sessions, skills. Every other tool call and every terminal command that is not one plain read (no pipes, redirects or substitutions) is refused. No draft; drafting happens when you reply in the session, with full tools. Nothing is sent or changed.
- **Todoist Inbox:** a task that has sat in the Inbox for `settle_minutes` gets a full plan following your method (`triage.todoist.guidance`): list, priority (always), `hermes` when the agent can do it, `@czekam` when it waits on access, a mail or a person. Context comes from your recent Hermes conversations about the same matter (`context_days`, read-only). The plan is applied at once and listed in one "Inbox Todoist: posortowane" session with a one-line reason per task; reply "ok" or corrections. Nothing starts on its own (`auto_start: false`): tasks the agent could begin now are listed and start after your OK; a dated task gets the `hermes` label only then, since the due-date poller would start it.
- **Do organizacji and current work:** tasks in the saved filters `review_filters` (default "Do organizacji") get the same plan once. Tasks with `hermes` in `proposal_filters` (the four "Bieżące" views, Bonus included) and no due date are offered once in an "Agent może to zrobić" session; OK sets the due date to today and the poller opens one session per task. `@czekam`/`@zablokowane` never start.
- **Waiting tasks:** with new mail, one model call checks whether a mail delivers what an `@czekam`/`@zablokowane` task waits for (title, description, last comments). Ready: one "Odblokowane: …" session offering to drop `@czekam` (and start it if it has `hermes`). Partly: a Todoist comment on the task, no question. Each mail speaks up at most once per task.
- **Brief sections from local commands:** `triage.brief_sources` (e.g. a finance review) run at their brief time with `SINCE` set to their last success and print one JSON object: `title`, `summary` ("" = nothing new), `lines`, `questions` (`text`, `reason`, `proposal`), optional `propose_prompt`/`categories`/`context` (missing proposals are filled by one model call without tools), `evidence` (checked against `@czekam` tasks like mail) and `agent_note` (hidden instructions for the session). A failed run (server asleep) retries each check and posts on its own. You answer the questions in the brief session; the agent there applies them with full tools.
- `hermes-auto triage-preview --days 14` shows what the sorter would decide for recent mail and the current Inbox, without changing anything (it does spend model tokens). The report is written to `triage-preview.md` in the automation home.
- Sessions appear in the normal Sessions list. Optional: `"hide_sessions": true` plus the Desktop plugin in `desktop-plugin/hermes-auto` (copy to `~/.hermes/desktop-plugins/hermes-auto`) moves them to a separate **Auto** page.

## Operate

```sh
hermes-auto scan --dry-run  # temporary copy of state; no sessions, no baseline changes
hermes-auto scan            # record opt-ins/baselines and enqueue; no model calls
hermes-auto run             # scan and prepare eligible pending jobs now
hermes-auto status          # works while the service is running; includes source errors
hermes-auto pause
hermes-auto resume
hermes-auto retry 12        # explicitly continue needs_attention job in its existing session
hermes-auto new-session 12  # explicitly allocate a DIFFERENT conversation
hermes-auto backup
```

`retry` and `new-session` queue work; run it explicitly or wait for the scheduler. Automatic retries are deliberately disabled after an uncertain outcome: first inspect the existing conversation. Deleting a conversation does not erase the event record and does not automatically recreate it. `new-session` is the explicit escape hatch.

On macOS:

```sh
hermes-auto install-launchd
```

By default, the service checks for the `Hermes` desktop process every 60 seconds. Opening the app triggers a scan within that interval; while open it polls every 5 minutes. Closing it stops new scheduled scans (it does not interrupt an already running preparation). Change `desktop_process` if your app uses another process name. Set `desktop_only: false` for background polling independent of the app. `run` is always an explicit immediate run. `max_jobs_per_run` bounds each run; `agent_run_budget_seconds`/`agent_max_turns` bound full-agent jobs and `run_budget_seconds`/`max_turns` restricted ones.

Linux users can schedule `hermes-auto tick` through a user timer; set `desktop_only: false` on headless machines. Machines asleep/offline cannot process events until they resume.

Uninstall the macOS service with `launchctl bootout gui/$(id -u)/io.github.hermes-auto` and remove `~/Library/LaunchAgents/io.github.hermes-auto.plist`. Keep the state directory if you might reinstall: deleting it forfeits deduplication history. Backups contain private task/mail context and need the same care as the live database.

## Reliability and preparation policy

- SQLite transactions reserve an event and its random session ID **before** Hermes starts. No session lookup by title. Permanent event records have no age-based eviction.
- Session creation uses Hermes' session API and is separate from agent execution. Results use structured JSON. Timeout does not lose the reserved session ID.
- A stopped `creating`/`running` job becomes `needs_attention`. It is never silently delivered again. This is conservative crash recovery, not a claim of exactly-once model execution.
- All state-changing commands share a process lock. Source cursors and their enqueued events commit together. Missing or damaged state stops execution; it is not silently replaced. A deleted whole configuration directory cannot be distinguished from a new installation.
- Two execution profiles. **Full agent** (Todoist tasks, agent-labelled mail threads): your normal Hermes toolsets, skills and rules, by your explicit choice; the only guards are the prompt's "prepare, don't act" rule and Hermes' approval gate in deny mode. **Restricted** (watched mail): a tool allowlist enforced at both schema and execution-batch boundaries; drafts have no tools, research only the two web tools; no terminal, code execution, delegation, mailbox-writing or task-writing tools.
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
