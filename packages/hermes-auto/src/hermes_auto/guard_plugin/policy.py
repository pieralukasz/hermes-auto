"""Hermes Auto guard: the first, automatic turn of a third-party session in the default profile (Dolores).

Shared by the Hermes plugin (``__init__.py``, loaded by the gateway for the profile that enables it) and by
hermes-auto's bridge and tests. Pure Python, no Hermes imports at module level: the bridge loads this file by path
and the tests import it.

One mode, ``first_turn``, set by ``scope.json`` in the profile's plugin directory. Sessions hermes-auto registered
before their first prompt (``<home>/hermes-auto-guard/sessions/<session id>``) carry someone else's content (mail,
Slack, GitHub, Linear, a "↩" reply). Until the user has written in such a session, its turns (and the background
review fork, which shares the session id) may only read: the plugin's read tool (one command its grammar proves to
be a read, limited to the read families of the session's account), raw skill reads and ``session_search`` in this
profile (when the session's account may search). Every other session of the profile is untouched, and so is a
registered one once the user wrote in it. The read families come from the scope: default reads, and per provider (an
account chosen by hermes-auto's ``routes``, e.g. a work account) its own reads and whether it may search earlier
conversations. Fail-closed: a registered session whose messages cannot be read stays locked; a session that cannot
be told (registry or state.db unreadable, lineage query failing, a compression lineage with a cycle, a dangling
parent or over COMPRESSION_HOPS) gets no tool at all; without a valid scope a locked session reads nothing and
cannot search. ``tool_search`` is refused while locked (Hermes may send its queries to remote connectors).

A chat session (iMessage, a Matrix room bridged from WhatsApp, Messenger, Instagram or LinkedIn) may also read that
one chat's history (``imsg history --chat-id N``, ``hermes-auto-matrix history ROOM``): the chat is recorded in the
session's registry entry by the bridge (``chat_of``), not in scope.json, so the scope format is unchanged and a
guard without chat reads ignores the entry. No other chat is readable, and nothing can be sent.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys

GUARD_NAME = "hermes-auto-guard"
GUARD_VERSION = "3.0.3"
FIRST_TURN_MODE = "first_turn"
SCOPE_VERSION = 2
TOOLSET = "hermes-auto"
READ_TOOL = "hermes_auto_read"
SKILL_VIEW_TOOL = "hermes_auto_skill_view"
SKILLS_LIST_TOOL = "hermes_auto_skills_list"
PLUGIN_TOOLS = (READ_TOOL, SKILL_VIEW_TOOL, SKILLS_LIST_TOOL)
SEARCH = "session_search"
# Hermes renames some tools on the Anthropic OAuth wire and maps them back before dispatch; the hook treats a wire
# name like the tool itself.
ALIASES = {"chat_history_lookup": SEARCH}
# Tools a locked (first, automatic) turn may call; session_search only in this profile and on an account that may.
FIRST_TURN_TOOLS = frozenset(PLUGIN_TOOLS) | {SEARCH}

# How a job's automatic session is launched (its `deny_tools`): "mail", "research" and "private" are the read
# profiles the sorter chose, and every one of them is a FIRST_TURN launch in the default profile (normal toolsets;
# this guard locks the turn until the user writes). "full" is a Todoist task with the profile's normal tools.
MAIL, RESEARCH, PRIVATE, FULL = "mail", "research", "private", "full"
DENY_PROFILES = (MAIL, RESEARCH, PRIVATE)
PROFILES = DENY_PROFILES + (FULL,)
FIRST_TURN = "first_turn"
# Read families: which programs the read tool may run.
GOOGLE, TODOIST, GITHUB, LINEAR, SLACK = "google", "todoist", "github", "linear", "slack"
FAMILIES = (GITHUB, GOOGLE, LINEAR, SLACK, TODOIST)
PROFILE_FAMILIES = {MAIL: frozenset({GOOGLE, TODOIST}), RESEARCH: frozenset(FAMILIES)}
OUTPUT_LIMIT = 30000
COMMAND_TIMEOUT = 120
SCOPE_FILE = "scope.json"


def profile_for(deny_tools, full_tools=False):
    """The launch profile of a job: its `deny_tools` value (True, "research"/"slack", other) or "full"."""
    if deny_tools:
        if deny_tools is True:
            return MAIL
        return RESEARCH if deny_tools in ("research", "slack") else PRIVATE
    return FULL if full_tools else None


def families_for(profile):
    """Read families for `profile`: a launch profile name/True (legacy callers) or an iterable of families."""
    if profile is True or profile == MAIL:
        return PROFILE_FAMILIES[MAIL]
    if profile in ("research", "slack"):
        return PROFILE_FAMILIES[RESEARCH]
    if isinstance(profile, (set, frozenset, list, tuple)):
        return frozenset(item for item in profile if item in FAMILIES)
    return frozenset()


def valid_families(value):
    return isinstance(value, list) and all(item in FAMILIES for item in value)


def valid_scope(data):
    """scope.json: {"version": 2, "mode": "first_turn", "reads": [families], "providers": {provider: {"reads":
    [...], "session_search": bool}}}. `reads` apply to sessions on any other account; a provider entry (e.g. a
    work account chosen by a hermes-auto route) replaces them for sessions running on that provider. The scopes of
    guard 2.x (read-only bots, the delegating `subagent` scope) are not valid here."""
    if not (isinstance(data, dict) and data.get("version") == SCOPE_VERSION and data.get("mode") == FIRST_TURN_MODE
            and valid_families(data.get("reads")) and set(data) <= {"version", "mode", "reads", "providers"}):
        return False
    providers = data.get("providers", {})
    return isinstance(providers, dict) and all(
        isinstance(name, str) and bool(name) and isinstance(spec, dict) and valid_families(spec.get("reads"))
        and isinstance(spec.get("session_search", False), bool) and set(spec) <= {"reads", "session_search"}
        for name, spec in providers.items())


def load_scope(plugin_dir):
    """The scope a profile's plugin was installed with, or None when the file is missing or malformed."""
    try:
        with open(os.path.join(str(plugin_dir), SCOPE_FILE), encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    return data if valid_scope(data) else None


def session_scope(data, provider):
    """(read families, session_search allowed) for a session running on `provider` under scope `data`.

    The profile serves two accounts (private and work): a session on a provider with its own entry gets only that
    entry's reads, others the default reads with session_search. An unknown provider (None: the session's row could
    not be read) gets what every entry allows and no session_search; no scope gets nothing."""
    if not data:
        return frozenset(), False
    providers = data.get("providers") or {}
    if provider is None:
        reads = frozenset(data["reads"])
        for spec in providers.values():
            reads &= frozenset(spec["reads"])
        return reads, False
    spec = providers.get(provider)
    if spec is not None:
        return frozenset(spec["reads"]), bool(spec.get("session_search", False))
    return frozenset(data["reads"]), True


SHELL_META = set(";|&<>`$\n\r\\(){}")
GOOGLE_API = "skills/productivity/google-workspace/scripts/google_api.py"
GOOGLE_READS = {("calendar", "list"), ("gmail", "search"), ("gmail", "get"), ("gmail", "labels")}
TODOIST_READS = {("today",), ("upcoming",), ("inbox",), ("completed",), ("activity",),
                 ("task", "list"), ("task", "view"), ("project", "list"), ("project", "view"),
                 ("section", "list"), ("label", "list"), ("filter", "list"), ("filter", "view")}
# Research read profiles (Slack, GitHub); "slack" is kept for sessions queued before the rename.
RESEARCH_PROFILES = ("research", "slack")
SLACK_MCP = "skills/tap-in/tapin-slack/scripts/slack_mcp.py"
# The TapIn skills moved into the TapIn bot's profile; both locations are trusted.
SLACK_MCP_PATHS = (SLACK_MCP, "profiles/tapin/" + SLACK_MCP)
SLACK_MCP_READS = {"slack_search_public", "slack_search_public_and_private", "slack_search_users",
                   "slack_search_channels", "slack_read_channel", "slack_read_thread", "slack_read_canvas",
                   "slack_read_user_profile", "slack_read_file", "slack_list_channel_members",
                   "slack_list_user_channels"}
# Programs given by path must live here; a bare name resolves through the worker's own PATH.
TRUSTED_BIN = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def grammar(flags=(), values=(), patterns=(), equals_only=False):
    """Options a read subcommand accepts: plain flags, options taking one value, attached-value regexes.

    Anything else that starts with `-` is refused, so an unknown or ambiguous option never runs.
    """
    return {"flags": frozenset(flags), "values": frozenset(values),
            "patterns": tuple(re.compile(pattern) for pattern in patterns), "equals_only": equals_only}


def split_options(args, spec, value_ok=lambda option, value: True):
    """Positional arguments of `args` under `spec`, or None when an option is not in the grammar.

    Value options take `--name=value`, or `--name value` unless the grammar is `equals_only`.
    Attached short values (`-XPOST`, `-Ovim`) only pass through an explicit pattern.
    """
    positionals, index = [], 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == "--":
            positionals.extend(args[index:])
            break
        if not token.startswith("-") or token == "-":
            positionals.append(token)
        elif token in spec["flags"] or any(pattern.fullmatch(token) for pattern in spec["patterns"]):
            continue
        elif token.startswith("--") and token.split("=", 1)[0] in spec["values"] and "=" in token:
            option, value = token.split("=", 1)
            if not value_ok(option, value):
                return None
        elif token in spec["values"] and not spec["equals_only"]:
            if index >= len(args) or args[index].startswith("-") or not value_ok(token, args[index]):
                return None
            index += 1
        else:
            return None
    return positionals


TD_OUTPUT = ("--json", "--ndjson", "--ids-only", "--full", "--raw", "--show-urls")
TD_PAGE = ("--limit", "--cursor")
TD_LIST = grammar(TD_OUTPUT + ("--all",), TD_PAGE)
TD_READS = {
    ("today",): grammar(TD_OUTPUT + ("--all", "--any-assignee", "--personal"), TD_PAGE + ("--workspace",)),
    ("upcoming",): grammar(TD_OUTPUT + ("--all", "--any-assignee", "--personal"), TD_PAGE + ("--workspace",)),
    ("inbox",): TD_LIST,
    ("task", "list"): grammar(TD_OUTPUT + ("--all", "--unassigned", "--personal"),
                              TD_PAGE + ("--project", "--parent", "--label", "--priority", "--due", "--filter",
                                         "--assignee", "--workspace")),
    ("task", "view"): grammar(TD_OUTPUT + ("--include-children",)),
    ("project", "list"): grammar(TD_OUTPUT + ("--all", "--personal"), TD_PAGE + ("--search",)),
    ("project", "view"): grammar(TD_OUTPUT + ("--detailed", "--include-children")),
    ("comment", "list"): grammar(TD_OUTPUT + ("--all", "-P", "--project"), ("--limit", "--lines")),
    ("section", "list"): grammar(TD_OUTPUT + ("--all",), ("--project", "--search", "--limit")),
    ("label", "list"): grammar(TD_OUTPUT + ("--all",), ("--search", "--limit")),
    ("filter", "list"): grammar(TD_OUTPUT),
    ("filter", "view"): grammar(TD_OUTPUT + ("--all",), TD_PAGE + ("--sort", "--sort-order")),
}


def todoist_read(tokens):
    args = tokens[1:]
    for prefix, spec in TD_READS.items():
        if tuple(args[:len(prefix)]) != prefix:
            continue
        positional = split_options(args[len(prefix):], spec)
        if positional is None or any(value.startswith("-") for value in positional):
            return False
        count = 1 if prefix[-1] == "view" or prefix == ("comment", "list") else 0
        return len(positional) == count
    return False


def hermes_homes():
    homes = {os.path.normpath(os.path.expanduser("~/.hermes"))}
    if os.environ.get("HERMES_HOME"):
        homes.add(os.path.normpath(os.environ["HERMES_HOME"]))
    return homes


def literal_path(token, words):
    """Absolute path of an unquoted `token`, or None.

    A quoted `~` stays literal in the shell and would resolve against the working directory, so the
    token has to appear verbatim among the command's words. `..` components are refused.
    """
    if token not in words:
        return None
    path = os.path.expanduser(token)
    if not os.path.isabs(path) or ".." in path.split("/"):
        return None
    return os.path.normpath(path)


def trusted_program(token, words, name):
    """`name` itself, or `name` at an absolute path inside TRUSTED_BIN."""
    if token == name:
        return True
    path = literal_path(token, words)
    return path is not None and path in {os.path.join(directory, name) for directory in TRUSTED_BIN}


def trusted_python(token, words):
    if token in ("python", "python3"):
        return True
    path = literal_path(token, words)
    venvs = {os.path.join(home, "hermes-agent/venv/bin", name) for home in hermes_homes()
             for name in ("python", "python3")}
    return path is not None and path in venvs | {os.path.normpath(sys.executable), "/usr/bin/python3",
                                                 "/opt/homebrew/bin/python3"}


def skill_script(tokens, words, relative):
    """`python <hermes home>/<relative> ...` with a trusted interpreter and the exact skill script (one relative
    path or a tuple of them)."""
    if len(tokens) < 4 or not trusted_python(tokens[0], words):
        return False
    paths = (relative,) if isinstance(relative, str) else relative
    return literal_path(tokens[1], words) in {os.path.join(home, item) for home in hermes_homes() for item in paths}


GH_REPO = re.compile(r"[\w.-]+/[\w.-]+")
GH_ENDPOINT = re.compile(r"/?[A-Za-z0-9_.{}/-]+(\?[A-Za-z0-9_.,%+=&-]*)?")
REPO = ("--repo", "-R")
GH_READS = {
    ("pr", "view"): grammar(("--comments", "-c"), REPO + ("--json",)),
    ("pr", "list"): grammar((), REPO + ("--json", "--limit", "-L", "--state", "-s", "--author", "-A", "--label",
                                        "-l", "--assignee", "-a", "--search", "-S", "--base", "-B", "--head",
                                        "-H")),
    ("pr", "diff"): grammar(("--patch", "--name-only"), REPO + ("--color",)),
    ("pr", "checks"): grammar(("--required",), REPO + ("--json",)),
    ("pr", "status"): grammar((), REPO + ("--json",)),
    ("issue", "view"): grammar(("--comments", "-c"), REPO + ("--json",)),
    ("issue", "list"): grammar((), REPO + ("--json", "--limit", "-L", "--state", "-s", "--author", "-A",
                                           "--label", "-l", "--assignee", "-a", "--search", "-S", "--milestone",
                                           "-m", "--mention")),
    ("search", "prs"): grammar((), ("--repo", "-R", "--owner", "--limit", "-L", "--state", "--author", "--label",
                                    "--json", "--sort", "--order", "--language", "--updated")),
    ("search", "issues"): grammar((), ("--repo", "-R", "--owner", "--limit", "-L", "--state", "--author",
                                       "--label", "--json", "--sort", "--order", "--language", "--updated")),
    ("search", "code"): grammar((), ("--repo", "-R", "--owner", "--limit", "-L", "--language", "--filename",
                                     "--extension", "--json")),
    ("search", "commits"): grammar((), ("--repo", "-R", "--owner", "--limit", "-L", "--author", "--json")),
    ("run", "list"): grammar((), REPO + ("--json", "--limit", "-L", "--branch", "-b", "--workflow", "--status",
                                         "-s", "--event", "-e", "--user", "-u")),
    ("run", "view"): grammar(("--log", "--log-failed", "--verbose", "-v", "--exit-status"),
                             REPO + ("--json", "--job", "-j", "--attempt", "-a")),
    ("repo", "view"): grammar((), ("--json", "--branch", "-b")),
    ("release", "list"): grammar((), REPO + ("--json", "--limit", "-L")),
    ("release", "view"): grammar((), REPO + ("--json",)),
    ("workflow", "list"): grammar(("--all", "-a"), REPO + ("--json", "--limit", "-L")),
}
# `gh api` stays a GET: no fields, input, method, headers, host or template options. gh's jq can read the
# process environment (`env`, `$ENV`: the worker holds API keys), so `--jq` takes only filters on this list.
GH_API = grammar(("--paginate", "--include", "-i", "--slurp"), ("--jq", "-q"))
GH_JQ_FILTERS = frozenset({".content | @base64d", ".content|@base64d", ".[].filename", ".[] | .filename",
                           ".[].body", ".[] | .body"})


# A `gh search --updated` value: one date or date-time, optionally after a comparison, or a range with `..` (an
# open end is `*`), as GitHub's search syntax takes it. gh pastes the value into the query (`updated:<value>`), so
# nothing else (no space, no further qualifier, no option) may ride along.
GH_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?(?:Z|[+-][0-9]{2}:[0-9]{2})?)?"
GH_DATE_FILTER = re.compile(rf"(?:[<>]=?)?{GH_DATE}|{GH_DATE}\.\.(?:{GH_DATE}|\*)|\*\.\.{GH_DATE}")


