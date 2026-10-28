"""Hermes Auto guard: the read rules of automatic sessions that carry third-party content.

Shared by the Hermes plugin (``__init__.py``, loaded by the gateway) and by hermes-auto's bridge and tests.
Pure Python, no Hermes imports at module level: the bridge loads this file by path and the tests import it.

The boundary has two layers. The gateway freezes each automatic session's toolsets when hermes-auto creates
it, so a session with someone else's mail or Slack never gets terminal, file, skill_manage or any send tool
(fail-closed even without this plugin: without it, the ``hermes-auto`` toolset does not exist and the
gateway refuses the session). This module is the second layer: the one read tool runs only commands its
grammar proves to be reads, and the ``pre_tool_call`` hook refuses every other tool in such a session.
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import sys
import time

GUARD_NAME = "hermes-auto-guard"
GUARD_VERSION = "1.1.0"
TOOLSET = "hermes-auto"
READ_TOOL = "hermes_auto_read"
SKILL_VIEW_TOOL = "hermes_auto_skill_view"
SKILLS_LIST_TOOL = "hermes_auto_skills_list"
PLUGIN_TOOLS = (READ_TOOL, SKILL_VIEW_TOOL, SKILLS_LIST_TOOL)

# Session profiles hermes-auto registers. "mail" and "research" read through READ_TOOL; "private" has no
# tools at all; "full" is a Todoist task (or a mail the user gave the agent label) with normal tools, where
# only commands that would need an approval are refused while the automatic turn runs.
MAIL, RESEARCH, PRIVATE, FULL = "mail", "research", "private", "full"
DENY_PROFILES = (MAIL, RESEARCH, PRIVATE)
PROFILES = DENY_PROFILES + (FULL,)
ALLOWED_TOOLS = {MAIL: frozenset(PLUGIN_TOOLS) | {"session_search"},
                 RESEARCH: frozenset(PLUGIN_TOOLS) | {"session_search"},
                 PRIVATE: frozenset()}
# Tools a "full" automatic turn may not run without a human: arbitrary local code outside the terminal's
# command approval (Hermes asks before execute_code in an attended session) and desktop control (each
# computer_use action goes through Hermes' approval gate, which `approvals.mode: off` approves).
FULL_REFUSED_TOOLS = frozenset({"execute_code", "computer_use"})
OUTPUT_LIMIT = 30000
COMMAND_TIMEOUT = 120
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def profile_for(deny_tools, full_tools=False):
    """The guard profile of a job: its `deny_tools` value (True, "research"/"slack", other) or "full"."""
    if deny_tools:
        if deny_tools is True:
            return MAIL
        return RESEARCH if deny_tools in ("research", "slack") else PRIVATE
    return FULL if full_tools else None


SHELL_META = set(";|&<>`$\n\r\\(){}")
GOOGLE_API = "skills/productivity/google-workspace/scripts/google_api.py"
GOOGLE_READS = {("calendar", "list"), ("gmail", "search"), ("gmail", "get"), ("gmail", "labels")}
TODOIST_READS = {("today",), ("upcoming",), ("inbox",), ("completed",), ("activity",),
                 ("task", "list"), ("task", "view"), ("project", "list"), ("project", "view"),
                 ("section", "list"), ("label", "list"), ("filter", "list"), ("filter", "view")}
# Research read profiles (Slack, GitHub); "slack" is kept for sessions queued before the rename.
RESEARCH_PROFILES = ("research", "slack")
SLACK_MCP = "skills/tap-in/tapin-slack/scripts/slack_mcp.py"
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
    """`python <hermes home>/<relative> ...` with a trusted interpreter and the exact skill script."""
    if len(tokens) < 4 or not trusted_python(tokens[0], words):
        return False
    return literal_path(tokens[1], words) in {os.path.join(home, relative) for home in hermes_homes()}


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


def slack_read_command(tokens, words=None):
    """Reads of the Slack session's research: trusted Slack, GitHub and Linear commands."""
    words = set(tokens) if words is None else words
    if skill_script(tokens, words, SLACK_MCP):
        return tokens[2] == "call" and tokens[3] in SLACK_MCP_READS and len(tokens) <= 5
    if trusted_program(tokens[0], words, "gh"):
        return gh_read(tokens)
    if trusted_program(tokens[0], words, "linear"):
        return linear_read(tokens)
    return False


def read_only_command(command, profile=True):
    """True only for a single plain read command: calendar/Gmail reads via the Google script, Todoist reads.
    With a research profile also the Slack session's research reads (`slack_read_command`). Profile
    "private" and unknown profiles allow no command.

    No shell syntax at all (pipes, redirects, substitutions, chaining), so the allowed program is the
    only thing that runs. Programs and scripts must be the trusted ones, and their options must be in an
    explicit read grammar. Anything else is refused, however harmless it looks.
    """
    if profile == MAIL:
        profile = True
    if profile is not True and profile not in RESEARCH_PROFILES:
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
        return todoist_read(tokens)
    if skill_script(tokens, words, GOOGLE_API):
        return (tokens[2], tokens[3]) in GOOGLE_READS
    if profile in RESEARCH_PROFILES:
        return slack_read_command(tokens, words)
    return False



