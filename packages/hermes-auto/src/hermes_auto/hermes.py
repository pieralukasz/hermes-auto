from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
from pathlib import Path

from . import bots
from .config import model_route, private_json
from .runner import full_tools

# Hermes SessionDB.MAX_TITLE_LENGTH; a longer title makes session creation fail.
TITLE_LIMIT = 100
# A line the bridge writes to stderr when it refuses a create or run (bridge.REFUSAL_MARK).
REFUSAL_MARK = "hermes-auto-refusal:"


def deny_mode(job):
    """False (normal run), True (mail: plain reads only) or a named read profile such as "slack"."""
    value = job.get("deny_tools") or json.loads(job.get("payload") or "{}").get("deny_tools")
    return value if isinstance(value, str) and value else bool(value)


def job_route(job, config):
    """The `routes` rule for a job's runs (e.g. a work organisation's Slack and GitHub on its account)."""
    try:
        payload = json.loads((job or {}).get("payload") or "{}")
    except ValueError:
        payload = {}
    return model_route(config, (job or {}).get("source", ""), payload if isinstance(payload, dict) else {})


# Sources named at the start of a title ("GitHub · Aviad: review sports stat cards"). Briefs, plans and
# proposals already say what they are ("Brief 27.10 06:00").
SOURCE_LABELS = {"gmail": "Gmail", "proton": "Proton", "slack": "Slack", "github": "GitHub", "linear": "Linear",
                 "todoist": "Todoist"}
ARROWS = re.compile(r"^(?:↩\s*)+")
OLD_SUFFIX = re.compile(r"(?: · [a-z]+ · #\d+)+$")


def session_title(job, limit=TITLE_LIMIT, prefix=""):
    """`<prefix>[↩ ]<Source> · <title>`, shortened to Hermes' limit. A reply keeps one leading "↩".

    Hermes keeps titles unique; the bridge adds " (2)" on a clash. No prefix by default; the optional Desktop
    "Auto" page plugin needs `title_prefix: "☀ "`.
    """
    text = " ".join(str(job["title"] or "").split())
    arrow = "↩ " if ARROWS.match(text) else ""
    text = ARROWS.sub("", text)
    text = OLD_SUFFIX.sub("", text)  # a title copied from a session named before this format
    label = SOURCE_LABELS.get(job.get("source", ""))
    if label and not text.startswith(label + " · "):
        # Older names carried the source as "GitHub: ..." or "Slack: ..."; keep only one.
        text = f"{label} · " + re.sub(rf"^{re.escape(label)}:\s*", "", text)
    room = limit - len(prefix) - len(arrow)
    if len(text) > room:
        text = text[:room - 1].rstrip() + "…"
    return f"{prefix}{arrow}{text}"