def gh_value(option, value):
    if option in ("--repo", "-R"):
        return bool(GH_REPO.fullmatch(value))
    if option == "--updated":
        return bool(GH_DATE_FILTER.fullmatch(value))
    if option in ("--jq", "-q"):
        return " ".join(value.split()) in GH_JQ_FILTERS
    return "://" not in value


def github_argument(token):
    return "://" not in token or token.startswith("https://github.com/")


def gh_read(tokens):
    """`gh <group> <read command>` or `gh api <GET endpoint>` with only the options in its grammar."""
    args = tokens[1:]
    if args[:1] == ["api"]:
        positionals = split_options(args[1:], GH_API, gh_value)
        return (positionals is not None and len(positionals) == 1 and bool(GH_ENDPOINT.fullmatch(positionals[0]))
                and not positionals[0].lstrip("/").startswith("graphql"))
    spec = GH_READS.get(tuple(args[:2]))
    if spec is None:
        return False
    positionals = split_options(args[2:], spec, gh_value)
    if tuple(args[:2]) == ("repo", "view"):
        return (positionals is not None and len(positionals) == 1
                and bool(GH_REPO.fullmatch(positionals[0].removeprefix("https://github.com/"))))
    return positionals is not None and all(github_argument(token) for token in positionals)


LINEAR_LIST = grammar(("--json", "-j", "--no-pager"), ("--team", "--limit"))
LINEAR_READS = {
    ("issue", "view"): grammar(("--json", "-j", "--no-pager", "--no-comments", "--no-download")),
    ("issue", "list"): grammar(("--json", "-j", "--no-pager", "--all-states", "--all-assignees", "-A",
                                "--unassigned", "-U"),
                               ("--state", "-s", "--assignee", "--sort", "--team", "--project", "--limit")),
    # Search and filters only; no --workspace (another account's credential).
    ("issue", "query"): grammar(("--json", "-j", "--no-pager", "--search-comments", "--all-teams", "--unassigned", "-U",
                                 "--include-archived"),
                                ("--search", "--team", "--state", "-s", "--sort", "--assignee", "--project",
                                 "--project-label", "--cycle", "--milestone", "--label", "-l", "--created-after",
                                 "--updated-after", "--limit")),
    ("project", "view"): LINEAR_LIST, ("project", "list"): LINEAR_LIST,
    ("team", "list"): LINEAR_LIST, ("cycle", "list"): LINEAR_LIST, ("cycle", "view"): LINEAR_LIST,
}