# --- Registry: which sessions are automatic, written by hermes-auto's bridge, read by the plugin ---------

def registry_dir(hermes_home):
    return os.path.join(str(hermes_home), "hermes-auto-guard", "sessions")


def registration_path(hermes_home, session_id):
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return None
    return os.path.join(registry_dir(hermes_home), session_id + ".json")


def write_registration(hermes_home, session_id, profile, until=None, job=None):
    """Mark `session_id` as an automatic session with `profile` (0600 file, atomic replace).

    `until` (epoch seconds) bounds a "full" session's restrictions to its automatic turn; None for the
    read profiles, whose restrictions hold for the whole session like its frozen toolsets."""
    if profile not in PROFILES:
        raise ValueError(f"unknown guard profile: {profile!r}")
    path = registration_path(hermes_home, session_id)
    if path is None:
        raise ValueError("invalid session id")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    data = {"version": 1, "profile": profile, "until": until, "job": job}
    temporary = f"{path}.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return data


def read_registration(hermes_home, session_id):
    """The registration of `session_id`, or None when it is not an automatic session.

    An unreadable or malformed file of an automatic session reads as "private" (no tools): the file
    exists, so the session is one of ours, and the safe answer is the strictest profile."""
    path = registration_path(hermes_home, session_id)
    if path is None or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        if not valid_registration(data):
            raise ValueError("bad registration")
        return data
    except (OSError, ValueError):
        return {"version": 1, "profile": PRIVATE, "until": None, "job": None}


def valid_registration(data):
    """The exact shape write_registration writes: version 1, a known profile, `until` None or a finite
    number (not a bool). Anything else (a hand-edited or truncated file) is not trusted."""
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("profile") not in PROFILES:
        return False
    until = data.get("until", "missing")
    if until is None:
        return True
    return isinstance(until, (int, float)) and not isinstance(until, bool) and math.isfinite(until)


def active(entry, now=None):
    """Whether a registration restricts tools now: always for read profiles, within `until` for "full"."""
    if entry is None:
        return False
    if entry["profile"] in DENY_PROFILES:
        return True
    until = entry.get("until")
    return isinstance(until, (int, float)) and not isinstance(until, bool) and (now or time.time()) < until


def active_full_registration(hermes_home, now=None):
    """Whether some "full" automatic turn is running now (its registration's `until` is in the future)."""
    try:
        with os.scandir(registry_dir(hermes_home)) as entries:
            names = [item.name[:-5] for item in entries if item.name.endswith(".json")]
    except OSError:
        return False
    for name in names:
        entry = read_registration(hermes_home, name)
        if entry is not None and entry["profile"] == FULL and active(entry, now):
            return True
    return False


def has_registrations(hermes_home):
    """Whether hermes-auto registered any session (one directory listing; False when there is none)."""
    try:
        with os.scandir(registry_dir(hermes_home)) as entries:
            return any(item.name.endswith(".json") for item in entries)
    except OSError:
        return False


# --- Hook decision -------------------------------------------------------------------------------------

REFUSED_READ = ("Not run: this automatic step only reads (third-party content). Use hermes_auto_read for "
                "one plain read command, hermes_auto_skill_view/hermes_auto_skills_list for skills and "
                "session_search for earlier conversations. Sending and writing wait for the user's OK.")
REFUSED_PRIVATE = "Not run: no tools in this automatic step (private third-party content). Answer from the conversation only."
REFUSED_FULL = ("Not run: this automatic run cannot ask the user to approve {what}. Find another way, or "
                "prepare it and leave it for the user's approval.")


REFUSED_PROFILE = ("Not run: this automatic step may search only this profile's conversations (no `profile`, "
                   "no `profile/id` session links).")
REFUSED_GUARD_FILES = "Not run: an automatic run may not change hermes-auto's guard or its session registry."


def own_profile_search(args):
    """session_search arguments that stay in the current profile: no `profile` (Hermes opens another
    profile's state.db for it) and no `<profile>/<id>` session link (it adopts the embedded profile)."""
    if not isinstance(args, dict):
        return False
    if str(args.get("profile") or "").strip():
        return False
    session_id = args.get("session_id")
    return session_id is None or (isinstance(session_id, str) and "/" not in session_id)


# Hermes' Tool Search bridge: plugin tools and session_search are deferred behind it, so a read session
# reaches its tools through these. tool_search/tool_describe only read the session's own catalog.
CATALOG_TOOLS = frozenset({"tool_search", "tool_describe"})


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


