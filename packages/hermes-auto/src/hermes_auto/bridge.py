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


def show_clean_prompt(agent_class, prompt, display):
    """The session shows `display` (a short card) as the first message; the model gets `prompt`.

    Hermes' own mechanism: `persist_user_message` is the transcript text, and the sent bytes are kept as the
    row's `api_content`, so later turns (the user continuing in Desktop) still see the full prompt and source.
    Only the turn carrying exactly `prompt` is affected."""
    if not display or display == prompt:
        return
    original = agent_class.run_conversation

    def run_conversation(self, user_message, *args, **kwargs):
        if user_message == prompt and kwargs.get("persist_user_message") is None:
            kwargs["persist_user_message"] = display
            kwargs.setdefault("title_user_message", display)
        return original(self, user_message, *args, **kwargs)

    agent_class.run_conversation = run_conversation


def unique_title(db, sid, title):
    """Hermes keeps titles unique; a second "GitHub · Aviad: review" becomes "GitHub · Aviad: review (2)"."""
    limit = db.MAX_TITLE_LENGTH
    for attempt in range(1, 50):
        suffix = "" if attempt == 1 else f" ({attempt})"
        candidate = title[:limit - len(suffix)].rstrip() + suffix
        try:
            db.set_session_title(sid, candidate)
            return candidate
        except ValueError as exc:
            if "already in use" not in str(exc):
                raise
    raise RuntimeError("No free session title")


SHELL_META = set(";|&<>`$\n\r\\(){}")
GOOGLE_API = "skills/productivity/google-workspace/scripts/google_api.py"
GOOGLE_READS = {("calendar", "list"), ("gmail", "search"), ("gmail", "get"), ("gmail", "labels")}
TODOIST_READS = {("today",), ("upcoming",), ("inbox",), ("completed",), ("activity",),
                 ("task", "list"), ("task", "view"), ("project", "list"), ("project", "view"),
                 ("section", "list"), ("label", "list"), ("filter", "list"), ("filter", "view")}
READ_TOOLS = frozenset({"skill_view", "skills_list", "session_search"})
# Unattended research cannot send private context to arbitrary web endpoints.
SLACK_READ_TOOLS = READ_TOOLS
# Research read profiles (Slack, GitHub); "slack" is kept for sessions queued before the rename.
RESEARCH_PROFILES = ("research", "slack")
# Tools each deny profile may execute. "private" (redacted private Slack content) and any unknown
# profile execute nothing.
PROFILE_TOOLS = {True: READ_TOOLS, "research": SLACK_READ_TOOLS, "slack": SLACK_READ_TOOLS}
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
# `gh api` stays a GET: no fields, input, method, headers, host or template options. `--jq` filters the
# response inside gh (no file, network or process access), so it is a read like the plain call.
GH_API = grammar(("--paginate", "--include", "-i", "--slurp"), ("--jq", "-q"))


def gh_value(option, value):
    if option in ("--repo", "-R"):
        return bool(GH_REPO.fullmatch(value))
    return "://" not in value


def github_argument(token):
    return "://" not in token or token.startswith("https://github.com/")


def gh_read(tokens):
    """`gh <group> <read command>` or `gh api <GET endpoint>` with only the options in its grammar."""
    args = tokens[1:]
    if args[:1] == ["api"]:
        positionals = split_options(args[1:], GH_API)
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
    import shlex
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


def terminal_output_dirs():
    """Where Hermes spills a long command output; the agent reads it back with read_file/search_files."""
    return [os.path.join(home, "cache", "terminal-output") for home in hermes_homes()]


def cached_output_read(name, arguments):
    """read_file/search_files inside Hermes' terminal-output cache only: text the step already fetched.

    The path is resolved (symlinks, `..`) before the check, so nothing outside the cache is reachable."""
    if name not in ("read_file", "search_files"):
        return False
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        return False
    real = os.path.realpath(os.path.expanduser(path))
    for directory in terminal_output_dirs():
        root = os.path.realpath(directory)
        if real == root or real.startswith(root + os.sep):
            return True
    return False


def refusal(call, note):
    """A neutral result for a call the automatic step does not run: no `error` key, so Desktop shows a
    skipped step instead of a red failure; the note still tells the model what to do instead."""
    payload = {"skipped": True, "note": note}
    if call.function.name == "terminal":
        payload.update(output="", exit_code=1)  # Desktop renders an empty exit 1 as a grey notice
    return json.dumps(payload, ensure_ascii=False)


