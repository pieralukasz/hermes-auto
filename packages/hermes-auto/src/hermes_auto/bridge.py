"""Executed by Hermes' Python, not the package's Python. No changes to Hermes source files."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def guard_agent(agent_class, allowed):
    """Guard the batch boundary, including inline tools that bypass model_tools dispatch."""
    original = agent_class._execute_tool_calls

    def guarded(self, assistant_message, *args, **kwargs):
        calls = (assistant_message.get("tool_calls", []) if isinstance(assistant_message, dict)
                 else getattr(assistant_message, "tool_calls", []) or [])
        for call in calls:
            function = call.get("function", {}) if isinstance(call, dict) else call.function
            name = function.get("name") if isinstance(function, dict) else function.name
            if name not in allowed:
                raise RuntimeError(f"Hermes Auto preparation policy blocked tool: {name}")
        return original(self, assistant_message, *args, **kwargs)

    agent_class._execute_tool_calls = guarded
    init = agent_class.__init__

    def restricted_init(self, *args, **kwargs):
        kwargs["skip_background_review"] = True
        kwargs["skip_memory"] = True
        init(self, *args, **kwargs)
        self.tools = [tool for tool in self.tools if tool.get("function", {}).get("name") in allowed]
        self.valid_tool_names = {tool["function"]["name"] for tool in self.tools}

    agent_class.__init__ = restricted_init


def desktop_parity(agent_class):
    """Make an unattended full-tools run indistinguishable from a new Desktop session.

    `cli.main(oneshot=True)` marks the process as a finite one-shot run, which Hermes uses to (a) render
    the prompt for a plain terminal ("Markdown does NOT render"), (b) drop the skill-loading guidance and
    skill_manage, and (c) cap delegation at delegation.oneshot_max_children. The result is read in the
    Desktop app, so the agent gets the desktop platform prompt and the interactive skill/delegation rules.
    The approval gate keeps reading the env marker directly, so dangerous commands are still denied.
    """
    import agent.oneshot_footprint as footprint
    footprint.is_single_query_session = lambda: False
    footprint.prune_oneshot_tools = lambda tools: list(tools)
    init = agent_class.__init__

    def desktop_init(self, *args, **kwargs):
        if kwargs.get("platform") in (None, "cli"):
            kwargs["platform"] = "desktop"
        init(self, *args, **kwargs)

    agent_class.__init__ = desktop_init


SHELL_META = set(";|&<>`$\n\r\\(){}")
GOOGLE_API = "/skills/productivity/google-workspace/scripts/google_api.py"
GOOGLE_READS = {("calendar", "list"), ("gmail", "search"), ("gmail", "get"), ("gmail", "labels")}
TODOIST_READS = {("today",), ("upcoming",), ("inbox",), ("completed",), ("activity",),
                 ("task", "list"), ("task", "view"), ("project", "list"), ("project", "view"),
                 ("section", "list"), ("label", "list"), ("filter", "list"), ("filter", "view")}
READ_TOOLS = frozenset({"skill_view", "skills_list", "session_search"})


def read_only_command(command):
    """True only for a single plain read command: calendar/Gmail reads via the Google script, Todoist reads.

    No shell syntax at all (pipes, redirects, substitutions, chaining), so the allowed program is the
    only thing that runs. Anything else is refused, however harmless it looks.
    """
    import shlex
    text = str(command or "").strip()
    # Single-quoted text is literal in sh (Todoist filters use `|` and `&`); everything else may not
    # contain shell syntax.
    bare = re.sub(r"'[^']*'", "''", text)
    if not text or "'" in bare.replace("''", "") or SHELL_META & set(bare):
        return False
    try:
        tokens = shlex.split(text)
    except ValueError:
        return False
    if not tokens:
        return False
    program = Path(tokens[0]).name
    if program == "td":
        return any(tuple(tokens[1:1 + len(read)]) == read for read in TODOIST_READS)
    if program in ("python", "python3") and len(tokens) >= 4 and tokens[1].endswith(GOOGLE_API):
        return (tokens[2], tokens[3]) in GOOGLE_READS
    return False


def deny_tool_execution(agent_class, allowed=READ_TOOLS):
    """Keep the session's normal tool list (and so its stored prompt) but execute only reads.

    Used when third-party mail enters a conversation: every call outside `allowed`, and every
    terminal command that is not a plain read (`read_only_command`), gets an error result instead
    of running, and the turn continues. Filtering the tool list instead would rebuild the stored
    system prompt with a reduced toolset, and the user's later turns in that session would lose
    their tools.
    """
    from agent.turn_tool_validation import _append_tool_error_results
    original = agent_class._execute_tool_calls

    def permitted(call):
        name = call.function.name
        if name in allowed:
            return True
        if name != "terminal":
            return False
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except (TypeError, ValueError):
            return False
        return not arguments.get("background") and read_only_command(arguments.get("command"))

    def guarded(self, assistant_message, messages, *args, **kwargs):
        calls = list(getattr(assistant_message, "tool_calls", None) or [])
        if all(permitted(call) for call in calls):
            return original(self, assistant_message, messages, *args, **kwargs)
        _append_tool_error_results(messages, calls, lambda call: json.dumps({
            "error": "Only reads are allowed in this automatic step (third-party mail): calendar list, "
                     "gmail search/get, td list/view/today, session_search, skills. Nothing ran in this batch."}))
        return None

    agent_class._execute_tool_calls = guarded
    init = agent_class.__init__

    def quiet_init(self, *args, **kwargs):
        kwargs["skip_background_review"] = True
        init(self, *args, **kwargs)

    agent_class.__init__ = quiet_init


def parse_json_block(text):
    """The first JSON value in a model reply, tolerating fences and prose around it."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    decoder = json.JSONDecoder()
    for start, char in enumerate(text):
        if char in "[{":
            try:
                return decoder.raw_decode(text[start:])[0]
            except ValueError:
                continue
    raise ValueError("Model reply contained no JSON")


