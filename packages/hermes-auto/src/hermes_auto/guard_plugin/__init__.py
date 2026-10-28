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


MAX_ANCESTORS = 64


def _parent(home, session_id):
    """The parent row of `session_id` (a delegated child's parent, or the session a compression continued),
    read-only and cached; None when it has none. Raises sqlite3.Error when state.db cannot be read: "no
    parent" and "could not tell" are different answers."""
    cached = _parents.get(session_id)
    if cached is not None and (cached[0] is not None or time.monotonic() - cached[1] < PARENT_TTL):
        return cached[0]
    conn = sqlite3.connect(f"file:{os.path.join(home, 'state.db')}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT parent_session_id FROM sessions WHERE id=?", (session_id,)).fetchone()
    finally:
        conn.close()
    parent = row[0] if row and row[0] else None
    _parents[session_id] = (parent, time.monotonic())
    return parent


def _owner(session_id):
    """(session id holding the registration, its registration) governing `session_id`: its own, else its
    nearest registered or latched ancestor's (a delegated child, or the row a compression continued under a
    new id), so restrictions follow the conversation. (session_id, None) when it is not an automatic session.
    Nothing is read beyond one directory listing while hermes-auto has registered no session."""
    home = _home()
    entry = policy.read_registration(home, session_id)
    if entry is not None or session_id in _latch or not (policy.has_registrations(home) or _latch):
        return session_id, entry
    current, seen = session_id, {session_id}
    for _ in range(MAX_ANCESTORS):
        current = _parent(home, current)
        if not current or current in seen:
            return session_id, None
        seen.add(current)
        entry = policy.read_registration(home, current)
        if entry is not None or current in _latch:
            return current, entry
    return session_id, None  # a lineage this deep is a long-lived conversation of the user's


def _entry(session_id, turn_id=""):
    owner, entry = _owner(session_id)
    return _latched(owner, entry, turn_id)


def _dangerous(command):
    from tools.approval import detect_dangerous_command
    return bool(detect_dangerous_command(command)[0])


def _gated_write(paths):
    """Whether Hermes would ask before this write (SSH client config, protected instruction files)."""
    from agent.file_safety import is_write_approval_required
    from tools.file_tools_write_guards import _protected_instruction_config, _protected_instruction_reason
    enabled, extra = _protected_instruction_config()
    resolved = [os.path.join(os.path.expanduser("~"), os.path.expanduser(path)) for path in paths]
    return any(is_write_approval_required(path)
               or (enabled and _protected_instruction_reason(path, enabled=enabled, extra_patterns=extra))
               for path in resolved)


def _resolver(task_id):
    """A path resolved the way the file tools resolve it (the task's working directory, which a terminal
    `cd` moves), so a relative `config` after `cd ~/.ssh` is checked as ~/.ssh/config."""
    from tools.file_tools_write_guards import _resolved_or_raw
    return lambda path: _resolved_or_raw(str(path), task_id or "default")


# Full-run restrictions seen in this gateway process: owner session -> (until, turn ids seen while active).
# A full run could rewrite or delete its own registration file (same user); while the deadline the plugin
# saw has not passed, the turns that ran under it stay restricted whatever the file says now. Another turn
# (the user's own, after the run) follows the file, so it is released as soon as the bridge lifts it.
_latch: dict = {}


def _latched(owner, entry, turn_id, now=None):
    now = now or time.time()
    if entry is not None and entry["profile"] == policy.FULL and policy.active(entry, now):
        until, turns = _latch.get(owner, (0, frozenset()))
        _latch[owner] = (max(until, entry["until"]), turns | {turn_id})
        return entry
    held = _latch.get(owner)
    if entry is not None and entry["profile"] != policy.FULL:
        return entry  # a read or private profile is stricter than any full-run restriction
    if held is not None and held[0] > now and turn_id in held[1]:
        return {"version": 1, "profile": policy.FULL, "until": held[0], "job": None}
    if held is not None and held[0] <= now:
        _latch.pop(owner, None)
    return entry


def pre_tool_call(tool_name=None, args=None, session_id="", task_id="", turn_id="", **_):
    """Refuse what an automatic session may not run. An exception here is a refusal (Hermes fails a raising
    pre_tool_call closed). When the session's lineage cannot be read, the call is refused only if it may
    be ours: the session itself is registered, or a "full" automatic turn is running right now."""
    sid = session_id or task_id or ""
    home = _home()
    try:
        entry = _entry(sid, turn_id or "")
    except Exception:  # noqa: BLE001 - never break the user's own sessions without cause
        logger.warning("hermes-auto-guard: could not resolve session %s", sid, exc_info=True)
        if ((sid and os.path.exists(policy.registration_path(home, sid) or "")) or policy.active_full_registration(home)
                or any(until > time.time() for until, _turns in _latch.values())):
            raise
        return None
    if entry is None:
        return None
    message = policy.decide(tool_name, args, entry, dangerous=_dangerous, gated_write=_gated_write,
                            resolve=_resolver(task_id) if entry["profile"] == policy.FULL else None)
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
    """The raw text of an enabled skill (policy.read_skill_text): Hermes' skill_view would render inline
    shell and install the skill's declared dependencies, which a read-only session must not trigger."""
    if _registered(kwargs) is None:
        return policy.neutral("Not run: this tool works only in hermes-auto's automatic sessions.")
    from tools.skills_tool import _skill_catalog
    args = args or {}
    name = str(args.get("name") or "").strip()
    matches = [item for item in _skill_catalog()
               if name and name in (item.get("load_name"), item.get("name"), item.get("relative_path"))]
    if len({str(item.get("path")) for item in matches}) != 1:
        return policy.neutral(f"No single enabled skill named {name!r}; use hermes_auto_skills_list.")
    return policy.read_skill_text(matches[0]["path"], args.get("file_path") or None)


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
