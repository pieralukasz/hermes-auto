# Working on Hermes Auto

- Keep the four Python distributions separate. The core must not depend on Todoist, Gmail or Proton packages. Adapters use the `hermes_auto.sources` entry-point contract.
- Never commit live config, credentials, message bodies, session databases or service logs. Use synthetic fixtures and `.test` email addresses.
- Do not modify the user's Hermes Agent source installation. Changes to the subprocess bridge require the opt-in real Hermes API contract test, not only mocks.
- `pytest -q` and `ruff check .` are the baseline checks. Build all four wheels and source archives before a release. Validate core + Todoist installation without mail dependencies.
- Do not claim exactly-once model execution. Preserve durable event identity and stop on ambiguous completion. Explicit retry and fresh-session creation must stay distinct.
- Polling adapters are read-only. The only source writes besides `source setup` are `finish`, triggered by the user archiving a session that was seen open, and the opt-in Todoist sorter (`triage.todoist.apply`), which moves, prioritises and labels Inbox tasks and reads each one back. Mail adapters stay read-only in every mode. Creating opt-in labels belongs only in the explicit `source setup` command. Never send email as a test.
- Third-party content must never execute tools beyond reads. Its sessions are created through the gateway with the `hermes-auto-guard` read toolset only, and the plugin's `pre_tool_call` hook is the second layer (`guard_plugin/policy.py`). Never write to Hermes' state.db from the bridge and never patch Hermes.
- Real-session tests must use temporary stores or a clearly identified synthetic session cleaned up through Hermes' API/CLI. Do not alter existing user conversations.
