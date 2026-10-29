"""hermes-auto-guard: Hermes plugin of hermes-auto's bots (install: `hermes-auto install-guard [profile]`).

Registers the `hermes-auto` toolset (one grammar-checked read command, raw skill reads) and a `pre_tool_call`
hook. What the hook allows comes from `scope.json` beside the plugin in the profile's home, written at install:

* read-only bots (e.g. `inbox`, mode `read_only`): every other tool is refused in every session of the profile
  (the profile's own toolset configuration is the first layer); the read families come from the scope, without
  one every read is refused. A subagent Dolores runs as this profile is one of its sessions.
* Dolores (`default`, mode `first_turn`): a session hermes-auto registered (third-party content) may, until the
  user writes in it, only delegate to the read-only subagent profile, search this profile's conversations and read
  skills; every other session is untouched. The read tool reads nothing here: the subagent reads.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3

from . import policy

logger = logging.getLogger(__name__)


def _home():
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:  # noqa: BLE001 - outside Hermes (tests)
        return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


POLICY_KEY = "gateway.local_policy.v1:"  # Hermes' immutable per-session creation policy in state_meta


def _provider(session_id):
    """The provider a session was created with: the `provider` of its frozen creation request (hermes-auto's
    `routes` account), read-only from this profile's state.db. "" when the request named none (the profile's own
    account); None when it cannot be told (the row or its policy is missing or unreadable)."""
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        conn = sqlite3.connect(f"file:{os.path.join(_home(), 'state.db')}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (POLICY_KEY + session_id,)).fetchone()
        finally:
            conn.close()
        saved = json.loads(row[0]) if row else None
        request = json.loads(saved["policy"]["request_json"] or "{}")
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return None
    if not isinstance(request, dict):
        return None
    provider = request.get("provider") or ""
    return provider if isinstance(provider, str) else None


def _installed():
    """This profile's scope.json (read from the active profile's plugin directory on each call: one module may
    serve several profiles in one gateway, and a delegated subagent runs in its own profile's scope)."""
    return policy.load_scope(os.path.join(_home(), "plugins", policy.GUARD_NAME))


def _scope(session_id, data=None):
    """(read families, session_search allowed) for this session: its own profile's scope and its account."""
    data = _installed() if data is None else data
    if data and data.get("mode") == policy.FIRST_TURN_MODE:
        return frozenset(), False
    return policy.session_scope(data, _provider(session_id) if data and data.get("providers") else "")


def pre_tool_call(tool_name=None, args=None, session_id="", task_id="", **_):
    """Refuse what this profile's mode does not allow. An exception here is a refusal (Hermes fails a raising
    pre_tool_call closed)."""
    data = _installed()
    sid = session_id or task_id
    if data and data.get("mode") == policy.FIRST_TURN_MODE:
        state, why = policy.first_turn_state(_home(), sid)
        if state == policy.OPEN:
            return None
        if state == policy.UNKNOWN:  # cannot be told: every tool refused (fail-closed)
            return {"action": "block", "message": policy.REFUSED_UNKNOWN.format(why=why)}
        message = policy.decide_first_turn(tool_name, args, data["subagent"])
        return {"action": "block", "message": message} if message else None
    search = True
    if tool_name == "session_search":
        search = _scope(sid, data)[1]
    message = policy.decide(tool_name, args, search)
    return {"action": "block", "message": message} if message else None


def read_tool(args, **kwargs):
    data = _installed()
    if data and data.get("mode") == policy.FIRST_TURN_MODE:
        return policy.neutral(f"Not run: this profile reads nothing through hermes-auto; the {data['subagent']} "
                              f"subagent does (delegate_task with profile \"{data['subagent']}\").")
    families, _search = _scope(kwargs.get("session_id") or kwargs.get("task_id") or "", data)
    if not families:
        return policy.neutral("Not run: no reads for this session (no read scope installed for this bot, or none for "
                              "this account); reinstall it with `hermes-auto install-guard` if that is wrong.")
    return policy.run_read((args or {}).get("command"), families)


def skill_view_tool(args, **kwargs):
    """The raw text of an enabled skill (policy.read_skill_text): Hermes' skill_view would render inline
    shell and install the skill's declared dependencies, which a read-only session must not trigger."""
    from tools.skills_tool import _skill_catalog
    args = args or {}
    name = str(args.get("name") or "").strip()
    matches = [item for item in _skill_catalog()
               if name and name in (item.get("load_name"), item.get("name"), item.get("relative_path"))]
    if len({str(item.get("path")) for item in matches}) != 1:
        return policy.neutral(f"No single enabled skill named {name!r}; use hermes_auto_skills_list.")
    return policy.read_skill_text(matches[0]["path"], args.get("file_path") or None)


def skills_list_tool(args, **kwargs):
    from tools.skills_tool import skills_list
    return skills_list(category=(args or {}).get("category") or None, task_id=kwargs.get("task_id"))


READ_SCHEMA = {
    "name": policy.READ_TOOL,
    "description": (
        "Run ONE read-only command in this read-only bot (no shell: no pipes, redirects or chaining). "
        "Allowed: Todoist reads (`td today`, `td upcoming`, `td task list --filter 'search: phrase' --json`, "
        "`td task view id:ID --json`, `td project list`), calendar and Gmail reads via the Google Workspace "
        "skill script (`python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py calendar "
        "list --start ... --end ...`, `... gmail search \"query\"`, `... gmail get ID`) and, in Slack/GitHub "
        "(only the reads this bot was installed with) Slack MCP reads, `gh` reads (pr/issue view/list, `gh api` "
        "GET) and `linear` reads. Anything else is refused with a note. Sending and writing happen in Dolores."),
    "parameters": {"type": "object", "properties": {
        "command": {"type": "string", "description": "One plain read command."}}, "required": ["command"]},
}
SKILL_VIEW_SCHEMA = {
    "name": policy.SKILL_VIEW_TOOL,
    "description": "Read a skill (SKILL.md) or one file inside it, by skill name. Read-only.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "Skill name, e.g. google-workspace."},
        "file_path": {"type": "string", "description": "Optional file inside the skill, e.g. references/x.md."}},
        "required": ["name"]},
}
SKILLS_LIST_SCHEMA = {
    "name": policy.SKILLS_LIST_TOOL,
    "description": "List the installed skills (name and description). Read-only.",
    "parameters": {"type": "object", "properties": {
        "category": {"type": "string", "description": "Optional category filter."}}},
}


def register(ctx):
    for schema, handler, emoji in ((READ_SCHEMA, read_tool, "🔎"), (SKILL_VIEW_SCHEMA, skill_view_tool, "📚"),
                                   (SKILLS_LIST_SCHEMA, skills_list_tool, "📚")):
        ctx.register_tool(name=schema["name"], toolset=policy.TOOLSET, schema=schema, handler=handler, emoji=emoji)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    logger.info("hermes-auto-guard %s loaded", policy.GUARD_VERSION)


__all__ = ["register", "pre_tool_call", "read_tool"]