def linear_read(tokens):
    args = tokens[1:]
    if args[:1] == ["api"]:
        # One GraphQL query and nothing else: no variables, no options.
        query = args[1].strip() if len(args) == 2 else ""
        return query.startswith(("query", "{")) and "mutation" not in query.lower()
    spec = LINEAR_READS.get(tuple(args[:2]))
    # No `--`: after it the grammar would take options (the required --no-download, --workspace) for arguments.
    if spec is None or "--" in args:
        return False
    positionals = split_options(args[2:], spec)
    if positionals is None or any(value.startswith("-") for value in positionals):
        return False
    if tuple(args[:2]) == ("issue", "view"):
        return "--no-download" in args[2:] and len(positionals) <= 1
    return True


# --- Chat history: only the session's own chat -------------------------------------------------------

IMESSAGE, MATRIX = "imessage", "matrix"
CHAT_SOURCES = (IMESSAGE, MATRIX)
CHAT_ID = re.compile(r"[0-9]{1,12}")
# A Matrix room id: room versions up to 11 carry ":server", version 12 is opaque.
ROOM_ID = re.compile(r"![A-Za-z0-9._~=+/-]{1,255}(?::[A-Za-z0-9.-]{1,255}(?::[0-9]{1,5})?)?")
HISTORY_LIMIT = 200
MATRIX_HISTORY_LIMIT = 100
ISO_TIME = r"[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9:.]+(Z|[+-][0-9:]+)?)?"
# No --db (another database), --participants (a handle filter spanning chats), --convert-attachments (writes
# converted copies), log options; the chat id is checked against the session's chat.
IMSG_HISTORY = grammar(("--json", "-j", "--json-output", "--jsonOutput", "--attachments"),
                       ("--chat-id", "--limit", "--start", "--end"))
