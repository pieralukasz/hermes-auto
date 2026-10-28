"""Executed by Hermes' Python, not the package's Python. No changes to Hermes source files.

Hermes >= v0.22.5 runs every turn in the gateway that owns ~/.hermes (the only writer of its sessions), so
this bridge is a gateway client: sessions are created and run over the gateway's RPC (`session.create`
with a frozen launch policy, `prompt.submit`), never by patching an in-process agent or writing state.db.
Reads (inspect, archive states, sorter context, reply provenance) stay read-only SQLite queries.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

GUARD_DIR = Path(__file__).with_name("guard_plugin")
SHELL_META = set(";|&<>`$\n\r\\(){}")
GOOGLE_API = "skills/productivity/google-workspace/scripts/google_api.py"
REFUSAL_MARK = "hermes-auto-refusal:"
TITLE_LIMIT = 100  # Hermes SessionDB.MAX_TITLE_LENGTH
# Launch fields a run compares with the session's frozen creation request (`launch_request`).
LAUNCH_KEYS = ("source", "toolsets", "max_turns", "run_budget", "ignore_rules", "provider", "model")


class Refusal(RuntimeError):
    """A run or create the bridge refuses before anything reaches the session; the message is for the user."""


def guard_policy():
    """The guard plugin's policy module, loaded by path under a name nothing else uses (this script runs
    with `-I`, so hermes_auto is not importable here, and its sibling modules would shadow Hermes')."""
    module = sys.modules.get("hermes_auto_guard_policy")
    if module is None:
        spec = importlib.util.spec_from_file_location("hermes_auto_guard_policy", GUARD_DIR / "policy.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["hermes_auto_guard_policy"] = module
        spec.loader.exec_module(module)
    return module


def unique_title(set_title, title, limit=TITLE_LIMIT):
    """Hermes keeps titles unique; a second "GitHub · Aviad: review" becomes "GitHub · Aviad: review (2)".

    `set_title(candidate)` raises on a clash with a message containing "already in use" (SessionDB) or the
    gateway's `title_conflict`; any other error propagates."""
    for attempt in range(1, 50):
        suffix = "" if attempt == 1 else f" ({attempt})"
        candidate = title[:limit - len(suffix)].rstrip() + suffix
        try:
            set_title(candidate)
            return candidate
        except Exception as exc:  # noqa: BLE001 - only a title clash is retried
            if not any(mark in str(exc) for mark in ("already in use", "title_conflict", "title_in_use")):
                raise
    raise RuntimeError("No free session title")


def route_kwargs(request):
    """Launch fields for the job's `routes` rule (provider/model); empty keeps Hermes' configured model."""
    route = request.get("route") or {}
    return {key: route[key] for key in ("provider", "model") if route.get(key)}


def launch_plan(request, config):
    """(guard profile or None, session.create fields) for a job's automatic session.

    The toolsets are the first boundary and the gateway freezes them: third-party mail and Slack/GitHub
    research get only the guard plugin's read toolset and past-session search (no terminal, files,
    skill_manage or anything that sends), private content no tools, bounded preparation web only (research
    and agent modes) or nothing. Full-tools jobs keep the profile's normal Desktop toolsets."""
    policy = guard_policy()
    job = request.get("job") or {}
    profile = policy.profile_for(request.get("deny_tools"), request.get("full_tools"))
    if profile in (policy.MAIL, policy.RESEARCH):
        toolsets = [policy.TOOLSET, "session_search"]
    elif profile == policy.PRIVATE:
        toolsets = []
    elif profile == policy.FULL:
        toolsets = None
    else:
        toolsets = ["web"] if job.get("mode") in ("research", "agent") else []
    agent_limits = profile in (policy.RESEARCH, policy.FULL)
    params = {"max_turns": int(config["agent_max_turns" if agent_limits else "max_turns"]),
              "run_budget": int(config["agent_run_budget_seconds" if agent_limits else "run_budget_seconds"])}
    if toolsets is not None:
        params["toolsets"] = toolsets
    if profile is None:
        params["ignore_rules"] = True  # bounded preparation: no project rules, as before
    params.update(route_kwargs(request))
    return profile, params


def launch_mismatch(launch, params):
    """The launch fields where a session's frozen creation request differs from what this job needs."""
    expected = {"source": "gui", **params}
    launch = dict(launch or {})
    if "model_requested" in launch:
        launch["model"] = launch["model_requested"]
    return [key for key in LAUNCH_KEYS if launch.get(key) != expected.get(key)]


def raw_settings(config):
    """This home's config.yaml exactly as written, parsed strictly. Hermes' own readers fall back to defaults or the
    last good copy on a broken file; a readiness check must refuse instead, so it reads the file itself."""
    path = Path(config["hermes_home"]) / "config.yaml"
    if not path.is_file():
        return {}
    from utils import fast_safe_load  # Hermes' own strict YAML reader (raises on a parse error)
    try:
        data = fast_safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except Exception as exc:  # noqa: BLE001 - any read or parse error refuses
        raise Refusal(f"Refused: {path} cannot be read ({exc.__class__.__name__}); nothing was sent.") from None
    if not isinstance(data, dict):
        raise Refusal(f"Refused: {path} is not a mapping; nothing was sent.")
    return data


def approvals_unattended_unsafe(config=None):
    """True when this profile's approval settings would let an automatic turn run a dangerous command
    (`approvals.mode: off`, or `single_query_mode: approve`). An automatic turn is finite, so with any other
    mode Hermes resolves an approval-gated action from `approvals.single_query_mode` (deny) at once. Read from
    the file itself (defaults as Hermes: mode smart, single_query_mode deny); an unreadable file refuses."""
    if config is None:
        return True
    approvals = raw_settings(config).get("approvals") or {}
    if not isinstance(approvals, dict):
        return True
    mode = approvals.get("mode", "smart")
    mode = ("off" if mode is False else "manual") if isinstance(mode, bool) else str(mode).strip().lower()
    single = approvals.get("single_query_mode", "deny")
    single = ("approve" if single is True else "deny") if isinstance(single, bool) else str(single).strip().lower()
    return mode == "off" or single == "approve" or os.environ.get("HERMES_YOLO_MODE") not in (None, "", "0")


# What a read-only bot's profile must switch off and may switch on (its `platform_toolsets.cli`).
READ_ONLY_DISABLED = frozenset({"terminal", "file", "browser", "web", "code_execution", "computer_use", "delegation",
                                "cronjob", "memory", "skills", "image_gen", "tts", "kanban", "connections"})


def read_only_toolsets():
    return frozenset({guard_policy().TOOLSET, "session_search"})


def plugin_version():
    match = re.search(r'^version:\s*"?([^"\n]+)"?', (GUARD_DIR / "plugin.yaml").read_text(), re.M)
    return match.group(1).strip() if match else ""


def bot_of(config):
    return config.get("bot") or {"name": "default", "read_only": False, "reads": []}


def expected_scope(config):
    """scope.json as `hermes-auto install-guard` writes it for this bot (guard.scope_data, without importing it)."""
    bot = bot_of(config)
    data = {"version": 1, "reads": sorted(set(bot.get("reads") or []))}
    if bot.get("providers"):
        data["providers"] = {name: {"reads": sorted(set(spec.get("reads") or [])),
                                    "session_search": bool(spec.get("session_search", False))}
                             for name, spec in sorted(bot["providers"].items())}
    return data


def profile_problems(config):
    """Why this home is not a ready read-only bot, read from its config.yaml as Hermes reads it (empty: ready).
    The boundary is the profile's own configuration: only the hermes-auto read toolset and session_search,
    everything that runs code, touches files, browses or sends switched off, the guard plugin enabled."""
    from agent.skill_utils import parse_config_string_list
    home = Path(config["hermes_home"])
    if not (home / "config.yaml").is_file():
        return [f"profile {bot_of(config)['name']!r} does not exist ({home})"]
    settings = raw_settings(config)
    problems = []
    platform = set(parse_config_string_list((settings.get("platform_toolsets") or {}).get("cli")))
    if not platform or not platform <= read_only_toolsets():
        problems.append(f"platform_toolsets.cli must be a subset of {sorted(read_only_toolsets())}, "
                        f"is {sorted(platform) or 'unset (all tools)'}")
    disabled = set(parse_config_string_list((settings.get("agent") or {}).get("disabled_toolsets")))
    if READ_ONLY_DISABLED - disabled:
        problems.append(f"agent.disabled_toolsets lacks {sorted(READ_ONLY_DISABLED - disabled)}")
    memory = settings.get("memory") or {}
    if memory.get("memory_enabled", True) is not False or memory.get("user_profile_enabled", True) is not False:
        problems.append("memory.memory_enabled and memory.user_profile_enabled must be false (no memory written "
                        "from third-party content)")
    review = (settings.get("auxiliary") or {}).get("background_review") or {}
    if review.get("enabled", True) is not False:
        problems.append("auxiliary.background_review.enabled must be false (no memory or skill patches after a turn)")
    if guard_policy().GUARD_NAME not in parse_config_string_list((settings.get("plugins") or {}).get("enabled")):
        problems.append("plugins.enabled lacks hermes-auto-guard")
    scope = guard_policy().load_scope(home / "plugins" / guard_policy().GUARD_NAME)
    if scope != expected_scope(config):
        problems.append("the installed read scope differs from hermes-auto's config; run `hermes-auto install-guard`")
    return problems


async def guard_status(client):
    """What the RUNNING gateway loaded of the guard plugin for this profile (`plugins.list` answers from the
    profile's own plugin manager)."""
    policy = guard_policy()
    listing = await client.rpc("plugins.list")
    for plugin in listing.get("plugins") or []:
        if policy.GUARD_NAME in (plugin.get("name"), str(plugin.get("key") or "").rsplit("/", 1)[-1]):
            ok = (plugin.get("enabled") is True and not plugin.get("error") and int(plugin.get("hooks") or 0) >= 1
                  and int(plugin.get("tools") or 0) >= len(policy.PLUGIN_TOOLS)
                  and plugin.get("version") == plugin_version())
            return {"loaded": True, "ok": ok, **{key: plugin.get(key) for key in
                                                 ("version", "enabled", "hooks", "tools", "error")},
                    "expected_version": plugin_version()}
    return {"loaded": False, "ok": False, "expected_version": plugin_version()}


def guard_message(status):
    if not status.get("loaded"):
        return "the running Hermes gateway has not loaded the hermes-auto-guard plugin for this bot"
    if status.get("version") != status.get("expected_version"):
        return (f"the running gateway has hermes-auto-guard {status.get('version')!r} for this bot, this hermes-auto "
                f"needs {status.get('expected_version')!r}")
    return f"hermes-auto-guard is loaded but not active ({status.get('error') or 'disabled or incomplete'})"


INTERNAL_SOURCES = ("brief", "questions", "proposals", "unblocked")  # triage.INTERNAL_SOURCES


def third_party(profile, job, request):
    """Whether a job carries someone else's content: a read profile, or anything that is neither the user's own
    full-tools job nor an internal source (a brief or plan hermes-auto wrote)."""
    return profile in guard_policy().DENY_PROFILES or (
        profile is None and not request.get("full_tools") and job.get("source") not in INTERNAL_SOURCES)


async def require_bot(client, config, profile, job, request):
    """Fail closed before anything reaches a session (create, import or run): third-party content only in a ready
    read-only bot; a full-tools automatic turn only in a working bot whose approvals deny what nobody can approve.
    Without a `bots` section (no `bot` in the request) only the old bounded preparation, with no tools, passes."""
    bot = bot_of(config)
    retry = f"Nothing was sent. Fix it, then `hermes-auto new-session {job.get('id', 'JOB_ID')}`."
    if bot.get("read_only"):
        if profile == guard_policy().FULL:
            raise Refusal(f"Refused: a full-tools job cannot run in the read-only bot {bot['name']!r}. {retry}")
        problems = profile_problems(config)
        status = await guard_status(client)
        if not status["ok"]:
            problems.append(guard_message(status) + f" (`hermes-auto guard-reload {bot['name']}`)")
        if problems:
            raise Refusal(f"Refused: bot {bot['name']!r} is not a ready read-only bot: {'; '.join(problems)}. {retry}")
    elif third_party(profile, job, request) and (profile is not None or config.get("bot")):
        raise Refusal(f"Refused: third-party content goes only to a read-only bot, not to profile "
                      f"{bot['name']!r} (configure `bots` in hermes-auto's config.json). {retry}")
    elif profile == guard_policy().FULL and approvals_unattended_unsafe(config):
        raise Refusal(f"Refused: bot {bot['name']!r} has approvals.mode off (or single_query_mode approve), so "
                      "an automatic turn would run approval-gated commands without anyone. Set `hermes "
                      f"-p {bot['name']} config set approvals.mode manual` (single_query_mode stays deny) "
                      f"or route these tasks to another bot. {retry}")


def run_gateway(work):
    """Run `work(client)` attached to this home's gateway (started if needed, as `hermes chat` does)."""
    from hermes_cli.gateway_client import connect_gateway

    async def attached():
        async with connect_gateway() as client:
            return await work(client)
    return asyncio.run(attached())


def title_owner(hermes_home, title):
    """Read-only: the session holding `title`, or None (Hermes keeps titles unique)."""
    import sqlite3
    path = Path(hermes_home, "state.db")
    if not path.exists():
        return None  # a fresh home: the gateway creates state.db with its first session
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        row = conn.execute("SELECT id FROM sessions WHERE title=? LIMIT 1", (title,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


async def rename(client, hermes_home, sid, title):
    """Title the session through the gateway; a title another session holds takes the next " (n)".

    The gateway answers a clash only with a generic refusal, so the clash is read from state.db."""
    snapshot = await client.rpc("session.resume", session_id=sid)

    async def set_title(candidate):
        owner = title_owner(hermes_home, candidate)
        if owner == sid:
            return
        if owner is not None:
            raise ValueError("already in use")
        try:
            await client.rpc("session.mutate", session_id=sid, request_id=f"hermes-auto-title:{sid}:{candidate}",
                             expected_revision=snapshot["revision"], operation="rename", payload={"title": candidate})
        except Exception:
            if title_owner(hermes_home, candidate) not in (None, sid):
                raise ValueError("already in use") from None  # taken meanwhile
            raise
    for attempt in range(1, 50):
        suffix = "" if attempt == 1 else f" ({attempt})"
        candidate = title[:TITLE_LIMIT - len(suffix)].rstrip() + suffix
        try:
            await set_title(candidate)
            return candidate
        except ValueError:
            continue
    raise RuntimeError("No free session title")


def create_agent_session(request, config, job, db):
    """A gateway session for an automatic run in the job's bot profile (this home): frozen toolsets, turn limit,
    budget and model route, then the title. Idempotent: the creation key is the job's reserved id."""
    profile, params = launch_plan(request, config)
    home = config["hermes_home"]

    async def work(client):
        contract = (await client.rpc("runtime.describe")).get("session_create") or {}
        missing = sorted(set(params) - set(contract.get("parameters") or []))
        if "gui" not in (contract.get("sources") or []) or missing:
            raise Refusal("Refused: this Hermes gateway cannot create automatic sessions with "
                          f"{', '.join(missing) or 'source gui'}; update Hermes.")
        await require_bot(client, config, profile, job, request)
        snapshot = await client.rpc("session.create", request_id=f"hermes-auto:{job['session_id']}",
                                    source="gui", cwd=str(Path.home()), **params,
                                    **({"hidden": True} if request.get("hidden") else {}))
        sid = snapshot["stored_session_id"]
        row = db.get_session(sid) or {}
        if not row.get("title"):
            await rename(client, home, sid, request["title"])
        return sid
    return run_gateway(work)


def create_open_session(request, config, job, db):
    """An `open` job (brief, plan, proposals): the prompt and a ready message, no run. The gateway imports
    it as a finished conversation under the job's reserved id; the user continues it with normal tools."""
    sid = job["session_id"]
    if db.get_session(sid) is not None:
        return sid
    def free(candidate):
        if title_owner(config["hermes_home"], candidate) is not None:
            raise ValueError("already in use")
    title = unique_title(free, request["title"])
    now = time.time()
    session = {"id": sid, "source": "cli", "title": title, "started_at": now, "hidden": int(bool(request.get("hidden"))),
               "messages": [{"role": "user", "content": request["prompt"], "timestamp": now},
                            {"role": "assistant", "content": request["open_message"], "timestamp": now + 0.001}]}
    route = route_kwargs(request)
    if route:
        # Desktop resumes a session on model / model_config: a routed brief keeps its account and model.
        session["model_config"] = dict(route)
        if route.get("model"):
            session["model"] = route["model"]

    async def work(client):
        policy = guard_policy()
        await require_bot(client, config, policy.profile_for(request.get("deny_tools"), request.get("full_tools")),
                          job, request)
        return await client.rpc("session.mutate", session_id=sid, request_id=f"hermes-auto-import:{sid}",
                                expected_revision=0, operation="import", payload={"sessions": [session]})
    result = run_gateway(work)
    if sid not in (result.get("imported_ids") or []) + (result.get("skipped_ids") or []):
        raise RuntimeError("Hermes did not import the session")
    return sid


def run_session(request, config, job):
    """Submit the job's prompt to its gateway session as one finite turn and print the stream-json result.

    Refused (nothing submitted) when the session's frozen launch differs from what this job needs (an older
    session, created by hand or before Hermes v0.22.5), or when its bot is not ready for it (`require_bot`)."""
    sid = job["session_id"]
    profile, params = launch_plan(request, config)
    prompt, display = request["prompt"], request.get("display")

    async def work(client):
        await require_bot(client, config, profile, job, request)
        snapshot = await client.rpc("session.resume", session_id=sid)
        different = launch_mismatch((snapshot.get("info") or {}).get("launch_request"), params)
        if different:
            raise Refusal(f"Refused: this session's frozen launch differs from the job's ({', '.join(different)}); "
                          f"nothing was sent. Use `hermes-auto new-session {job.get('id', 'JOB_ID')}`.")
        from hermes_cli.gateway_chat_view import GatewayChatView
        from hermes_cli.stream_json import StreamJsonEmitter
        rpc = client.rpc

        async def submit_with_card(method, **fields):
            if method == "prompt.submit" and display and display != prompt:
                # The transcript shows the card; the model is sent the full prompt (kept as api_content).
                fields = {**fields, "text": display, "api_text": prompt}
            return await rpc(method, **fields)
        client.rpc = submit_with_card
        emitter = StreamJsonEmitter(model=str((snapshot.get("info") or {}).get("model") or ""))
        emitter.bind_session(sid)
        view = GatewayChatView(client, snapshot, quiet=True, emitter=emitter)
        view.unattended = False  # finite, never auto-approving: no one can answer, so approvals resolve to deny
        return await view.run(prompt, oneshot=True)

    return run_gateway(work)


def doctor(config, db):
    """Import and contract checks without a model call for this home's bot: the gateway client, its creation
    contract, and for a read-only bot its profile configuration and the guard plugin in the running gateway;
    for a working bot whether automatic turns would auto-approve."""
    from hermes_cli.gateway_chat_view import GatewayChatView
    from hermes_cli.stream_json import StreamJsonEmitter
    from hermes_state import SessionDB
    from agent.auxiliary_client import call_llm
    assert callable(call_llm) and callable(GatewayChatView.run) and callable(StreamJsonEmitter.emit_result)
    assert callable(SessionDB.get_compression_lineage) and callable(SessionDB.resolve_resume_session_id)
    assert "read_only" in SessionDB.__init__.__code__.co_varnames
    bot = bot_of(config)
    if not (Path(config["hermes_home"]) / "config.yaml").is_file() and bot["name"] != "default":
        return {"ok": False, "ready": False, "bot": bot["name"], "problems": [f"profile {bot['name']!r} does not exist"]}

    async def work(client):
        contract = (await client.rpc("runtime.describe")).get("session_create") or {}
        needed = {"toolsets", "max_turns", "run_budget", "provider", "model", "cwd", "ignore_rules"}
        missing = sorted(needed - set(contract.get("parameters") or []))
        if "gui" not in (contract.get("sources") or []) or missing:
            raise Refusal(f"Hermes gateway lacks the creation contract hermes-auto needs: {missing or 'source gui'}")
        return await guard_status(client) if bot.get("read_only") else None
    guard = run_gateway(work)
    report = {"ok": True, "bot": bot["name"], "home": config["hermes_home"], "read_only": bool(bot.get("read_only")),
              "python": sys.executable, "source": config["hermes_source"]}
    if bot.get("read_only"):
        problems = profile_problems(config) + ([] if guard["ok"] else [guard_message(guard)])
        report.update(guard=guard, reads=bot.get("reads"), problems=problems, ready=not problems)
    else:
        unsafe = approvals_unattended_unsafe(config)
        report.update(approvals_off=unsafe, ready=not unsafe,
                      problems=["approvals.mode off: automatic Todoist runs here are refused"] if unsafe else [])
    return report


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
    chosen = route_kwargs(request)
    route = {}
    response = call_llm(task="hermes_auto_triage",
                        provider=chosen.get("provider") or triage.get("provider") or "anthropic",
                        model=chosen.get("model") or triage.get("model") or "claude-sonnet-5-5",
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


def live_owner(session_id):
    """Surface (e.g. "desktop") holding a live lease on this session, else None.

    Hermes refuses a second writer (SESSION_NOT_OWNED) while e.g. Desktop keeps the conversation's runtime;
    Desktop holds every conversation opened since it started until ~6 h idle. Unreadable registry: None,
    and the run's own refusal is still caught by the caller.
    """
    try:
        from hermes_cli.active_sessions import active_session_registry_snapshot
        for entry in active_session_registry_snapshot():
            if entry.get("session_id") == session_id:
                return entry.get("surface") or "another window"
    except Exception:  # noqa: BLE001
        return None
    return None


AUTOMATION_TITLE = "☀ %"  # sessions from before titles lost the prefix


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
            "ORDER BY active DESC LIMIT ?", (since, AUTOMATION_TITLE, int(request.get("limit") or 400) + len(request.get("exclude") or []))).fetchall()
    finally:
        conn.close()
    exclude = set(request.get("exclude") or [])
    return [{"id": sid, "title": title, "date": time.strftime("%Y-%m-%d", time.localtime(active))}
            for sid, title, active in rows if sid not in exclude]


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


GOOGLE_SENDS = {("gmail", "send"), ("gmail", "reply")}
# Quoted text the shell takes literally: single quotes, and double quotes without $, ` or \.
LITERAL_QUOTES = re.compile(r"'[^']*'|\"[^\"$`\\\\]*\"")


def google_send_command(command):
    """True only for one plain `python …/google_api.py gmail send|reply …` invocation.

    The Google Workspace skill script is the only known sender (`gmail send` / `gmail reply`; both
    print `{"status": "sent", "id": …, "threadId": …}`). Anything else (drafts, other tools, reads,
    chained or substituted shell) is not a send this function recognises, so routing fails closed.
    """
    import shlex
    text = str(command or "").strip()
    bare = LITERAL_QUOTES.sub("''", text)
    if not text or "'" in bare.replace("''", "") or '"' in bare or SHELL_META & set(bare):
        return False
    try:
        tokens = shlex.split(text)
    except ValueError:
        return False
    return (len(tokens) >= 4 and re.fullmatch(r"python3?(\.\d+)?", Path(tokens[0]).name) is not None
            and tokens[1].endswith(GOOGLE_API) and (tokens[2], tokens[3]) in GOOGLE_SENDS)


def send_calls(tool_calls):
    """{tool_call_id} of recognised send invocations in an assistant row's tool_calls JSON."""
    try:
        calls = json.loads(tool_calls or "[]")
    except (TypeError, ValueError):
        return set()
    found = set()
    for call in calls if isinstance(calls, list) else []:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or function.get("name") != "terminal" or not call.get("id"):
            continue
        arguments = function.get("arguments")
        try:
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            continue
        if (isinstance(arguments, dict) and not arguments.get("background")
                and google_send_command(arguments.get("command"))):
            found.add(str(call["id"]))
    return found


def send_receipt(content, thread_id, message_ids):
    """True when a terminal result is exactly a successful Gmail send receipt for this thread
    (receipt threadId) or for one of the user's sent messages (receipt id)."""
    try:
        outer = json.loads(content or "")
        receipt = json.loads(str(outer.get("output") or "").strip()) if isinstance(outer, dict) else None
    except (TypeError, ValueError):
        return False
    if not isinstance(receipt, dict) or outer.get("exit_code") != 0 or receipt.get("status") != "sent":
        return False
    return bool((thread_id and receipt.get("threadId") == thread_id)
                or (receipt.get("id") and str(receipt["id"]) in message_ids))


def provenance_sessions(rows, thread_id, message_ids):
    """Sessions holding a recognised send call AND its successful receipt (same session, same
    tool_call_id). A receipt without its call, a call without its receipt, or any read never counts."""
    calls = {}
    for session_id, role, content, tool_calls, tool_call_id in rows:
        if role == "assistant":
            for call_id in send_calls(tool_calls):
                calls[call_id] = session_id
    return {session_id for session_id, role, content, tool_calls, tool_call_id in rows
            if role == "tool" and tool_call_id and calls.get(str(tool_call_id)) == session_id
            and send_receipt(content, thread_id, message_ids)}


def find_session(db_path, request):
    """Which Hermes session sent the message this reply answers? Read-only; fails closed.

    Only a parsed send action with its correlated success receipt naming this thread (or the sent
    message) counts, within a bounded window around each send time (see `provenance_sessions`).
    Subjects, recipients, prose and reads are no proof: the user may have replied by hand. Mail
    sent any other way is not routed. If more than one session shows provenance, nothing is returned.
    """
    import sqlite3
    conn = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        if not {"role", "content", "tool_calls", "tool_call_id", "timestamp", "session_id"} <= columns:
            return None
        thread_id = str(request.get("thread_id") or "")
        thread_id = thread_id if len(thread_id) >= 6 else ""
        excluded = set(request.get("exclude", []))
        for sent in sorted(request.get("sent", []), key=lambda item: -item["ts"]):
            message_ids = {str(sent["id"])} if sent.get("id") else set()
            if not thread_id and not message_ids:
                continue
            first = rowid_at(conn, sent["ts"] - 1800)
            last = rowid_at(conn, sent["ts"] + 300)
            rows = conn.execute(
                "SELECT session_id, role, content, tool_calls, tool_call_id FROM messages WHERE id BETWEEN ? "
                "AND ? AND timestamp BETWEEN ? AND ? AND role IN ('tool','assistant') ORDER BY id",
                (first, last, sent["ts"] - 1800, sent["ts"] + 300)).fetchall()
            sessions = provenance_sessions(rows, thread_id, message_ids) - excluded
            if len(sessions) == 1:
                return sessions.pop()
            if sessions:
                return None  # Ambiguous: never guess which conversation gets a third party's reply.
        return None
    finally:
        conn.close()


def delivery_scope(db, session_id):
    """Every session id the conversation spans: the whole compression lineage (root to live tip)
    plus the resume target. None when Hermes cannot tell, so a retry is refused rather than guessed."""
    try:
        lineage = list(db.get_compression_lineage(session_id) or [])
        tip = db.resolve_resume_session_id(session_id)
        if tip and tip not in lineage:
            lineage += list(db.get_compression_lineage(tip) or []) or [tip]
    except Exception:  # noqa: BLE001 - a missing or failing lineage API is no proof of absence
        return None
    ids = list(dict.fromkeys([session_id, *lineage]))
    return ids if lineage else None


def reply_delivered(db_path, session_ids, needles):
    """Read-only: does one user message in these sessions contain every needle (a routed reply's
    date and sender)? Independent of how the prompt framed the mail."""
    import sqlite3
    ids = [sid for sid in dict.fromkeys(session_ids) if sid]
    needles = [str(needle) for needle in needles or [] if needle]
    if not needles or not ids:
        return False
    conn = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        marks = ",".join("?" for _ in ids)
        # The transcript may show a short card; the sent text (with the reply's date and sender) is api_content.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        text = ("(coalesce(content,'') || char(10) || coalesce(api_content,''))" if "api_content" in columns
                else "coalesce(content,'')")
        found = " AND ".join(f"instr({text}, ?) > 0" for _ in needles)
        return conn.execute(f"SELECT 1 FROM messages WHERE session_id IN ({marks}) AND role='user' AND {found} "
                            "LIMIT 1", (*ids, *needles)).fetchone() is not None
    finally:
        conn.close()


class ReadOnlySessions:
    """Hermes' SessionDB opened read-only, on first use (a fresh home has no state.db until the gateway
    creates its first session). The gateway is the only writer; nothing here can migrate or checkpoint."""

    def __init__(self, path):
        self.path, self.db = Path(path), None

    def _open(self):
        if self.db is None and self.path.exists():
            from hermes_state import SessionDB
            self.db = SessionDB(db_path=self.path, read_only=True)
        return self.db

    def get_session(self, session_id):
        db = self._open()
        return db.get_session(session_id) if db is not None and session_id else None

    def get_compression_lineage(self, session_id):
        return self._open().get_compression_lineage(session_id)

    def resolve_resume_session_id(self, session_id):
        db = self._open()
        return db.resolve_resume_session_id(session_id) if db is not None else None

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    config = request["config"]
    os.environ["HERMES_HOME"] = config.get("hermes_runtime_home", config["hermes_home"])
    for key in ("HERMES_YOLO", "HERMES_YOLO_MODE", "HERMES_KANBAN_TASK", "HERMES_KANBAN_GOAL_MODE",
                "HERMES_TUI_GATEWAY_URL"):
        os.environ.pop(key, None)
    sys.path.insert(0, config["hermes_source"])
    import hermes_bootstrap  # noqa: F401
    os.environ["HERMES_HOME"] = config["hermes_home"]
    # Only run/create import run_agent, which loads the profile's .env; classify calls call_llm directly,
    # so a routed provider's key_env (e.g. a work account) resolved to the no-key placeholder → 401.
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv(hermes_home=config["hermes_home"])
    db = ReadOnlySessions(Path(config["hermes_home"]) / "state.db")
    action = request["action"]
    job = request.get("job", {})
    sid = job.get("session_id")
    try:
        if action == "doctor":
            print(json.dumps(doctor(config, db)))
            return
        if action == "reload_plugins":
            # The gateway's own `reload-plugins` control verb (what `hermes plugins enable` sends): plugins
            # are re-discovered in the running gateway, open conversations keep running.
            from gateway.control_socket import reload_gateway_plugins
            home = Path(config["hermes_home"])
            root = home.parent.parent if bot_of(config)["name"] != "default" else home
            answer = reload_gateway_plugins(root, profile_home=home) or {"reloaded": False,
                                                                         "error": "no gateway answered"}
            answer = {key: answer.get(key) for key in ("reloaded", "error", "plugins")}
            answer["guard"] = run_gateway(guard_status) if answer.get("reloaded") else None
            print(json.dumps(answer))
            return
        if action == "archive_states":
            # Read-only: None = session missing, otherwise whether the user archived it (whole lineage).
            states = {}
            for item in request.get("session_ids", []):
                row = db.get_session(item)
                states[item] = None if row is None else bool(row.get("archived"))
            print(json.dumps({"states": states}))
            return
        if action == "classify":
            print(json.dumps(classify(config, request), ensure_ascii=False))
            return
        if action in ("recent_sessions", "session_excerpts"):
            reader = recent_sessions if action == "recent_sessions" else session_excerpts
            print(json.dumps({"result": reader(Path(config["hermes_home"]) / "state.db", request)}, ensure_ascii=False))
            return
        if action == "find_session":
            found = find_session(Path(config["hermes_home"]) / "state.db", request)
            if found:
                # A compression continuation carries the conversation on under a newer id.
                found = db.resolve_resume_session_id(found) or found
            print(json.dumps({"session_id": found}))
            return
        session = db.get_session(sid)
        if action == "inspect":
            info = {"exists": session is not None, "owned_by": live_owner(sid),
                    "messages": session.get("message_count", 0) if session else 0,
                    "archived": bool(session.get("archived")) if session else False,
                    "last_active": session.get("last_activity_at") if session else None}
            if request.get("needles"):
                # Retry of an interrupted reply: was its text already appended anywhere in the compression
                # lineage? None (unknown) when the lineage cannot be read; the caller then refuses.
                ids = delivery_scope(db, sid) if session is not None else None
                info["delivered"] = (reply_delivered(Path(config["hermes_home"]) / "state.db", ids, request["needles"])
                                     if ids else None)
            print(json.dumps(info))
            return
        if action == "create":
            if job["mode"] == "open":
                print(json.dumps({"session_id": create_open_session(request, config, job, db)}))
                return
            if session is not None:
                # A retry before anything was sent: the gateway session already exists under this id; the run
                # compares its frozen launch with this job's before submitting anything.
                print(json.dumps({"session_id": sid}))
                return
            print(json.dumps({"session_id": create_agent_session(request, config, job, db)}))
            return
        if action != "run" or session is None:
            raise RuntimeError("Target session is missing; it will not be recreated")
        db.close()  # nothing here writes state.db; the gateway owns the session from now on
        db = None
        sys.exit(run_session(request, config, job))
    except Refusal as exc:
        print(f"{REFUSAL_MARK} {exc}", file=sys.stderr, flush=True)
        sys.exit(3)
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    main()