WRITE_TOOLS = frozenset({"write_file", "patch"})
PATCH_FILE = re.compile(r"^\*\*\* (?:Update File|Add File|Delete File|Move to):\s*(.+?)\s*$", re.M)
GUARD_MARK = GUARD_NAME  # both the plugin directory and the registry directory carry this name


def write_paths(tool_name, args):
    """Every path a write_file/patch call would touch (`path`, and the files a V4A patch names)."""
    if tool_name not in WRITE_TOOLS or not isinstance(args, dict):
        return []
    paths = [str(args["path"])] if args.get("path") else []
    if isinstance(args.get("patch"), str):
        paths += PATCH_FILE.findall(args["patch"])
    return paths


def touches_guard(paths, homes=None):
    """A path inside the guard plugin or its registry (after `~` expansion and realpath), in any Hermes home."""
    roots = []
    for home in homes or hermes_homes():
        roots += [os.path.join(home, GUARD_MARK), os.path.join(home, "plugins", GUARD_MARK)]
    roots = [os.path.realpath(root) for root in roots]
    for path in paths:
        expanded = os.path.expanduser(str(path))
        if not os.path.isabs(expanded):
            expanded = os.path.join(os.path.expanduser("~"), expanded)  # automatic sessions run in ~
        for candidate in (os.path.normpath(expanded), os.path.realpath(expanded)):
            if any(candidate == root or candidate.startswith(root + os.sep) for root in roots):
                return True
    return False


def decide(tool_name, args, entry, dangerous=None, now=None, gated_write=None, resolve=None):
    """The block message for one tool call of a registered session, or None to let it run.

    `dangerous(command) -> bool` is Hermes' own approval detector (tools.approval) and
    `gated_write(paths) -> bool` its approval-gated write paths (SSH config, protected instruction
    files), injected so this stays pure; when one is missing the call is refused. Read profiles allow
    only their tool list; "full" refuses, while its automatic turn runs, what an attended session would
    have to approve (with `approvals.mode: off` Hermes would approve it on its own) and any change to
    the guard itself. `resolve(path) -> path` makes a relative path absolute against the session's working
    directory (Hermes keeps a `cd` across terminal calls); without it relative paths count from ~."""
    if not active(entry, now):
        return None
    profile = entry["profile"]
    if profile in DENY_PROFILES:
        allowed = ALLOWED_TOOLS.get(profile, frozenset())
        if tool_name == "session_search" and "session_search" in allowed and not own_profile_search(args):
            return REFUSED_PROFILE
        if tool_name in allowed or (allowed and tool_name in CATALOG_TOOLS):
            return None
        if allowed and tool_name == "tool_call" and bridged_names(args) <= allowed and bridged_names(args):
            return None  # Hermes unwraps the bridge and checks the real tool again before it runs
        return REFUSED_PRIVATE if profile == PRIVATE else REFUSED_READ
    if tool_name in FULL_REFUSED_TOOLS:
        return REFUSED_FULL.format(what=f"running {tool_name}")
    if tool_name == "terminal":
        command = (args or {}).get("command") if isinstance(args, dict) else None
        if not isinstance(command, str) or dangerous is None:
            return REFUSED_FULL.format(what="this command")
        workdir = args.get("workdir")
        if GUARD_MARK in command or (isinstance(workdir, str) and (GUARD_MARK in workdir
                                                                     or touches_guard([workdir]))):
            return REFUSED_GUARD_FILES
        if dangerous(command):
            return REFUSED_FULL.format(what="this command")
    if tool_name in WRITE_TOOLS:
        paths = write_paths(tool_name, args)
        if resolve is not None:
            paths = [resolve(path) for path in paths]
        if touches_guard(paths):
            return REFUSED_GUARD_FILES
        if gated_write is None or gated_write(paths):
            return REFUSED_FULL.format(what="this write")
    return None


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
    """The argv to run for one read `command` under `profile`, or None when it is not a read.

    No shell runs it: the grammar's own checks still refuse shell syntax, and the program and script
    tokens are replaced by the exact trusted paths the grammar accepted (a `~` is expanded here, as the
    shell would have)."""
    if profile not in (MAIL, RESEARCH) or not read_only_command(command, profile):
        return None
    tokens = shlex.split(str(command).strip())
    words = set(str(command).split())
    for index in (0, 1)[:len(tokens)]:
        path = literal_path(tokens[index], words)
        if path is not None:
            tokens[index] = path
    return tokens


def run_read(command, profile, *, timeout=COMMAND_TIMEOUT, cwd=None):
    """Run one read command (no shell) and return the tool result JSON."""
    argv = read_argv(command, profile)
    if argv is None:
        return neutral(REFUSED_READ + " This command is not one of the allowed reads (one plain command, "
                       "no pipes or redirects; see the session's first message for the commands).")
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              cwd=cwd or os.path.expanduser("~"), errors="replace")
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