def run_alive(pid, directory):
    """True when a process of the run's group still carries its run directory in its command line.

    A bare pid check would take a reused pid (any unrelated process) for the run, and the overrun kill
    would then signal that process. `start` makes the shell a group leader (pgid == pid) and passes the
    request path, so group plus directory identifies the run. None when `ps` itself fails.
    """
    try:
        listing = subprocess.run(["ps", "-A", "-ww", "-o", "pid=", "-o", "pgid=", "-o", "command="],
                                 capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    marker = str(directory)
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == str(pid) and marker in parts[2]:
            return True
    return False


class SessionBusy(RuntimeError):
    """Hermes refused the run: another surface (Desktop) holds the conversation (SESSION_NOT_OWNED)."""


class Hermes:
    def __init__(self, config, home):
        self.config = config
        self.home = home
        self.python = config.get("hermes_python")
        if not self.python:
            result = subprocess.run([config["hermes_command"], "--print-runtime-command"],
                                    capture_output=True, text=True, timeout=30, check=True)
            self.python = json.loads(result.stdout)[0]
        if not (Path(config["hermes_source"]) / "hermes_state_registry.py").is_file():
            raise RuntimeError("Set hermes_source to a supported Hermes Agent source installation")

    def profile_config(self, profile):
        """This config aimed at one bot: `hermes_home` is the bot's profile home (the gateway serves it through
        the multiplexer, like `hermes -p <bot>`), `bot` says what the bridge must check before using it."""
        profile = profile or bots.DEFAULT
        if profile == bots.DEFAULT and not bots.enabled(self.config):
            return self.config
        spec = bots.read_only(self.config).get(profile)
        return {**self.config, "hermes_home": bots.profile_home(self.config, profile),
                "bot": {"name": profile, "read_only": spec is not None,
                        "reads": sorted((spec or {}).get("reads") or []),
                        "providers": (spec or {}).get("providers") or {}}}

    def invoke(self, action, job=None, profile=None, **extra):
        """Run one bridge action. `profile`: the bot whose home it targets (default: the job's bot)."""
        profile = profile or (bots.of(job) if job else bots.DEFAULT)
        with tempfile.TemporaryDirectory(prefix="request-", dir=self.home) as directory:
            request = Path(directory) / "request.json"
            private_json(request, {"action": action, "config": self.profile_config(profile), "job": job or {},
                                   **extra})
            command = [self.python, "-I", str(Path(__file__).with_name("bridge.py")), str(request)]
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                process = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True)
                try:
                    process.wait(timeout=self.budget(job) + 30 if action == "run" else
                                 240 if action == "classify" else 90)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    # Reap any descendants as well, even if the parent already exited.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise RuntimeError("Hermes interrupted or exceeded its budget; inspect the existing session")
                out.seek(0)
                err.seek(0)
                return self.parse(action, process.returncode, out.read().decode(errors="replace"),
                                  err.read().decode(errors="replace"))

    def parse(self, action, returncode, stdout, stderr):
        lines = stdout.splitlines()
        records = []
        for line in lines:
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
            except ValueError:
                pass
        if returncode:
            # Keep private diagnostics locally; CLI output does not expose email/task bodies.
            diagnostic = stderr[-12000:]
            private_json(self.home / "last-error.json", {"action": action, "error": diagnostic})
            refused = [line.split(REFUSAL_MARK, 1)[1].strip() for line in stderr.splitlines() if REFUSAL_MARK in line]
            if refused:
                # The bridge refused before anything reached the session; its sentence is for the user.
                raise RuntimeError(refused[-1])
            if "hermes-refusal-reason: SESSION_NOT_OWNED" in diagnostic:
                raise SessionBusy(f"Hermes {action} refused: the session is open in another window")
            raise RuntimeError(f"Hermes {action} failed (exit {returncode}); see local last-error.json")
        if action == "run":
            results = [record for record in records if record.get("type") == "result"]
            if not results or results[-1].get("is_error") or results[-1].get("exit_code", 0):
                private_json(self.home / "last-error.json", {
                    "action": action, "result": results[-1] if results else None,
                    "record_types": [record.get("type") for record in records][-40:],
                    "stdout_tail": "\n".join(lines)[-6000:], "stderr_tail": stderr[-6000:]})
                raise RuntimeError("Hermes returned no confirmed successful result; see local last-error.json")
            return results[-1]
        if not records:
            raise RuntimeError(f"Hermes {action} returned no receipt")
        return records[-1]

    def start(self, job, prompt, display=None):
        """Start a run in the background and return a handle; `collect` reads it later.

        The process gets its own session (survives the tick and launchd) and writes its exit code to a
        file, so any later tick can tell finished, crashed and overdue runs apart.
        """
        import time
        runs = self.home / "runs"
        runs.mkdir(mode=0o700, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=f"job-{job['id']}-", dir=runs))
        request = directory / "request.json"
        full, deny = self.tools_of(job)
        private_json(request, {"action": "run", "config": self.profile_config(bots.of(job)), "job": job,
                               "prompt": prompt,
                               "display": display, "full_tools": full, "deny_tools": deny,
                               "route": job_route(job, self.config)})
        command = [self.python, "-I", str(Path(__file__).with_name("bridge.py")), str(request)]
        script = 'umask 077; "$@" >"$OUT" 2>"$ERR"; echo $? >"$CODE.tmp" && mv "$CODE.tmp" "$CODE"'
        env = {**os.environ, "OUT": str(directory / "stdout"), "ERR": str(directory / "stderr"),
               "CODE": str(directory / "exit_code")}
        process = subprocess.Popen(["/bin/sh", "-c", script, "hermes-auto-run", *command], env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, close_fds=True)
        return {"pid": process.pid, "dir": str(directory), "deadline": time.time() + self.budget(job) + 60}

    def collect(self, handle):
        """None while running; the run's result when done; raises like `run` on failure or overrun."""
        import shutil
        import time
        directory = Path(handle["dir"])
        if directory.resolve().parent != (self.home / "runs").resolve():
            # Provenance: only read (and later delete) a directory `start` made under this home's runs/.
            raise RuntimeError("Hermes run directory is not one this automation created; inspect the session")
        code_file = directory / "exit_code"
        if not code_file.exists():
            alive = run_alive(handle["pid"], directory)
            if alive is not False and time.time() < handle["deadline"]:
                return None
            if alive:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(handle["pid"], sig)
                    except (ProcessLookupError, PermissionError):
                        break
                    time.sleep(2)
                raise RuntimeError("Hermes exceeded its budget; inspect the existing session")
            if alive is None:
                raise RuntimeError("Hermes exceeded its budget and its process could not be identified; "
                                   "inspect the existing session")
            if not code_file.exists():
                raise RuntimeError("Hermes run stopped without an exit code; inspect the existing session")
        returncode = int(code_file.read_text().strip() or 1)
        stdout = (directory / "stdout").read_text(errors="replace") if (directory / "stdout").exists() else ""
        stderr = (directory / "stderr").read_text(errors="replace") if (directory / "stderr").exists() else ""
        try:
            return self.parse("run", returncode, stdout, stderr)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def tools_of(self, job):
        """(full_tools, deny_tools) for a job's session: a job in a read-only bot never has full tools and always
        has a read profile (an agent-labelled mail thread there reads like any other mail)."""
        if bots.is_read_only(self.config, bots.of(job)):
            return False, deny_mode(job) or True
        return full_tools(job), deny_mode(job)

    def budget(self, job):
        # Slack sessions research before drafting (reads only), so they get the full agent budget.
        key = ("agent_run_budget_seconds" if job and ((full_tools(job) and not job.get("deny_tools"))
                                                      or deny_mode(job) in ("research", "slack"))
               else "run_budget_seconds")
        return self.config[key]

    def exists(self, job):
        return self.invoke("inspect", job)["exists"]

    def inspect(self, session_id, profile=None):
        return self.invoke("inspect", {"session_id": session_id}, profile=profile)

    def classify(self, system, user, max_tokens=4000, route=None):
        """One model call through Hermes; `route` (provider/model) overrides the sorter's default model."""
        return self.invoke("classify", system=system, user=user, max_tokens=max_tokens, route=route or {})["result"]

    def find_session(self, **query):
        return self.invoke("find_session", **query)["session_id"]

    def recent_sessions(self, days=21, limit=400, exclude=()):
        """Recent user conversations (minus `exclude`, the automation's own): [{id, title, date}]. Read-only."""
        return self.invoke("recent_sessions", days=days, limit=limit, exclude=list(exclude))["result"]

    def session_excerpts(self, ids, profile=None):
        """{session_id: {title, date, last_user, last_assistant}}. Read-only."""
        return self.invoke("session_excerpts", profile=profile, ids=list(ids))["result"]

    def archive_states(self, sessions):
        """{session_id: True archived / False open / None missing}: one bridge process per bot profile.
        `sessions`: session ids (default profile) or (session_id, profile) pairs."""
        groups = {}
        for item in sessions:
            sid, profile = item if isinstance(item, tuple) else (item, bots.DEFAULT)
            groups.setdefault(profile or bots.DEFAULT, []).append(sid)
        states = {}
        for profile, ids in groups.items():
            states.update(self.invoke("archive_states", profile=profile, session_ids=ids)["states"])
        return states

    def doctor(self):
        """The bridge's checks against every bot profile the config names (just `default` without bots)."""
        names = bots.profiles(self.config) if bots.enabled(self.config) else [bots.DEFAULT]
        report = {}
        for name in names:
            try:
                report[name] = self.invoke("doctor", profile=name)
            except Exception as exc:  # noqa: BLE001 - one broken bot must not hide the others
                report[name] = {"ok": False, "error": str(exc)}
        return {"ok": all(entry.get("ok") and entry.get("ready", True) for entry in report.values()),
                "bots": report}

    def message_count(self, job):
        return self.invoke("inspect", job)["messages"]

    def create(self, job, prompt):
        title = session_title(job, prefix=self.config.get("title_prefix", ""))
        payload = json.loads(job.get("payload") or "{}")
        message = payload.get("open_message") or (
            "Sesja jest gotowa. Automatyczne przygotowanie jest wyłączone; możesz zacząć rozmowę."
            if self.config["language"].lower() in ("polish", "polski", "pl") else
            "This session is ready. Automatic preparation is disabled; continue when you are ready.")
        hidden = bool(self.config.get("triage", {}).get("hide_sessions", False))
        # The gateway creates a run's session (its id may differ from the reserved one); `open` keeps it.
        full, deny = self.tools_of(job)
        return self.invoke("create", job, title=title, prompt=prompt, open_message=message, hidden=hidden,
                           full_tools=full, deny_tools=deny,
                           route=job_route(job, self.config))["session_id"]

    def run(self, job, prompt, display=None):
        full, deny = self.tools_of(job)
        return self.invoke("run", job, prompt=prompt, display=display, full_tools=full, deny_tools=deny,
                           route=job_route(job, self.config))
