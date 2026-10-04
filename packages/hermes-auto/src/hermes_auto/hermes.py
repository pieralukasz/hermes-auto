from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

from .config import private_json
from .runner import full_tools

# Hermes SessionDB.MAX_TITLE_LENGTH; a longer title makes session creation fail.
TITLE_LIMIT = 100


def session_title(job, limit=TITLE_LIMIT):
    """`☀ <title> · <source> · #<id>`, shortening only the task title so the suffix survives."""
    suffix = f" · {job['source']} · #{job['id']}"
    text = " ".join(str(job["title"] or "").split())
    room = limit - len(suffix) - 2
    if len(text) > room:
        text = text[:room - 1].rstrip() + "…"
    return f"☀ {text}{suffix}"


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

    def invoke(self, action, job=None, **extra):
        with tempfile.TemporaryDirectory(prefix="request-", dir=self.home) as directory:
            request = Path(directory) / "request.json"
            private_json(request, {"action": action, "config": self.config, "job": job or {}, **extra})
            command = [self.python, "-I", str(Path(__file__).with_name("bridge.py")), str(request)]
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                process = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True)
                try:
                    process.wait(timeout=self.budget(job) + 30 if action == "run" else 90)
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
                lines = out.read().decode(errors="replace").splitlines()
                records = []
                for line in lines:
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict):
                            records.append(record)
                    except ValueError:
                        pass
                if process.returncode:
                    # Keep private diagnostics locally; CLI output does not expose email/task bodies.
                    err.seek(0)
                    diagnostic = err.read().decode(errors="replace")[-12000:]
                    private_json(self.home / "last-error.json", {"action": action, "error": diagnostic})
                    raise RuntimeError(f"Hermes {action} failed (exit {process.returncode}); see local last-error.json")
                if action == "run":
                    results = [record for record in records if record.get("type") == "result"]
                    if not results or results[-1].get("is_error") or results[-1].get("exit_code", 0):
                        err.seek(0)
                        private_json(self.home / "last-error.json", {
                            "action": action, "result": results[-1] if results else None,
                            "record_types": [record.get("type") for record in records][-40:],
                            "stdout_tail": "\n".join(lines)[-6000:],
                            "stderr_tail": err.read().decode(errors="replace")[-6000:]})
                        raise RuntimeError("Hermes returned no confirmed successful result; see local last-error.json")
                    return results[-1]
                if not records:
                    raise RuntimeError(f"Hermes {action} returned no receipt")
                return records[-1]

    def budget(self, job):
        key = "agent_run_budget_seconds" if job and full_tools(job) else "run_budget_seconds"
        return self.config[key]

    def exists(self, job):
        return self.invoke("inspect", job)["exists"]

    def message_count(self, job):
        return self.invoke("inspect", job)["messages"]

    def create(self, job, prompt):
        title = session_title(job)
        message = ("Sesja jest gotowa. Automatyczne przygotowanie jest wyłączone; możesz zacząć rozmowę."
                   if self.config["language"].lower() in ("polish", "polski", "pl") else
                   "This session is ready. Automatic preparation is disabled; continue when you are ready.")
        return self.invoke("create", job, title=title, prompt=prompt, open_message=message)

    def run(self, job, prompt):
        return self.invoke("run", job, prompt=prompt, full_tools=full_tools(job))