class CallSubset:
    """The assistant message with only some of its tool calls; Hermes' executors read nothing else."""

    def __init__(self, message, calls):
        self._message = message
        self.tool_calls = calls

    def __getattr__(self, name):
        return getattr(self._message, name)


def deny_tool_execution(agent_class, allowed=None, profile=True):
    """Keep the session's normal tool list (and so its stored prompt) but execute only reads.

    Used when third-party content enters a conversation: every call outside `allowed` (by default the
    profile's PROFILE_TOOLS; none for "private" or an unknown profile), and every terminal command that
    is not a plain read (`read_only_command`), gets an error result instead of running, and the turn
    continues. Wrappers such as tool_call, execute_code and delegate_task are never allowed. Filtering
    the tool list instead would rebuild the stored system prompt with a reduced toolset, and the user's
    later turns in that session would lose their tools.
    """
    from agent.turn_tool_validation import _append_tool_error_results
    if allowed is None:
        allowed = PROFILE_TOOLS.get(profile, frozenset())
    original = agent_class._execute_tool_calls

    def permitted(call):
        name = call.function.name
        if name in allowed:
            return True
        if name not in ("terminal", "read_file", "search_files") or profile not in PROFILE_TOOLS:
            return False
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except (TypeError, ValueError):
            return False
        if not isinstance(arguments, dict):
            return False
        if name != "terminal":
            return cached_output_read(name, arguments)
        return not arguments.get("background") and read_only_command(arguments.get("command"), profile)

    if profile in RESEARCH_PROFILES:
        refused = ("Not run: this automatic step only reads (third-party messages). Allowed: Slack MCP reads, "
                   "gh/linear reads (code via `gh api repos/OWNER/REPO/contents/PATH?ref=SHA --jq '.content | @base64d'`), calendar list, "
                   "td list/view (search: `td task list --filter 'search: phrase'`), session_search, skills, and "
                   "read_file/search_files on saved command output. Local git and the web are off. One plain "
                   "command per call, no pipes. Sending and writing wait for the user's explicit OK.")
    elif profile is True:
        refused = ("Not run: this automatic step only reads (third-party mail). Allowed: calendar list, gmail "
                   "search/get, td list/view/today (search: `td task list --filter 'search: phrase'`), "
                   "session_search, skills, and read_file/search_files on saved command output. One plain command "
                   "per call, no pipes.")
    else:
        refused = ("Not run: no tools in this automatic step (private third-party content). Answer from the "
                   "conversation only.")

    def guarded(self, assistant_message, messages, *args, **kwargs):
        calls = list(getattr(assistant_message, "tool_calls", None) or [])
        allowed_calls = [call for call in calls if permitted(call)]
        if len(allowed_calls) == len(calls):
            return original(self, assistant_message, messages, *args, **kwargs)
        # Per call, not per batch: the reads in a parallel batch still run; only the refused calls get a
        # neutral "not run" result. Every tool call keeps exactly one result.
        result = None
        if allowed_calls:
            result = original(self, CallSubset(assistant_message, allowed_calls), messages, *args, **kwargs)
        _append_tool_error_results(messages, [call for call in calls if call not in allowed_calls],
                                   lambda call: refusal(call, refused))
        return result

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


def route_kwargs(request):
    """`cli.main` keyword arguments for the job's `routes` rule; empty keeps Hermes' configured model."""
    route = request.get("route") or {}
    return {key: route[key] for key in ("provider", "model") if route.get(key)}


