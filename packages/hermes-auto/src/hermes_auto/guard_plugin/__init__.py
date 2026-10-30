"""hermes-auto-guard: Hermes plugin of hermes-auto's automatic sessions (install: `hermes-auto install-guard`).

Registers the `hermes-auto` toolset (one grammar-checked read command, raw skill reads) and a `pre_tool_call` hook
for the default profile (Dolores), where every automatic session lives. A session hermes-auto registered (it
carries someone else's content) may, until the user writes in it, only read: the read tool with the read families
of the session's account, skill reads and session_search in this profile when that account may search (the
`scope.json` beside the plugin, written at install from config.json `first_turn`). Every other session is
untouched. A session that cannot be told gets no tool at all (fail-closed).
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
    """This profile's scope.json, read from the active profile's plugin directory on each call (one module may
    serve several profiles in one gateway); None when missing or invalid."""
    return policy.load_scope(os.path.join(_home(), "plugins", policy.GUARD_NAME))


def _scope(session_id, data=None):
    """(read families, session_search allowed) for this session: the profile's scope and the session's account.
    Without a valid scope: nothing."""
    data = _installed() if data is None else data
    return policy.session_scope(data, _provider(session_id) if data and data.get("providers") else "")


def pre_tool_call(tool_name=None, args=None, session_id="", task_id="", **_):
    """Refuse what a locked automatic turn may not run. An exception here is a refusal (Hermes fails a raising
    pre_tool_call closed)."""
    sid = session_id or task_id
    state, why = policy.first_turn_state(_home(), sid)
    if state == policy.OPEN:
        return None
    if state == policy.UNKNOWN:  # cannot be told: every tool refused (fail-closed)
        return {"action": "block", "message": policy.REFUSED_UNKNOWN.format(why=why)}
    message = policy.decide_first_turn(tool_name, args, _scope(sid)[1])
    return {"action": "block", "message": message} if message else None


def read_tool(args, **kwargs):
    sid = kwargs.get("session_id") or kwargs.get("task_id") or ""
    families, _search = _scope(sid)
    chat = policy.session_chat(_home(), sid)  # a chat session also reads its own chat's history
    if not families and chat is None:
        return policy.neutral("Not run: no reads for this session (no read scope installed, or none for this "
                              "account); reinstall it with `hermes-auto install-guard` if that is wrong.")
    return policy.run_read((args or {}).get("command"), families, chat=chat)


def skill_view_tool(args, **kwargs):
    """The raw text of an enabled skill (policy.read_skill_text): Hermes' skill_view would render inline
    shell and install the skill's declared dependencies, which a read-only turn must not trigger."""
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
    result = skills_list(category=(args or {}).get("category") or None, task_id=kwargs.get("task_id"))
    return policy.redact_urls(result) if isinstance(result, str) else result


READ_SCHEMA = {
    "name": policy.READ_TOOL,
    "description": (
        "Run ONE read-only command (no shell: no pipes, redirects or chaining). Allowed, within the reads of this "
        "session's account: Todoist reads (`td today`, `td upcoming`, `td task list --filter 'search: phrase' "
        "--json`, `td task view id:ID --json`, `td project list`), calendar and Gmail reads via the Google "
        "Workspace skill script (`python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py "
        "calendar list --start ... --end ...`, `... gmail search \"query\"`, `... gmail get ID`), Slack MCP reads, "
        "`gh` reads (pr/issue view/list, `gh search prs --owner ORG --updated '>=2026-10-01'`: a value with < or > "
        "goes in single quotes; `gh api` GET), `linear` reads, and in a chat session that chat's history "
        "(`imsg history --chat-id N --json`, `hermes-auto-matrix history ROOM`). Anything else is refused with a "
        "note. Credentials in URLs of the output (token, signature, key and similar parameters) read REDACTED. "
        "In an automatic first turn this is how you read; sending and writing wait for the user."),
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
