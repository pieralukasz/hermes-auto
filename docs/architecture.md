# Architecture

`hermes-auto` owns the queue, runtime bridge, execution policy, status and scheduling. It imports no adapter modules and has no mailbox credentials or provider dependency. Python entry points discover only installed adapters.

```text
hermes-auto-todoist ──┐
hermes-auto-gmail ────┼── durable event queue ── reserved session ID ── Hermes session API
hermes-auto-proton ───┘                                  │
                                              bounded preparation worker
```

Adapter API: export a `Source` class with `defaults() -> dict`, `poll(config, store)` and `setup(config)`. Register it under `[project.entry-points."hermes_auto.sources"]`. `poll` may read its provider and enqueue immutable source events. `setup` is an explicit user command for reversible opt-in label creation, never called by the scheduler. Source failures block that source's pending jobs without disabling unrelated sources.

Events use unique `(event_key, generation)` records. Normal polling uses generation zero. Only `new-session` allocates another generation and a new session ID. The title is presentation only. A user may rename or delete a Hermes conversation without changing event identity.

Lifecycle: `pending -> creating -> created -> running -> ready`. Open-only jobs go `created -> ready`. Errors become `needs_attention`; recovery converts abandoned `creating` and `running` states to the same status. Explicit retry uses `retry_pending` and the existing ID. A missing session becomes `deleted`; removed opt-ins can cancel pending work. No automatic model retry follows ambiguous completion.

The bridge runs in a child process with Hermes' dependencies. Runtime home (dependency activation) and session home (database/config) are separate, to support profile isolation. It uses `hermes_state_registry.acquire`, `create_session`, `append_message` and Hermes' single-query CLI implementation. An in-process preparation guard narrows the model's tools and blocks any disallowed batch before inline or registry dispatch. No installed Hermes source is patched.

The package has no exactly-once side-effect guarantee across an LLM and external systems. It avoids those effects during unattended preparation and stops on ambiguous outcomes. Recovery, deletion and deliberate regeneration are distinct actions.

Provider details stay in adapters. The core's `observe_stream` is provider-neutral: mark first observations as a baseline; persist seen item IDs and eligible events atomically. Gmail uses provider thread/message IDs. Proton maps RFC reply chains into stream/message IDs. Todoist uses task IDs plus the latest available completion timestamp for recurring occurrences.

Limits in 0.1: synchronous preparation with a configurable per-run cap; no push/webhook server; no inbox-wide automatic classification; no attachment extraction; no cross-mail-provider deduplication; no hosted UI. Adapter polling is intentionally opt-in. Versions are aligned for this first release, but distributions and dependencies are separate.