def pin_route(db, sid, session, request):
    """Record a routed job's provider on its session row: Desktop resumes a session on `model_config.provider`,
    else on the profile default, so a reply in a work-account session would run on the personal account.
    The row is created here, before the agent, so the agent never writes its own route."""
    provider = route_kwargs(request).get("provider")
    stored = (session or {}).get("model_config") or "{}"
    try:
        stored = json.loads(stored) if isinstance(stored, str) else dict(stored)
    except (TypeError, ValueError):
        stored = {}
    if provider and not stored.get("provider"):
        db.patch_session_model_config(sid, {"provider": provider})


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


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    config = request["config"]
    os.environ["HERMES_HOME"] = config.get("hermes_runtime_home", config["hermes_home"])
    for key in ("HERMES_YOLO", "HERMES_KANBAN_TASK", "HERMES_KANBAN_GOAL_MODE"):
        os.environ.pop(key, None)
    sys.path.insert(0, config["hermes_source"])
    import hermes_bootstrap  # noqa: F401
    os.environ["HERMES_HOME"] = config["hermes_home"]
    # Only run/create import run_agent, which loads the profile's .env; classify calls call_llm directly,
    # so a routed provider's key_env (e.g. a work account) resolved to the no-key placeholder → 401.
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv(hermes_home=config["hermes_home"])
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
        # Clean first message: the transcript text differs from what the model is sent.
        assert "persist_user_message" in inspect.signature(AIAgent.run_conversation).parameters
        # Delivery guard of retry-followup reads the whole compression lineage.
        assert callable(db.get_compression_lineage) and callable(db.resolve_resume_session_id)
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
        db.close()
        return
    if action == "create":
        if session is None:
            db.create_session(sid, source="cli")
        if session is None or not session.get("title"):
            # A failed earlier create can leave an untitled session; finish it instead of skipping.
            unique_title(db, sid, request["title"])
        if request.get("hidden"):
            # Listed in the Desktop "Auto" page instead of the main Sessions list.
            db.set_session_hidden(sid, True)
        pin_route(db, sid, session, request)
        if job["mode"] == "open" and not db.get_messages(sid):
            db.append_message(sid, "user", content=request["prompt"])
            db.append_message(sid, "assistant", content=request["open_message"])
        db.close()
        print(json.dumps({"session_id": sid}))
        return
    if action != "run" or session is None:
        raise RuntimeError("Target session is missing; it will not be recreated")
    pin_route(db, sid, session, request)
    db.close()
    from run_agent import AIAgent
    import cli
    import toolsets
    # Only this worker's in-memory configuration changes. Interactive Hermes keeps its settings.
    # Unattended runs never get interactive approval: dangerous commands are denied.
    cli.CLI_CONFIG["approvals"] = {"mode": "manual", "single_query_mode": "deny"}
    show_clean_prompt(AIAgent, request["prompt"], request.get("display"))
    profile = request.get("deny_tools")
    if profile:
        # Third-party content inside a normal session: the session keeps its full toolset and prompt.
        # Research profiles (Slack, GitHub) run reads only (Slack, code, PRs, Linear, web) and draft; mail
        # runs plain reads (calendar, Gmail search/get, Todoist, past sessions, skills); "private" and
        # unknown profiles execute no tool. Nothing is sent.
        desktop_parity(AIAgent)
        deny_tool_execution(AIAgent, profile=profile)
        research = profile in RESEARCH_PROFILES
        cli.main(query=request["prompt"], resume=sid, quiet=True, oneshot=True, output_format="stream-json",
                 max_turns=config["agent_max_turns" if research else "max_turns"],
                 run_budget=config["agent_run_budget_seconds" if research else "run_budget_seconds"],
                 **route_kwargs(request))
        return
    if request.get("full_tools"):
        # Todoist tasks and mail the user labelled for the agent: normal toolsets, skills and project rules.
        desktop_parity(AIAgent)
        cli.main(query=request["prompt"], resume=sid, quiet=True, oneshot=True, output_format="stream-json",
                 max_turns=config["agent_max_turns"], run_budget=config["agent_run_budget_seconds"],
                 **route_kwargs(request))
        return
    allowed = {"web_search", "web_extract"} if job["mode"] in ("research", "agent") else set()
    guard_agent(AIAgent, allowed)
    toolsets.TOOLSETS["hermes-auto-preparation"] = {
        "description": "Hermes Auto bounded preparation", "tools": sorted(allowed), "includes": [],
    }
    cli.CLI_CONFIG["tool_search"] = {"enabled": "off"}
    cli.main(query=request["prompt"], resume=sid, quiet=True, oneshot=True,
             output_format="stream-json", toolsets="hermes-auto-preparation", ignore_rules=True,
             max_turns=config["max_turns"], run_budget=config["run_budget_seconds"], **route_kwargs(request))


if __name__ == "__main__":
    main()
