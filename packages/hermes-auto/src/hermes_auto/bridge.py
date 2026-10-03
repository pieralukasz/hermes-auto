"""Executed by Hermes' Python, not the package's Python. No changes to Hermes source files."""
from __future__ import annotations

import json
import os
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


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    config = request["config"]
    os.environ["HERMES_HOME"] = config.get("hermes_runtime_home", config["hermes_home"])
    for key in ("HERMES_YOLO", "HERMES_KANBAN_TASK", "HERMES_KANBAN_GOAL_MODE"):
        os.environ.pop(key, None)
    sys.path.insert(0, config["hermes_source"])
    import hermes_bootstrap  # noqa: F401
    os.environ["HERMES_HOME"] = config["hermes_home"]
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
        print(json.dumps({"ok": True, "python": sys.executable, "source": config["hermes_source"]}))
        db.close()
        return
    session = db.get_session(sid)
    if action == "inspect":
        print(json.dumps({"exists": session is not None,
                          "messages": session.get("message_count", 0) if session else 0}))
        db.close()
        return
    if action == "create":
        if session is None:
            db.create_session(sid, source="cli")
            db.set_session_title(sid, request["title"])
        if job["mode"] == "open" and not db.get_messages(sid):
            db.append_message(sid, "user", content=request["prompt"])
            db.append_message(sid, "assistant", content=request["open_message"])
        db.close()
        print(json.dumps({"session_id": sid}))
        return
    if action != "run" or session is None:
        raise RuntimeError("Target session is missing; it will not be recreated")
    db.close()
    from run_agent import AIAgent
    import cli
    import toolsets
    allowed = {"web_search", "web_extract"} if job["mode"] == "research" else set()
    guard_agent(AIAgent, allowed)
    toolsets.TOOLSETS["hermes-auto-preparation"] = {
        "description": "Hermes Auto bounded preparation", "tools": sorted(allowed), "includes": [],
    }
    # Only this worker's in-memory configuration changes. Interactive Hermes keeps its settings.
    cli.CLI_CONFIG["approvals"] = {"mode": "manual", "single_query_mode": "deny"}
    cli.CLI_CONFIG["tool_search"] = {"enabled": "off"}
    cli.main(query=request["prompt"], resume=sid, quiet=True, oneshot=True,
             output_format="stream-json", toolsets="hermes-auto-preparation", ignore_rules=True,
             max_turns=config["max_turns"], run_budget=config["run_budget_seconds"])


if __name__ == "__main__":
    main()
