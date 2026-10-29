"""Hermes Auto guard: the read rules of the read-only inbox bots (Hermes profiles such as `inbox`).

Shared by the Hermes plugin (``__init__.py``, loaded by the gateway for the profiles that enable it) and by
hermes-auto's bridge and tests. Pure Python, no Hermes imports at module level: the bridge loads this file by path
and the tests import it.

Two modes, chosen by ``scope.json`` in the profile's plugin directory:

* ``read_only`` (default; the `inbox` profile): the boundary lives in the bot profile's own configuration: its
  `platform_toolsets` name only the `hermes-auto` toolset and `session_search`, `agent.disabled_toolsets` removes
  terminal, files, browser, web, delegation and the rest, and the gateway refuses a session that asks for more.
  The plugin applies to every session there (no per-session registry): the one read tool runs only commands its
  grammar proves to be reads, limited to the read families the profile was installed with, and the
  ``pre_tool_call`` hook refuses any other tool. This also holds for a subagent Dolores runs as this profile
  (``delegate_task`` with ``profile``): its turns run in this profile's scope, so this profile's hook guards them.
* ``first_turn`` (Dolores' `default` profile): sessions hermes-auto registered before their first prompt
  (``<home>/hermes-auto-guard/sessions/<session id>``) carry someone else's content. Until the user has written in
  such a session, its turns (and the background review fork, which shares the session id) may only hand the
  matter to the read-only subagent profile (``delegate_task``, every task with that ``profile``), search this
  profile's conversations and read skills. Every other session of the profile is untouched. Fail-closed: a
  registered session whose messages cannot be read stays locked, and a session that cannot be told (registry or
  state.db unreadable, lineage query failing, a compression lineage over COMPRESSION_HOPS or with a cycle) gets no
  tool at all. ``tool_search`` is refused in both modes (Hermes may send its queries to remote connectors).
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
GUARD_VERSION = "2.2.1"
READ_ONLY_MODE, FIRST_TURN_MODE = "read_only", "first_turn"
MODES = (READ_ONLY_MODE, FIRST_TURN_MODE)
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")  # bots.NAME
TOOLSET = "hermes-auto"
READ_TOOL = "hermes_auto_read"
SKILL_VIEW_TOOL = "hermes_auto_skill_view"
SKILLS_LIST_TOOL = "hermes_auto_skills_list"
PLUGIN_TOOLS = (READ_TOOL, SKILL_VIEW_TOOL, SKILLS_LIST_TOOL)
# Tools a session of a read-only bot may call (the hook refuses everything else).
ALLOWED_TOOLS = frozenset(PLUGIN_TOOLS) | {"session_search"}

# How a job's automatic session is launched. "mail" and "research" read through READ_TOOL in a read-only bot;
# "private" (private Slack channels) has no tools at all without bots (in Dolores it is DELEGATE like the rest);
# "full" is a Todoist task (or a mail the user gave the agent label) with the target bot's normal tools.
MAIL, RESEARCH, PRIVATE, FULL = "mail", "research", "private", "full"
DENY_PROFILES = (MAIL, RESEARCH, PRIVATE)
PROFILES = DENY_PROFILES + (FULL,)
# A third-party job in Dolores' profile (bots): her normal tools under the first_turn guard; the read-only
# subagent profile reads.
DELEGATE = "delegate"
# Read families: which programs a profile's read tool may run.
GOOGLE, TODOIST, GITHUB, LINEAR, SLACK = "google", "todoist", "github", "linear", "slack"
FAMILIES = (GOOGLE, TODOIST, GITHUB, LINEAR, SLACK)
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


def valid_scope(data):
    """scope.json: {"version": 1, "reads": [families], "providers": {provider: {"reads": [...], "session_search":
    bool}}}. `reads` apply to sessions on any other account; a provider entry (e.g. a work account chosen by a
    hermes-auto route) replaces them for sessions running on that provider.

    Dolores' profile: {"version": 1, "mode": "first_turn", "reads": [], "subagent": "<read-only profile>"}: no
    reads of its own and no providers; `subagent` is the only profile an automatic first turn may delegate to."""
    if not (isinstance(data, dict) and data.get("version") == 1 and isinstance(data.get("reads"), list)
            and all(item in FAMILIES for item in data["reads"]) and data.get("mode", READ_ONLY_MODE) in MODES):
        return False
    if data.get("mode") == FIRST_TURN_MODE:
        subagent = data.get("subagent")
        return (data["reads"] == [] and "providers" not in data and isinstance(subagent, str)
                and bool(PROFILE_NAME.fullmatch(subagent)))
    providers = data.get("providers", {})
    return isinstance(providers, dict) and all(
        isinstance(name, str) and isinstance(spec, dict) and isinstance(spec.get("reads"), list)
        and all(item in FAMILIES for item in spec["reads"]) and isinstance(spec.get("session_search", False), bool)
        for name, spec in providers.items())


def load_scope(plugin_dir):
    """The scope a profile's plugin was installed with, or None when the file is missing or malformed."""
    try:
        with open(os.path.join(str(plugin_dir), SCOPE_FILE), encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    return data if valid_scope(data) else None


def read_scope(plugin_dir):
    """The default read families of a profile's plugin; none (every read refused) without a valid scope."""
    data = load_scope(plugin_dir)
    return frozenset(data["reads"]) if data else frozenset()


def session_scope(data, provider):
    """(read families, session_search allowed) for a session running on `provider` under scope `data`.

    One bot may serve two accounts (private and work): a session on a provider with its own entry gets only that
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
                                    "--json", "--sort", "--order", "--language")),
    ("search", "issues"): grammar((), ("--repo", "-R", "--owner", "--limit", "-L", "--state", "--author",
                                       "--label", "--json", "--sort", "--order", "--language")),
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


def gh_value(option, value):
    if option in ("--repo", "-R"):
        return bool(GH_REPO.fullmatch(value))
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
    if tuple(args[:2]) == ("issue", "view") and "--no-download" not in args[2:]:
        return False
    return spec is not None and split_options(args[2:], spec) is not None


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


def read_only_command(command, profile=True):
    """True only for a single plain read command of the allowed families: calendar/Gmail reads via the Google
    script, Todoist reads, and the research reads (`slack_read_command`). `profile` is a set of families or a
    launch profile ("mail"/True: Google and Todoist; "research": all). "private" and unknown profiles allow
    no command.

    No shell syntax at all (pipes, redirects, substitutions, chaining), so the allowed program is the
    only thing that runs. Programs and scripts must be the trusted ones, and their options must be in an
    explicit read grammar. Anything else is refused, however harmless it looks.
    """
    families = families_for(profile)
    if not families:
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
    if trusted_program(tokens[0], words, "td"):
        return TODOIST in families and todoist_read(tokens)
    if skill_script(tokens, words, GOOGLE_API):
        return GOOGLE in families and (tokens[2], tokens[3]) in GOOGLE_READS
    return slack_read_command(tokens, words, families)


# --- Hook decision -------------------------------------------------------------------------------------

REFUSED_READ = ("Not run: this bot only reads (third-party content). Use hermes_auto_read for one plain read "
                "command, hermes_auto_skill_view/hermes_auto_skills_list for skills and session_search for earlier "
                "conversations. Sending and writing happen in Dolores (\"Kontynuuj w Dolores\").")
REFUSED_PROFILE = ("Not run: this bot may search only its own conversations (no `profile`, no `profile/id` "
                   "session links).")


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
# (tools/tool_search.py, `connections_in_scope`), so it is refused in both modes. `tool_describe` reaches the
# connector service only for `connectors__` names (tools/connectors/search.py, `remote_schemas_for`): it may load
# only the mode's own tools.
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
    return frozenset(name.strip() for name in names)


def describe_allowed(args, allowed):
    names = described_names(args)
    return bool(names) and names <= allowed


def bridged_names(args):
    """The real tool names inside a `tool_call` bridge call ({"name"} or {"calls": [{"name"}, ...]})."""
    if not isinstance(args, dict):
        return frozenset()
    calls = args.get("calls")
    if calls is None:
        calls = [args]
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except ValueError:
            return frozenset({""})
    if isinstance(calls, dict):
        calls = [calls]
    if not isinstance(calls, list):
        return frozenset({""})
    return frozenset(str(call.get("name") or "") if isinstance(call, dict) else "" for call in calls)


REFUSED_SEARCH = ("Not run: earlier conversations are not searchable from a session on this account (the bot also "
                  "serves another account's matters).")


def decide(tool_name, args, search=True):
    """The block message for one tool call in a read-only bot's session, or None to let it run: only the
    plugin's tools, session_search in this profile (when the session's account may search) and the Tool Search
    bridge to them (tool_call, tool_describe of these tools; never tool_search)."""
    if tool_name == "session_search":
        if not search:
            return REFUSED_SEARCH
        return None if own_profile_search(args) else REFUSED_PROFILE
    if tool_name in ALLOWED_TOOLS:
        return None
    if tool_name == SEARCH_TOOL:
        return REFUSED_TOOL_SEARCH
    if tool_name == DESCRIBE_TOOL:
        return None if describe_allowed(args, ALLOWED_TOOLS) else REFUSED_READ
    if tool_name == "tool_call":
        names = bridged_names(args)
        if names and names <= ALLOWED_TOOLS:
            return None  # Hermes unwraps the bridge and checks the real tool again before it runs
    return REFUSED_READ


# --- first_turn: Dolores' automatic sessions ------------------------------------------------------------

# Appended by the bridge to the automatic prompt (model text and transcript card): a user row carrying it is the
# automation's own message, never the user's. Text inside a third party's message can only keep the lock.
AUTO_TURN_MARK = "<!-- hermes-auto:auto-turn -->"
REGISTRY = ("hermes-auto-guard", "sessions")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
# User rows Hermes writes itself with no display kind: notices and wake-ups (old rows), compaction handoffs.
SYSTEM_PREFIXES = ("[IMPORTANT:", "[System", "[ASYNC", "[INTERNAL", "[CONTEXT COMPACTION", "[CONTEXT SUMMARY]",
                   "[PRIOR CONTEXT", "[STILL IN PROGRESS")
# Display kinds of rows a person typed: a normal message (NULL), a steer, a hidden composer send.
HUMAN_KINDS = ("steer", "hidden")
FIRST_TURN_TOOLS = frozenset({SKILL_VIEW_TOOL, SKILLS_LIST_TOOL})
# What tool_describe may load in a locked session (no connector name: those are described remotely).
FIRST_TURN_DESCRIBE = frozenset({"delegate_task", "session_search"}) | FIRST_TURN_TOOLS
REFUSED_FIRST_TURN = (
    "Nie uruchomiono: to pierwsza, automatyczna tura sesji z treścią od osoby trzeciej. Teraz działa tylko "
    "delegate_task z zadaniami `profile: \"{subagent}\"` (agent {subagent} czyta i przygotowuje sprawę), "
    "session_search w tym profilu i odczyt skilli. Pełne narzędzia ruszą po pierwszej wiadomości Łukasza w tej "
    "sesji. (Not run: automatic first turn; full tools after the user's first message.)")
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


def register_session(home, session_id, job_id=None):
    """Mark a session of this profile as an automatic one with third-party content (before its first prompt)."""
    from pathlib import Path
    if not valid_session_id(session_id):
        raise ValueError(f"invalid session id {session_id!r}")
    directory = Path(registry_dir(home))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / session_id
    temporary = directory / f".{session_id}.{os.getpid()}"
    temporary.write_text(json.dumps({"job": job_id, "version": 1}) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def is_registered(home, session_id):
    return valid_session_id(session_id) and os.path.isfile(os.path.join(registry_dir(home), session_id))


def _ancestors(conn, session_id):
    """(compression ancestors oldest last, complete). Complete when the walk reached a session that is no
    compression continuation within COMPRESSION_HOPS ancestors; a cycle or a longer lineage is incomplete."""
    found, current = [], session_id
    for _ in range(COMPRESSION_HOPS + 1):
        row = conn.execute("SELECT p.id, p.end_reason FROM sessions c JOIN sessions p ON p.id = c.parent_session_id "
                           "WHERE c.id = ?", (current,)).fetchone()
        if row is None or row[1] != "compression":
            return found, True
        if row[0] == session_id or row[0] in found:
            return found, False  # a cycle
        if len(found) == COMPRESSION_HOPS:
            return found, False  # more ancestors than the walk reads
        found.append(row[0])
        current = row[0]
    return found, False


def human_row(content, api_content, kind, metadata, summary):
    """Whether one user row was written by a person (see the module docstring for what counts)."""
    if summary or (kind is not None and kind not in HUMAN_KINDS):
        return False
    if kind == "hidden" and metadata and "notification_category" in str(metadata):
        return False  # a muted diagnostic wake
    texts = [str(content or ""), str(api_content or "")]
    if any(AUTO_TURN_MARK in text for text in texts):
        return False
    return not str(content or "").lstrip().startswith(SYSTEM_PREFIXES)


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
        except sqlite3.Error:
            return LOCKED, ""
        return (OPEN, "") if any(human_row(*row) for row in rows) else (LOCKED, "")
    finally:
        conn.close()


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


def delegation_allowed(args, subagent):
    """`delegate_task` that only hands work to `subagent`: `list`, or a spawn whose every task names that profile
    (no top-level goal: Hermes would run it in this profile) and carries no local images."""
    if not isinstance(args, dict):
        return False
    action = str(args.get("action") or "").strip().lower()
    if action == "list":
        return True
    if action not in ("", "spawn") or args.get("images"):
        return False
    tasks = args.get("tasks")
    if isinstance(tasks, str):
        try:
            tasks = json.loads(tasks)
        except ValueError:
            return False
    return (isinstance(tasks, list) and bool(tasks)
            and all(isinstance(task, dict) and task.get("profile") == subagent and not task.get("images")
                    for task in tasks))


def decide_first_turn(tool_name, args, subagent):
    """The block message for one tool call in a locked automatic session of Dolores' profile, or None."""
    allowed = None
    if tool_name == "delegate_task":
        allowed = delegation_allowed(args, subagent)
    elif tool_name == "session_search":
        allowed = own_profile_search(args)
    elif tool_name in FIRST_TURN_TOOLS:
        allowed = True
    elif tool_name == SEARCH_TOOL:
        return REFUSED_TOOL_SEARCH
    elif tool_name == DESCRIBE_TOOL:
        allowed = describe_allowed(args, FIRST_TURN_DESCRIBE)
    elif tool_name == "tool_call":
        calls = bridged_calls(args)
        allowed = bool(calls) and all(name != "tool_call" and decide_first_turn(name, arguments, subagent) is None
                                      for name, arguments in calls)
    return None if allowed else REFUSED_FIRST_TURN.format(subagent=subagent)


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


def read_argv(command, profile):
    """The argv to run for one read `command` under `profile` (families or a launch profile), or None when it is
    not a read.

    No shell runs it: the grammar's own checks still refuse shell syntax, and the program and script
    tokens are replaced by the exact trusted paths the grammar accepted (a `~` is expanded here, as the
    shell would have)."""
    if not read_only_command(command, profile):
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


def run_read(command, profile, *, timeout=COMMAND_TIMEOUT, cwd=None):
    """Run one read command (no shell) and return the tool result JSON."""
    argv = read_argv(command, profile)
    if argv is None:
        return neutral(REFUSED_READ + " This command is not one of the allowed reads (one plain command, "
                       "no pipes or redirects; see the session's first message for the commands).")
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
    if len(output) > OUTPUT_LIMIT:
        output = output[:OUTPUT_LIMIT] + f"\n[... truncated at {OUTPUT_LIMIT} characters; narrow the query]"
    return json.dumps({"output": output, "exit_code": done.returncode}, ensure_ascii=False)