MATRIX_HISTORY = grammar((), ("--limit",))
MATRIX_HELPER = "hermes-auto-matrix"


def valid_chat(chat):
    return (isinstance(chat, dict) and set(chat) == {"source", "id"} and isinstance(chat.get("id"), str)
            and ((chat["source"] == IMESSAGE and bool(CHAT_ID.fullmatch(chat["id"])))
                 or (chat["source"] == MATRIX and bool(ROOM_ID.fullmatch(chat["id"])))))


def chat_of(job):
    """{"source", "id"} of the one chat a job's session may read (an iMessage chat rowid, a Matrix room id), or
    None for every other job and for a malformed id."""
    source = (job or {}).get("source")
    if source not in CHAT_SOURCES:
        return None
    try:
        payload = json.loads((job or {}).get("payload") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("chat_id") if source == IMESSAGE else payload.get("room_id")
    if source == IMESSAGE and (isinstance(value, bool) or not isinstance(value, int)):
        return None
    chat = {"source": source, "id": str(value)}
    return chat if valid_chat(chat) else None


def bounded_int(value, top):
    return bool(re.fullmatch(r"[0-9]{1,4}", value)) and 1 <= int(value) <= top


def chat_read(tokens, words, chat):
    """`imsg history` or `hermes-auto-matrix history` of exactly the session's chat, with read options only."""
    if not valid_chat(chat):
        return False
    if chat["source"] == IMESSAGE and trusted_program(tokens[0], words, "imsg"):
        if tokens[1:2] != ["history"]:
            return False
        seen = []

        def value_ok(option, value):
            seen.append((option, value))
            if option == "--chat-id":
                return value == chat["id"]
            if option == "--limit":
                return bounded_int(value, HISTORY_LIMIT)
            return bool(re.fullmatch(ISO_TIME, value))
        positionals = split_options(tokens[2:], IMSG_HISTORY, value_ok)
        chats = [value for option, value in seen if option == "--chat-id"]
        return positionals == [] and chats == [chat["id"]] and len(seen) == len({option for option, _ in seen})
    if chat["source"] == MATRIX and trusted_program(tokens[0], words, MATRIX_HELPER):
        if tokens[1:2] != ["history"]:
            return False
        limits = []
        positionals = split_options(tokens[2:], MATRIX_HISTORY,
                                    lambda option, value: limits.append(value) is None
                                    and bounded_int(value, MATRIX_HISTORY_LIMIT))
        return positionals == [chat["id"]] and len(limits) <= 1
    return False


def slack_read_command(tokens, words=None, families=None):
    """Reads of the research families: trusted Slack, GitHub and Linear commands."""
    words = set(tokens) if words is None else words
    families = PROFILE_FAMILIES[RESEARCH] if families is None else families
    if SLACK in families and skill_script(tokens, words, SLACK_MCP_PATHS):
        return tokens[2] == "call" and tokens[3] in SLACK_MCP_READS and len(tokens) <= 5
    if GITHUB in families and trusted_program(tokens[0], words, "gh"):
        return gh_read(tokens)
    if LINEAR in families and trusted_program(tokens[0], words, "linear"):
        return linear_read(tokens)
    return False


def read_only_command(command, profile=True, chat=None):
    """True only for a single plain read command of the allowed families: calendar/Gmail reads via the Google
    script, Todoist reads, and the research reads (`slack_read_command`). `profile` is a set of families or a
    launch profile ("mail"/True: Google and Todoist; "research": all). "private" and unknown profiles allow
    no command. `chat` (the session's own chat, `session_chat`) also allows that chat's history (`chat_read`).

    No shell syntax at all (pipes, redirects, substitutions, chaining), so the allowed program is the
    only thing that runs. Programs and scripts must be the trusted ones, and their options must be in an
    explicit read grammar. Anything else is refused, however harmless it looks.
    """
    families = families_for(profile)
    if not families and not valid_chat(chat):
        return False
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
    words = set(text.split())
    if trusted_program(tokens[0], words, "imsg") or trusted_program(tokens[0], words, MATRIX_HELPER):
        return chat_read(tokens, words, chat)
    if trusted_program(tokens[0], words, "td"):
        return TODOIST in families and todoist_read(tokens)
    if skill_script(tokens, words, GOOGLE_API):
        return GOOGLE in families and (tokens[2], tokens[3]) in GOOGLE_READS
    return slack_read_command(tokens, words, families)


# --- Hook decision -------------------------------------------------------------------------------------

REFUSED_READ = ("Not run: the first, automatic turn of this session only reads (third-party content). Use "
                "hermes_auto_read for one plain read command, hermes_auto_skill_view/hermes_auto_skills_list for "
                "skills and session_search for earlier conversations; every tool works after the user's first "
                "message.")
REFUSED_PROFILE = ("Not run: an automatic turn may search only this profile's conversations (no `profile`, no "
                   "`profile/id` session links).")


def own_profile_search(args):
    """session_search arguments that stay in the current profile: no `profile` (Hermes opens another
    profile's state.db for it) and no `<profile>/<id>` session link (it adopts the embedded profile)."""
    if not isinstance(args, dict):
        return False
    if str(args.get("profile") or "").strip():
        return False
    session_id = args.get("session_id")
    return session_id is None or (isinstance(session_id, str) and "/" not in session_id)


# Hermes' Tool Search bridge. Plugin tools are deferred behind it: a session calls them by their exact name through
# `tool_call` (or directly when the request lists them). `tool_search` is no local catalog read: with
# manage_connections among the session's tools Hermes sends every query to the connector service
# (tools/tool_search.py, `connections_in_scope`), so a locked turn may not use it. `tool_describe` reaches the
# connector service only for `connectors__` names (tools/connectors/search.py, `remote_schemas_for`): a locked
# turn may load only its own tools.
SEARCH_TOOL, DESCRIBE_TOOL = "tool_search", "tool_describe"
REFUSED_TOOL_SEARCH = (
    "Not run: tool_search is off here (it can send its queries to remote connectors). Call a tool by its exact "
    "name, directly or through tool_call. (Nie uruchomiono: wyszukiwarka narzędzi jest tu wyłączona; wywołaj "
    "narzędzie po dokładnej nazwie, bezpośrednio albo przez tool_call.)")


def described_names(args):
    """The tool names a `tool_describe` call asks for (as Hermes reads them: a list or one string, each stripped), or
    None when the shape cannot be read."""
    names = args.get("names") if isinstance(args, dict) else None
    names = [names] if isinstance(names, str) else names
    if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
        return None
    return frozenset(ALIASES.get(name.strip(), name.strip()) for name in names)


def describe_allowed(args, allowed):
    names = described_names(args)
    return bool(names) and names <= allowed


REFUSED_SEARCH = ("Not run: earlier conversations are not searchable from an automatic turn on this account (the "
                  "profile also holds the other account's matters). (Nie uruchomiono: z automatycznej tury na tym "
                  "koncie nie przeszukuje się wcześniejszych rozmów.)")


# --- first_turn: Dolores' automatic sessions ------------------------------------------------------------

# Appended by the bridge to the automatic prompt (model text and transcript card): a user row carrying it is the
# automation's own message, never the user's. Text inside a third party's message can only keep the lock.
AUTO_TURN_MARK = "<!-- hermes-auto:auto-turn -->"
REGISTRY = ("hermes-auto-guard", "sessions")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
# User rows Hermes writes itself with no display kind: notices and wake-ups (old rows), compaction handoffs, the
# todo and planning snapshots, cron output (agent.context_compressor._SYNTHETIC_USER_ROW_PREFIXES).
SYSTEM_PREFIXES = ("[IMPORTANT:", "[System", "[ASYNC", "[INTERNAL", "[CONTEXT", "[PRIOR CONTEXT", "[STILL IN PROGRESS",
                   "[Your active task list", "[Planning state preserved", "Cronjob Response:")
# Hermes' own nudges persisted as plain user rows (SessionDB drops their metadata flags; Hermes recognizes them by
# this exact text, agent.context_compressor._is_synthetic_compression_user_turn). The iteration-limit request lands
# when the automatic turn spends agent_max_turns, which a third party's instructions can force; the background
# review fork runs right after it.
HERMES_NUDGES = frozenset({
    "You've reached the maximum number of tool-calling iterations allowed. Please provide a final response "
    "summarizing what you've found and accomplished so far, without calling any more tools.",
    "Continue from the compressed conversation context above. This marker exists because no human user turn was "
    "available.",
    "Continue from the compressed conversation context above. This marker exists because the compacted transcript "
    "contained no preserved user turn.",
    "You just executed tool calls but returned an empty response. Please process the tool results above and "
    "continue with the task.",
    "Your previous turn indicated a tool call but none was included. Do not narrate a plan or restate intent — "
    "issue the actual tool call now to continue the task.",
})
# A steer delivered while a turn runs rides inside a TOOL result between these markers; a compaction restates the
# newest one as a plain user row (agent.conversation_compression._ensure_compressed_has_user_turn). Tool output
# also carries third-party text (a mail read through hermes_auto_read), so a user row restating it is not the user.
STEER_OPEN = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool "
              "output and not a new delivery when replayed from conversation history]")
