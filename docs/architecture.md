# Architecture

`hermes-auto` owns the queue, runtime bridge, execution policy, status and scheduling. It imports no adapter modules and has no mailbox credentials or provider dependency. Python entry points discover only installed adapters.

```text
hermes-auto-todoist ──┐
hermes-auto-gmail ────┼── durable event queue ── reserved session ID ── Hermes session API
hermes-auto-proton ───┘                                  │
                                              full-agent or restricted worker
```

Adapter API: export a `Source` class with `defaults() -> dict`, `poll(config, store)` and `setup(config)`. Optionally `finish(config, job) -> str`: called once when the user archives the job's session after it was seen open (`ready`/`needs_attention` -> `done`); it closes the source item (Todoist completes the task) and must be idempotent against items already closed at the source. `poll` stays read-only. Register it under `[project.entry-points."hermes_auto.sources"]`. `poll` may read its provider and enqueue immutable source events. `setup` is an explicit user command for reversible opt-in label creation, never called by the scheduler. Source failures block that source's pending jobs without disabling unrelated sources.

Events use unique `(event_key, generation)` records. Normal polling uses generation zero. Only `new-session` allocates another generation and a new session ID. The title is presentation only. A user may rename or delete a Hermes conversation without changing event identity.

Lifecycle: `pending -> creating -> created -> running -> ready`. Open-only jobs go `created -> ready`. Errors become `needs_attention`; recovery converts abandoned `creating` and `running` states to the same status. Explicit retry uses `retry_pending` and the existing ID. A missing session becomes `deleted`; mail opt-out cancels pending work. A not-yet-created Todoist job can be `deferred` until its task qualifies again, preserving its reserved ID. No automatic model retry follows ambiguous completion.

The bridge runs in a child process with Hermes' dependencies. Runtime home (dependency activation) and session home (database/config) are separate, to support profile isolation. It uses `hermes_state_registry.acquire`, `create_session`, `append_message` and Hermes' single-query CLI implementation. Full-agent jobs (Todoist, agent-labelled mail) get in-process Desktop parity: platform `desktop` and Hermes' one-shot footprint (`agent/oneshot_footprint.py`) disabled, so prompt, skills guidance and delegation match an interactive session; the approval gate still reads the single-query marker and denies. Restricted jobs get an in-process guard that narrows the model's tools and blocks any disallowed batch before inline or registry dispatch. No installed Hermes source file is modified; these are in-memory patches in the bridge process and are version-sensitive.

The package has no exactly-once side-effect guarantee across an LLM and external systems. It avoids those effects during unattended preparation and stops on ambiguous outcomes. Recovery, deletion and deliberate regeneration are distinct actions.

Provider details stay in adapters. The core's `observe_stream` is provider-neutral: mark first observations as a baseline; persist seen item IDs and eligible events atomically. Gmail uses provider thread/message IDs. Proton maps RFC reply chains into stream/message IDs. Todoist uses task IDs plus the latest available completion timestamp for recurring occurrences.

Limits in 0.1: synchronous preparation with a configurable per-run cap; no push/webhook server; no attachment extraction; no cross-mail-provider deduplication. Adapter polling is intentionally opt-in. Versions are aligned for this first release, but distributions and dependencies are separate.

## Sorter (`triage`, opt-in)

`hermes_auto.triage` runs after the adapters' `poll` when `config.triage.enabled` is true. Adapters that implement it supply candidates; the core owns decisions, state and prompts.

- Mail adapters: `inbox(config, store, limit, since_ms=None) -> (candidates, cursor_key, cursor)`. A candidate carries headers, a text excerpt, `thread_id` and `sent` (the user's earlier sent messages in that thread: time and recipients). The first call only returns a baseline cursor. The cursor is committed together with the decisions, so a failed classification re-reads the same mail.
- A candidate in a thread the user wrote is routed to the Hermes session that sent it: the bridge's read-only `find_session` requires a correlated successful send receipt from a recognized `google_api.py gmail send/reply` tool invocation within a bounded window around each send time and resolves compression continuations. Unrecognized send mechanisms (including Proton sends) fall back to normal classification rather than guessing. Mere mentions of a subject or recipient and ambiguous matches do not route replies. The thread then stays mapped to that session (`meta thread_session:*`). Other candidates get one batched model call (`classify`, Hermes `call_llm`, provider/model from `triage.provider`/`triage.model`): `session` (enqueued job that posts a short notice), `brief` (collected) or `skip`.
- Third-party mail never runs with tools. Both a new mail session and a followup in an existing session run with the session's normal Desktop toolset (so the stored system prompt and tool pin stay complete for the user's later turns) while `deny_tool_execution (reads only, see read_only_command)` answers every call except `skill_view`/`skills_list` with an error result. Followups unarchive the target first and wait while the session was active in the last two minutes.
- Todoist: `inbox_tasks(settings)`, `outline(settings, store, hours)` (lists, sections, sample titles; cached) and `apply(settings, task, decision, label)`. A task is classified after it has sat in the Inbox for `settle_minutes`. With `triage.todoist.apply` the adapter moves it, sets an evident priority, adds the agent label and reads it back; a labelled task is only started when `auto_start` is on (then `triage_run`, which the due-date poller leaves alone); otherwise the questions session asks whether to start it. Open questions go into one `questions` session. This is the only scheduled source write besides `finish`.
- Decisions are persisted before Todoist writes. Failed applications retry their saved decision using a fresh full task read, including tasks already moved to the decided destination. A move by the user to a different project stops retries. Three failed attempts become `needs_attention`, visible alongside `user_changed` in `status.triage_issues`. Successful plans are durably queued with unique batch IDs. With `apply=false`, proposals are delivered in the brief without modifying tasks.
- Interrupted replies become `needs_attention`; `status` exposes them and `retry-followup ID` checks for prior delivery before explicitly queueing a retry.
- LinkedIn notifications use provider thread IDs only; names and links in the message body never join separate threads.
- Gmail drains all result pages with a timestamp/ID cursor and overlap; Proton decodes unknown charsets using a replacement-character fallback.
- At each `brief_times` slot, collected mail and unapplied task proposals become one `brief` session. `brief` and `questions` are internal sources: enabled with triage, never finished by archiving.
- Optional: `triage.hide_sessions` marks created sessions hidden; the Desktop plugin in `desktop-plugin/hermes-auto` then lists every `☀ ` session on its "Auto" page. Off by default.
- Full-agent jobs per check are capped by `max_agent_jobs_per_run` (default 1): a check holds the lock for the whole run.
