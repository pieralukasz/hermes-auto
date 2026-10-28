"""hermes-auto-guard: Hermes plugin of the read-only inbox bots (install: `hermes-auto install-guard`).

Enabled only in the profiles of those bots (e.g. `inbox`, `tapin-inbox`), never in Dolores' or a working bot's
profile. Registers the `hermes-auto` toolset (one grammar-checked read command, raw skill reads) and a
`pre_tool_call` hook that refuses every other tool in every session of that profile; the profile's own
toolset configuration is the first layer. The read families come from `scope.json` beside the plugin in the
profile's home, written at install; without it every read is refused.
"""
from __future__ import annotations

import logging
import os

from . import policy

logger = logging.getLogger(__name__)


def _home():
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:  # noqa: BLE001 - outside Hermes (tests)
        return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _families():
    """The reads of the profile this turn runs in, read from that profile's own plugin directory each call (one
    module may serve several profiles in one gateway, so nothing per profile is cached here)."""
    return policy.read_scope(os.path.join(_home(), "plugins", policy.GUARD_NAME))


def pre_tool_call(tool_name=None, args=None, **_):
    """Refuse what a read-only bot may not run. An exception here is a refusal (Hermes fails a raising
    pre_tool_call closed)."""
    message = policy.decide(tool_name, args)
    return {"action": "block", "message": message} if message else None


def read_tool(args, **kwargs):
    families = _families()
    if not families:
        return policy.neutral("Not run: this profile's hermes-auto-guard has no read scope; reinstall it with "
                              "`hermes-auto install-guard`.")
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