def classify(config, request):
    from agent.auxiliary_client import call_llm
    triage = config.get("triage", {})
    route = {}
    response = call_llm(task="hermes_auto_triage", provider=triage.get("provider") or "anthropic",
                        model=triage.get("model") or "claude-sonnet-4-6",
                        messages=[{"role": "system", "content": request["system"]},
                                  {"role": "user", "content": request["user"]}],
                        max_tokens=int(request.get("max_tokens") or 4000), temperature=0, timeout=180,
                        route_info=route)
    return {"result": parse_json_block(response.choices[0].message.content), "route": route}


SUBJECT_PREFIX = ("re:", "fwd:", "fw:", "odp:", "pd:", "aw:", "wg:")


def normalized_subject(subject):
    text = " ".join(str(subject or "").split())
    while text.casefold().startswith(SUBJECT_PREFIX):
        text = text.split(":", 1)[1].strip()
    return text


def rowid_at(conn, timestamp):
    """Smallest message rowid whose timestamp is >= `timestamp` (rowids grow with time)."""
    lo, hi = conn.execute("SELECT MIN(id), MAX(id) FROM messages").fetchone()
    if lo is None:
        return 0
    while lo < hi:
        mid = (lo + hi) // 2
        row = conn.execute("SELECT timestamp FROM messages WHERE id>=? ORDER BY id LIMIT 1", (mid,)).fetchone()
        if row is None or row[0] >= timestamp:
            hi = mid
        else:
            lo = mid + 1
    return lo


AUTOMATION_TITLE = "☀ %"


def recent_sessions(db_path, request):
    """User conversations active in the last `days` (newest first): id, title, last activity date."""
    import sqlite3
    import time
    since = time.time() - float(request.get("days") or 21) * 86400
    conn = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute(
            "SELECT id, title, COALESCE(last_activity_at, started_at) AS active FROM sessions "
            "WHERE COALESCE(last_activity_at, started_at) >= ? AND parent_session_id IS NULL "
            "AND COALESCE(title,'') != '' AND title NOT LIKE ? AND title NOT LIKE 'Subagent:%' "
            "ORDER BY active DESC LIMIT ?", (since, AUTOMATION_TITLE, int(request.get("limit") or 400))).fetchall()
    finally:
        conn.close()
    return [{"id": sid, "title": title, "date": time.strftime("%Y-%m-%d", time.localtime(active))}
            for sid, title, active in rows]