STEER_OPEN_PREFIX, STEER_CLOSE = "[OUT-OF-BAND USER MESSAGE", "[/OUT-OF-BAND USER MESSAGE]"
# Display kinds of rows a person typed: a normal message (NULL), a steer, a hidden composer send.
HUMAN_KINDS = ("steer", "hidden")
REFUSED_FIRST_TURN = (
    "Nie uruchomiono: to pierwsza, automatyczna tura sesji z treścią od osoby trzeciej. Teraz działają tylko "
    "odczyty: hermes_auto_read (jedno polecenie odczytu), hermes_auto_skill_view/hermes_auto_skills_list i "
    "session_search w tym profilu. Pełne narzędzia ruszą po pierwszej wiadomości Łukasza w tej sesji. (Not run: "
    "automatic first turn, reads only; every tool after the user's first message.)")
REFUSED_UNKNOWN = (
    "Nie uruchomiono: nie da się ustalić, czy ta sesja jest automatyczną sesją z treścią od osoby trzeciej "
    "({why}), więc hermes-auto-guard wstrzymuje wszystkie narzędzia, dopóki odczyt się nie uda. Spróbuj ponownie "
    "za chwilę; jeśli to się powtarza, sprawdź katalog hermes-auto-guard/sessions i state.db tego profilu. "
    "(Not run: cannot tell whether this is an automatic third-party session; every tool is refused until it can.)")
