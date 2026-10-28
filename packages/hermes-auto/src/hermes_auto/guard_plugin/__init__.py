"""hermes-auto-guard: Hermes plugin for hermes-auto's automatic sessions (install: `hermes-auto install-guard`).

Registers the `hermes-auto` toolset (one grammar-checked read command, skill reads) and a `pre_tool_call`
hook keyed by session id. Sessions hermes-auto did not register are never touched: the hook returns None
at once, and only reads a session's parent when a "full" automatic turn is running (delegated children).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

from . import policy

logger = logging.getLogger(__name__)
_parents: dict = {}  # session id -> (parent id or None, checked at)
PARENT_TTL = 60.0


def _home():
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:  # noqa: BLE001 - outside Hermes (tests)
        return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _parent(home, session_id):
    """The parent row of `session_id` (a delegated child's parent, or the session a compression continued),
    read-only and cached; None when it has none or state.db cannot be read."""
    cached = _parents.get(session_id)
    if cached is not None and (cached[0] is not None or time.monotonic() - cached[1] < PARENT_TTL):
        return cached[0]
    try:
        conn = sqlite3.connect(f"file:{os.path.join(home, 'state.db')}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute("SELECT parent_session_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    parent = row[0] if row and row[0] else None
    _parents[session_id] = (parent, time.monotonic())
    return parent


def _entry(session_id):
    """The registration governing `session_id`: its own, else its nearest registered ancestor's (a delegated
    child, or the row a compression continued under a new id), so restrictions follow the conversation.
    Nothing is read beyond one directory listing while hermes-auto has registered no session."""
    home = _home()
    entry = policy.read_registration(home, session_id)
    if entry is not None or not policy.has_registrations(home):
        return entry
    current = session_id
    for _ in range(4):
        current = _parent(home, current)
        if not current:
            return None
        entry = policy.read_registration(home, current)
        if entry is not None:
            return entry
    return None


def _dangerous(command):
    from tools.approval import detect_dangerous_command
    return bool(detect_dangerous_command(command)[0])


def pre_tool_call(tool_name=None, args=None, session_id="", task_id="", **_):
    """Refuse what an automatic session may not run. An exception here is a refusal (Hermes fails a raising
    pre_tool_call closed), so only the cheap "not ours" path is guarded against errors."""
    sid = session_id or task_id or ""
    try:
        entry = _entry(sid)
    except Exception:  # noqa: BLE001 - never break the user's own sessions
        logger.debug("hermes-auto-guard: registry read failed for %s", sid, exc_info=True)
        if sid and os.path.exists(policy.registration_path(_home(), sid) or ""):
            raise
        return None
    if entry is None:
        return None
    message = policy.decide(tool_name, args, entry, dangerous=_dangerous)
    return {"action": "block", "message": message} if message else None


def _registered(kwargs):
    return _entry(kwargs.get("session_id") or kwargs.get("task_id") or "")


def read_tool(args, **kwargs):
    entry = _registered(kwargs)
    profile = entry["profile"] if entry else None
    if profile not in (policy.MAIL, policy.RESEARCH):
        return policy.neutral("Not run: hermes_auto_read works only in hermes-auto's automatic sessions.")
    return policy.run_read((args or {}).get("command"), profile)


def skill_view_tool(args, **kwargs):
    if _registered(kwargs) is None:
        return policy.neutral("Not run: this tool works only in hermes-auto's automatic sessions.")
    from tools.skills_tool import skill_view
    args = args or {}
    return skill_view(str(args.get("name") or ""), file_path=args.get("file_path") or None,
                      task_id=kwargs.get("task_id"))


def skills_list_tool(args, **kwargs):
    if _registered(kwargs) is None:
        return policy.neutral("Not run: this tool works only in hermes-auto's automatic sessions.")
    from tools.skills_tool import skills_list
    return skills_list(category=(args or {}).get("category") or None, task_id=kwargs.get("task_id"))


READ_SCHEMA = {
    "name": policy.READ_TOOL,
    "description": (
        "Run ONE read-only command in this automatic session (no shell: no pipes, redirects or chaining). "
        "Allowed: Todoist reads (`td today`, `td upcoming`, `td task list --filter 'search: phrase' --json`, "
        "`td task view id:ID --json`, `td project list`), calendar and Gmail reads via the Google Workspace "
        "skill script (`python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py calendar "
        "list --start ... --end ...`, `... gmail search \"query\"`, `... gmail get ID`) and, in Slack/GitHub "
        "sessions, Slack MCP reads, `gh` reads (pr/issue view/list, `gh api` GET) and `linear` reads. Anything "
        "else is refused with a note. Sending and writing wait for the user's OK."),
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