def session_excerpts(db_path, request):
    """Title, date and the latest user and assistant messages of each session (bounded)."""
    import sqlite3
    import time
    conn = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=10)
    out = {}
    try:
        for sid in list(request.get("ids") or [])[:30]:
            row = conn.execute("SELECT title, COALESCE(last_activity_at, started_at) FROM sessions WHERE id=?",
                               (sid,)).fetchone()
            if row is None:
                continue
            last = {}
            for role in ("user", "assistant"):
                found = conn.execute("SELECT content FROM messages WHERE session_id=? AND role=? AND "
                                     "length(content) > 20 ORDER BY id DESC LIMIT 1", (sid, role)).fetchone()
                last[role] = " ".join(str(found[0] if found else "").split())[:800]
            out[sid] = {"title": row[0], "date": time.strftime("%Y-%m-%d", time.localtime(row[1] or 0)),
                        "last_user": last["user"], "last_assistant": last["assistant"]}
    finally:
        conn.close()
    return out


def find_session(db_path, request):
    """Which Hermes session sent the message this reply answers? Read-only, newest send first.

    A send from a session leaves the subject or the recipient in that session's tool calls or
    results within minutes of Gmail/Proton recording it as sent. Searching a bounded rowid window
    around each send time keeps this to a few thousand rows instead of the whole history.
    """
    import sqlite3
    conn = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        subject = normalized_subject(request.get("subject"))
        needles = [value for value in (request.get("thread_id"), subject if len(subject) >= 6 else "")
                   if value]
        for sent in sorted(request.get("sent", []), key=lambda item: -item["ts"]):
            first = rowid_at(conn, sent["ts"] - 1800)
            last = rowid_at(conn, sent["ts"] + 300)
            terms = needles + [address for address in sent.get("to", []) if "@" in address]
            if not terms:
                continue
            clauses = " OR ".join("instr(coalesce(content,''),?) OR instr(coalesce(tool_calls,''),?)"
                                  for _ in terms)
            params = [value for term in terms for value in (term, term)]
            rows = conn.execute(
                f"SELECT session_id, timestamp FROM messages WHERE id BETWEEN ? AND ? AND ({clauses})",
                (first, last, *params)).fetchall()
            sessions = {}
            for session_id, timestamp in rows:
                distance = abs(timestamp - sent["ts"])
                sessions[session_id] = min(sessions.get(session_id, distance), distance)
            excluded = set(request.get("exclude", []))
            ranked = sorted((distance, session_id) for session_id, distance in sessions.items()
                            if session_id not in excluded)
            if ranked:
                return ranked[0][1]
        return None
    finally:
        conn.close()


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    config = request["config"]
    os.environ["HERMES_HOME"] = config.get("hermes_runtime_home", config["hermes_home"])
    for key in ("HERMES_YOLO", "HERMES_KANBAN_TASK", "HERMES_KANBAN_GOAL_MODE"):
        os.environ.pop(key, None)
    sys.path.insert(0, config["hermes_source"])
    import hermes_bootstrap  # noqa: F401
    os.environ["HERMES_HOME"] = config["hermes_home"]
    from hermes_state_registry import acquire

    db = acquire(Path(config["hermes_home"]) / "state.db")
    action = request["action"]
    job = request.get("job", {})
    sid = job.get("session_id")
    if action == "doctor":
        from run_agent import AIAgent
        from cli import main as cli_main
        import inspect
        assert callable(AIAgent._execute_tool_calls)
        assert "output_format" in inspect.signature(cli_main).parameters
        # Sorter dependencies: no-tools turns and the classifier's model call.
        from agent.turn_tool_validation import _append_tool_error_results
        from agent.auxiliary_client import call_llm
        assert callable(_append_tool_error_results) and callable(call_llm)
        assert callable(db.set_session_hidden) and callable(db.set_session_archived)
        print(json.dumps({"ok": True, "python": sys.executable, "source": config["hermes_source"]}))
        db.close()
        return
    if action == "archive_states":
        # Read-only: None = session missing, otherwise whether the user archived it (whole lineage).
        states = {}
        for item in request.get("session_ids", []):
            row = db.get_session(item)
            states[item] = None if row is None else bool(row.get("archived"))
        print(json.dumps({"states": states}))
        db.close()
        return
    if action == "classify":
        db.close()
        print(json.dumps(classify(config, request), ensure_ascii=False))
        return
    if action in ("recent_sessions", "session_excerpts"):
        db.close()
        reader = recent_sessions if action == "recent_sessions" else session_excerpts
        print(json.dumps({"result": reader(Path(config["hermes_home"]) / "state.db", request)}, ensure_ascii=False))
        return
    if action == "find_session":
        found = find_session(Path(config["hermes_home"]) / "state.db", request)
        if found:
            # A compression continuation carries the conversation on under a newer id.
            found = db.resolve_resume_session_id(found) or found
        print(json.dumps({"session_id": found}))
        db.close()
        return
    session = db.get_session(sid)
    if action == "inspect":
        print(json.dumps({"exists": session is not None,
                          "messages": session.get("message_count", 0) if session else 0,
                          "archived": bool(session.get("archived")) if session else False,
                          "last_active": session.get("last_activity_at") if session else None}))
        db.close()
        return
    if action == "unarchive":
        # Bring the user's conversation back before a reply lands in it.
        if session is None:
            raise RuntimeError("Target session is missing")
        if session.get("archived"):
            db.set_session_archived(sid, False)
        db.close()
        print(json.dumps({"session_id": sid, "archived": False}))
        return
    if action == "create":
        if session is None:
            db.create_session(sid, source="cli")
        if session is None or not session.get("title"):
            # A failed earlier create can leave an untitled session; finish it instead of skipping.
            db.set_session_title(sid, request["title"][:db.MAX_TITLE_LENGTH])
        if request.get("hidden"):
            # Listed in the Desktop "Auto" page instead of the main Sessions list.
            db.set_session_hidden(sid, True)
        if job["mode"] == "open" and not db.get_messages(sid):
            db.append_message(sid, "user", content=request["prompt"])
            db.append_message(sid, "assistant", content=request["open_message"])
        db.close()
        print(json.dumps({"session_id": sid}))
        return
    if action != "run" or session is None:
        raise RuntimeError("Target session is missing; it will not be recreated")
    db.close()
    from run_agent import AIAgent
    import cli
    import toolsets
    # Only this worker's in-memory configuration changes. Interactive Hermes keeps its settings.
    # Unattended runs never get interactive approval: dangerous commands are denied.
    cli.CLI_CONFIG["approvals"] = {"mode": "manual", "single_query_mode": "deny"}
    if request.get("deny_tools"):
        # Third-party mail inside a normal session: the session keeps its full toolset and prompt,
        # only plain reads (calendar, Gmail search/get, Todoist, past sessions, skills) execute.
        desktop_parity(AIAgent)
        deny_tool_execution(AIAgent)
        cli.main(query=request["prompt"], resume=sid, quiet=True, oneshot=True, output_format="stream-json",
                 max_turns=config["max_turns"], run_budget=config["run_budget_seconds"])
        return
    if request.get("full_tools"):
        # Todoist tasks and mail the user labelled for the agent: normal toolsets, skills and project rules.
        desktop_parity(AIAgent)
        cli.main(query=request["prompt"], resume=sid, quiet=True, oneshot=True, output_format="stream-json",
                 max_turns=config["agent_max_turns"], run_budget=config["agent_run_budget_seconds"])
        return
    allowed = {"web_search", "web_extract"} if job["mode"] in ("research", "agent") else set()
    guard_agent(AIAgent, allowed)
    toolsets.TOOLSETS["hermes-auto-preparation"] = {
        "description": "Hermes Auto bounded preparation", "tools": sorted(allowed), "includes": [],
    }
    cli.CLI_CONFIG["tool_search"] = {"enabled": "off"}
    cli.main(query=request["prompt"], resume=sid, quiet=True, oneshot=True,
             output_format="stream-json", toolsets="hermes-auto-preparation", ignore_rules=True,
             max_turns=config["max_turns"], run_budget=config["run_budget_seconds"])


if __name__ == "__main__":
    main()