# Safety bound for the lineage walk, not a policy limit: a real lineage ends long before it, and an ordinary
# long-lived chat must never be refused merely for having been compressed many times. Only a cycle (or a
# lineage beyond this bound) is incomplete.
COMPRESSION_HOPS = 100_000
# first_turn_state: an ordinary session (nothing refused), an automatic one without the user (first-turn rules),
# or one that cannot be told (every tool refused, with the reason).
OPEN, LOCKED, UNKNOWN = "open", "locked", "unknown"


def registry_dir(home):
    return os.path.join(str(home), *REGISTRY)


def valid_session_id(session_id):
    return isinstance(session_id, str) and bool(SESSION_ID.fullmatch(session_id))


def register_session(home, session_id, job_id=None, chat=None):
    """Mark a session of this profile as an automatic one with third-party content (before its first prompt).
    `chat` (`chat_of`): the one chat whose history its first turn may read."""
    from pathlib import Path
    if not valid_session_id(session_id):
        raise ValueError(f"invalid session id {session_id!r}")
    if chat is not None and not valid_chat(chat):
        raise ValueError(f"invalid chat {chat!r}")
    directory = Path(registry_dir(home))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / session_id
    temporary = directory / f".{session_id}.{os.getpid()}"
    entry = {"job": job_id, "version": 1, **({"chat": chat} if chat else {})}
    temporary.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def is_registered(home, session_id):
    return valid_session_id(session_id) and os.path.isfile(os.path.join(registry_dir(home), session_id))


def _ancestors(conn, session_id):
    """(compression ancestors oldest last, complete). Complete when the walk reached a session that is no
    compression continuation; a cycle, a lineage beyond COMPRESSION_HOPS or a parent pointer to a missing row
    (it cannot be told whether that parent was a registered session's compression) is incomplete."""
    found, current = [], session_id
    for _ in range(COMPRESSION_HOPS + 1):
        row = conn.execute("SELECT c.parent_session_id, p.id, p.end_reason FROM sessions c "
                           "LEFT JOIN sessions p ON p.id = c.parent_session_id WHERE c.id = ?", (current,)).fetchone()
        if row is None or not row[0]:
            return found, True  # no row (a session not persisted yet) or no parent: the lineage ends here
        if row[1] is None:
            return found, False  # dangling parent pointer (Codex review round 2, finding 6)
        if row[2] != "compression":
            return found, True
        if row[1] == session_id or row[1] in found:
            return found, False  # a cycle
        if len(found) == COMPRESSION_HOPS:
            return found, False  # more ancestors than the walk reads
        found.append(row[1])
        current = row[1]
    return found, False


_HERMES = {}


def hermes_function(module, name):
    """A Hermes function this guard double-checks with when it runs inside Hermes, else None (looked up once)."""
    key = (module, name)
    if key not in _HERMES:
        try:
            _HERMES[key] = getattr(__import__(module, fromlist=[name]), name)
        except Exception:  # noqa: BLE001 - outside Hermes (tests, the bridge), or a Hermes without it
            _HERMES[key] = None
    return _HERMES[key]


def hermes_synthetic(text):
    """Hermes' own recognizer of its synthetic user rows (False outside Hermes or when it fails: the static lists
    above still apply)."""
    compressor = hermes_function("agent.context_compressor", "ContextCompressor")
    try:
        return bool(compressor and compressor._is_synthetic_compression_user_turn({"role": "user", "content": text}))
    except Exception:  # noqa: BLE001
        return False


def steer_text(text):
    """The text a compaction would restate from one tool result's steer marker (Hermes'
    _extract_steer_text_from_message, replicated: the first marker pair), or None."""
    text = str(text or "")
    start = text.find(STEER_OPEN)
    if start == -1:
        start = text.find(STEER_OPEN_PREFIX)
        if start == -1:
            return None
        newline = text.find("\n", start)
        start = newline + 1 if newline != -1 else start + len(STEER_OPEN_PREFIX)
    else:
        start += len(STEER_OPEN)
    end = text.find(STEER_CLOSE, start)
    return (text[start:end].strip() or None) if end != -1 else None


def restated_steers(tool_contents):
    """Texts a compaction may restate as user rows from these tool results (Hermes' extraction and ours)."""
    extract = hermes_function("agent.conversation_compression", "_extract_steer_text_from_message")
    found = set()
    for content in tool_contents:
        found.add(steer_text(content))
        try:
            found.add(extract({"role": "tool", "content": content}) if extract else None)
        except Exception:  # noqa: BLE001
            pass
    found.discard(None)
    return frozenset(" ".join(str(text).split()) for text in found)


def human_row(content, api_content, kind, metadata, summary, restated=frozenset()):
    """Whether one user row was written by a person (see the module docstring for what counts). `restated`: steer
    texts found in the lineage's tool results (`restated_steers`); a plain row repeating one is a compaction's
    restatement of tool output, not the user."""
    if summary or (kind is not None and kind not in HUMAN_KINDS):
        return False
    if kind == "hidden" and metadata and "notification_category" in str(metadata):
        return False  # a muted diagnostic wake
    texts = [str(content or ""), str(api_content or "")]
    if any(AUTO_TURN_MARK in text for text in texts):
        return False
    if not texts[0].strip():
        return False  # no words, so no person (fail-closed)
    for text in (item.strip() for item in texts):
        if text and (text.startswith(SYSTEM_PREFIXES) or text in HERMES_NUDGES or hermes_synthetic(text)):
            return False
    return kind is not None or " ".join(texts[0].split()) not in restated


def first_turn_state(home, session_id):
    """(state, reason) of `session_id` in Dolores' profile.

    OPEN: certainly not an automatic session without the user (nothing registered, neither the session nor any
    compression ancestor registered, or a person has written in its lineage). LOCKED: registered (itself or an
    ancestor) and no person has written yet; also a registered session whose messages cannot be read. UNKNOWN: it
    cannot be told (the registry cannot be listed, state.db or the lineage cannot be read, or the lineage is
    incomplete: over COMPRESSION_HOPS ancestors or a cycle), so every tool is refused (fail-closed)."""
    if not valid_session_id(session_id):
        return OPEN, ""
    try:
        names = {name for name in os.listdir(registry_dir(home)) if not name.startswith(".")}
    except FileNotFoundError:
        return OPEN, ""  # nothing was ever registered in this profile
    except OSError as exc:
        return UNKNOWN, f"rejestr sesji nie daje się odczytać: {exc.__class__.__name__}"
    if not names:
        return OPEN, ""
    registered = session_id in names
    try:
        conn = sqlite3.connect(f"file:{os.path.join(str(home), 'state.db')}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return (LOCKED, "") if registered else (UNKNOWN, f"state.db nie daje się otworzyć: {exc.__class__.__name__}")
    try:
        try:
            ancestors, complete = _ancestors(conn, session_id)
        except sqlite3.Error as exc:
            if registered:
                return LOCKED, ""
            return UNKNOWN, f"historii kompresji w state.db nie da się odczytać: {exc.__class__.__name__}"
        lineage = [session_id, *ancestors]
        if not any(sid in names for sid in lineage):
            if complete:
                return OPEN, ""
            return UNKNOWN, (f"historia kompresji ma ponad {COMPRESSION_HOPS} poprzednich sesji albo cykl, a w "
                             "odczytanej części nie ma sesji automatu")
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            if not {"session_id", "role", "content", "display_kind"} <= columns:
                return LOCKED, ""
            optional = [name if name in columns else "NULL" for name in
                        ("api_content", "display_metadata", "_compressed_summary")]
            marks = ",".join("?" for _ in lineage)
            rows = conn.execute(
                f"SELECT content, {optional[0]}, display_kind, {optional[1]}, {optional[2]} FROM messages "
                f"WHERE session_id IN ({marks}) AND role = 'user'", lineage).fetchall()
            restated = restated_steers(row[0] for row in conn.execute(
                f"SELECT content FROM messages WHERE session_id IN ({marks}) AND role = 'tool' AND "
                f"instr(content, ?) > 0", [*lineage, STEER_OPEN_PREFIX]))
        except sqlite3.Error:
            return LOCKED, ""
        return (OPEN, "") if any(human_row(*row, restated=restated) for row in rows) else (LOCKED, "")
    finally:
        conn.close()


def session_chat(home, session_id):
    """The chat recorded for `session_id`'s registered session (itself or the compression ancestor that was
    registered), or None: not registered, an entry without a chat (older guards), or anything unreadable (no chat
    read then; the first-turn lock itself is `first_turn_state`)."""
    if not valid_session_id(session_id):
        return None
    lineage = [session_id]
    if not os.path.isfile(os.path.join(registry_dir(home), session_id)):
        try:
            conn = sqlite3.connect(f"file:{os.path.join(str(home), 'state.db')}?mode=ro", uri=True, timeout=5)
            try:
                lineage += _ancestors(conn, session_id)[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return None
    for sid in lineage:
        path = os.path.join(registry_dir(home), sid)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as stream:
                    chat = json.load(stream).get("chat")
            except (OSError, ValueError, AttributeError):
                return None
            return chat if valid_chat(chat) else None
    return None


def first_turn_locked(home, session_id):
    """True unless `session_id` is certainly open (first_turn_state): locked and undeterminable sessions alike."""
    return first_turn_state(home, session_id)[0] != OPEN


def bridged_calls(args):
    """[(name, arguments)] of a `tool_call` bridge call, or None when its shape cannot be read."""
    if not isinstance(args, dict):
        return None
    calls = args.get("calls")
    if calls is None:
        calls = [args]
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except ValueError:
            return None
    if isinstance(calls, dict):
        calls = [calls]
    if not isinstance(calls, list) or not calls:
        return None
    found = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            return None
        arguments = call.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                return None
        found.append((call["name"], arguments))
    return found


def decide_first_turn(tool_name, args, search=False):
    """The block message for one tool call in a locked automatic session, or None to let it run: the plugin's read
    tools, session_search in this profile when the session's account may search (`search`), and the Tool Search
    bridge to them (tool_call; tool_describe of these tools; never tool_search). The read tool checks its command
    and the account's read families itself."""
    tool_name = ALIASES.get(tool_name, tool_name)
    if tool_name == SEARCH:
        if not search:
            return REFUSED_SEARCH
        return None if own_profile_search(args) else REFUSED_PROFILE
    if tool_name in PLUGIN_TOOLS:
        return None
    if tool_name == SEARCH_TOOL:
        return REFUSED_TOOL_SEARCH
    if tool_name == DESCRIBE_TOOL:
        return None if describe_allowed(args, FIRST_TURN_TOOLS) else REFUSED_FIRST_TURN
    if tool_name == "tool_call":
        calls = bridged_calls(args)
        if calls and all(name.strip() != "tool_call" and decide_first_turn(name.strip(), arguments, search) is None
                         for name, arguments in calls):
            return None  # Hermes unwraps the bridge and checks the real tool again before it runs
    return REFUSED_FIRST_TURN


# --- Skill reads (raw text: no template rendering, inline shell or dependency install) ---------------

SKILL_TEXT_LIMIT = 60000
SKILL_SUBDIRS = ("references", "templates", "scripts", "assets")


def read_skill_text(skill_md, file_path=None):
    """The raw text of a skill's SKILL.md, or of one file inside its directory, as a tool result.

    Hermes' own skill_view renders SKILL.md (template variables, `!`command`` inline shell when enabled)
    and installs declared `deps`; a third-party-steered session gets only the bytes on disk."""
    skill_md = os.path.realpath(str(skill_md))
    skill_dir = os.path.dirname(skill_md)
    target = skill_md
    if file_path:
        relative = str(file_path)
        if os.path.isabs(relative) or ".." in relative.replace("\\", "/").split("/"):
            return neutral("Not run: give a relative path inside the skill, e.g. references/x.md.")
        target = os.path.realpath(os.path.join(skill_dir, relative))
        if not target.startswith(skill_dir + os.sep) or not os.path.isfile(target):
            return neutral(f"Not found in this skill: {relative}.")
    try:
        with open(target, encoding="utf-8", errors="replace") as stream:
            text = stream.read(SKILL_TEXT_LIMIT + 1)
    except OSError as exc:
        return neutral(f"Could not read the skill file ({exc.__class__.__name__}).")
    text = redact_urls(text)  # a skill may note a signed link or a keyed endpoint; the turn needs no credential
    if len(text) > SKILL_TEXT_LIMIT:
        text = text[:SKILL_TEXT_LIMIT] + f"\n[... truncated at {SKILL_TEXT_LIMIT} characters]"
    result = {"success": True, "path": target, "content": text}
    if not file_path:
        linked = []
        for sub in SKILL_SUBDIRS:
            base = os.path.join(skill_dir, sub)
            for root, _dirs, files in os.walk(base):
                linked += [os.path.relpath(os.path.join(root, name), skill_dir) for name in sorted(files)]
        result["linked_files"] = sorted(linked)[:200]
    return json.dumps(result, ensure_ascii=False)


# --- The read tool -------------------------------------------------------------------------------------

def neutral(note):
    """A refused or failed read: no `error` key, so Desktop shows a grey notice instead of a red failure;
    the note tells the model what to do instead."""
    return json.dumps({"skipped": True, "note": note, "output": "", "exit_code": 1}, ensure_ascii=False)


def read_argv(command, profile, chat=None):
    """The argv to run for one read `command` under `profile` (families or a launch profile) and the session's
    `chat`, or None when it is not a read.

    No shell runs it: the grammar's own checks still refuse shell syntax, and the program and script
    tokens are replaced by the exact trusted paths the grammar accepted (a `~` is expanded here, as the
    shell would have)."""
    if not read_only_command(command, profile, chat):
        return None
    tokens = shlex.split(str(command).strip())
    words = set(str(command).split())
    for index in (0, 1)[:len(tokens)]:
        path = literal_path(tokens[index], words)
        if path is not None:
            tokens[index] = path
    return tokens


def read_env():
    """The read's environment: the gateway's, with HERMES_HOME on the root home, where the Google token lives
    (a bot profile's home has none of the user's credentials files)."""
    return {**os.environ, "HERMES_HOME": os.path.expanduser("~/.hermes")}


# --- Credentials in URLs -------------------------------------------------------------------------------
# Read output reaches the model (and every copy of the transcript). Signed links carry their credential in the
# query: raw.githubusercontent.com `download_url`s of private repos (`?token=`), S3/GCS presigned URLs, Linear
# upload links, Azure SAS (`sig`), API keys. The value is replaced; scheme, host, path and the other parameters
# stay, so the link is still recognisable (and useless).
REDACTED = "REDACTED"
SECRET_PARAMS = ("token", "access_token", "refresh_token", "id_token", "client_secret", "api_key", "apikey", "key",
                 "sig", "signature", "password", "x-amz-signature", "x-amz-credential", "x-amz-security-token",
                 "x-goog-signature", "x-goog-credential")
# A URL as it appears in plain text, in JSON (`\/` and `\u0026` escapes), in HTML (`&amp;`) or percent-encoded
# inside another URL's parameter (a redirect or tracking link: `%3Ftoken%3D...%26`).
_URL_CHAR = r"(?:[^\s\"'<>`\\]|\\/|\\u0026|\\u003[dD])"
_URL = re.compile(rf"(?i)\b[a-z][a-z0-9+.-]*:(?:\\?/){{2}}{_URL_CHAR}+")
_URL_PARAM = re.compile(r"(?i)((?:\\u0026|&amp;|%%3F|%%26|%%23|[?&;#])(?:%s)(?:=|%%3D|\\u003d))"
                        r"((?:(?!%%26|%%23)[^&#;\s\"'<>`\\])+)" % "|".join(re.escape(name) for name in SECRET_PARAMS))
# A password in the URL's user info (`https://user:secret@host`, a git remote with a token).
_URL_USERINFO = re.compile(r"(?i)^([a-z][a-z0-9+.-]*:(?:\\?/){2}[^/@:\s\\]*:)([^/@\s\\]+)(@)")


def _redact_url(match):
    url = _URL_PARAM.sub(lambda m: m.group(1) + REDACTED, match.group(0))
    return _URL_USERINFO.sub(lambda m: m.group(1) + REDACTED + m.group(3), url)


def redact_urls(text):
    """`text` with the values of credential parameters (SECRET_PARAMS, any case) and user-info passwords in its
    URLs replaced by REDACTED. Text outside URLs is left alone."""
    return _URL.sub(_redact_url, text) if text else (text or "")


UNQUOTED_COMPARISON = ("Quote a value with < or > in single quotes (no shell runs it, but the command may not "
                       "look like a redirect), e.g. gh search prs --owner ORG --updated '>=2026-10-01'.")


def run_read(command, profile, *, timeout=COMMAND_TIMEOUT, cwd=None, chat=None):
    """Run one read command (no shell) and return the tool result JSON, with credentials in URLs redacted."""
    argv = read_argv(command, profile, chat)
    if argv is None:
        hint = ""
        if set("<>") & set(re.sub(r"'[^']*'", "''", str(command or ""))):
            hint = " " + UNQUOTED_COMPARISON
        return neutral(REFUSED_READ + " This command is not one of the allowed reads (one plain command, "
                       "no pipes or redirects; see the session's first message for the commands)." + hint)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              cwd=cwd or os.path.expanduser("~"), errors="replace",
                              env=read_env())
    except FileNotFoundError:
        return neutral(f"Not run: {argv[0]} is not installed here.")
    except subprocess.TimeoutExpired:
        return neutral(f"Stopped after {timeout} s; narrow the query.")
    output = done.stdout or ""
    if done.stderr.strip():
        output += ("\n" if output else "") + "[stderr]\n" + done.stderr
    output = redact_urls(output)  # before the cut, so a cut cannot split a URL from its credential
    if len(output) > OUTPUT_LIMIT:
        output = output[:OUTPUT_LIMIT] + f"\n[... truncated at {OUTPUT_LIMIT} characters; narrow the query]"
    return json.dumps({"output": output, "exit_code": done.returncode}, ensure_ascii=False)
