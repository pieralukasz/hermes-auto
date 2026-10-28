"""Install the hermes-auto-guard Hermes plugin (shipped in `guard_plugin/`) into a read-only bot's profile."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from .guard_plugin import policy

PLUGIN = "hermes-auto-guard"
FILES = ("plugin.yaml", "__init__.py", "policy.py")
SOURCE = Path(__file__).with_name("guard_plugin")


def target(hermes_home):
    return Path(hermes_home).expanduser() / "plugins" / PLUGIN


def scope_data(reads, providers=None):
    """The scope.json content: default reads, plus per-provider reads for sessions on another account."""
    data = {"version": 1, "reads": sorted(set(reads))}
    if providers:
        data["providers"] = {name: {"reads": sorted(set(spec.get("reads") or [])),
                                    "session_search": bool(spec.get("session_search", False))}
                             for name, spec in sorted(providers.items())}
    if not policy.valid_scope(data):
        raise ValueError(f"invalid read scope: {data}")
    return data


def scope_bytes(reads, providers=None):
    return (json.dumps(scope_data(reads, providers), sort_keys=True) + "\n").encode()


def differences(hermes_home, reads=None, providers=None):
    """Files of the installed plugin that differ from this package's copy (all of them when absent), plus
    `scope.json` when `reads` is given and the installed scope differs."""
    installed = target(hermes_home)
    changed = [name for name in FILES
               if not (installed / name).is_file() or (installed / name).read_bytes() != (SOURCE / name).read_bytes()]
    if reads is not None and policy.load_scope(installed) != scope_data(reads, providers):
        changed.append(policy.SCOPE_FILE)
    return changed


def install(hermes_home, reads, providers=None):
    """Copy the plugin and its read scope into <profile home>/plugins/hermes-auto-guard, replacing the directory
    atomically. Does not enable it and does not touch the running gateway (see `activation_steps`)."""
    scope = scope_bytes(reads, providers)  # validates before anything is replaced
    destination = target(hermes_home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{PLUGIN}-", dir=destination.parent))
    for name in FILES:
        shutil.copyfile(SOURCE / name, staging / name)
        os.chmod(staging / name, 0o644)
    (staging / policy.SCOPE_FILE).write_bytes(scope)
    os.chmod(staging / policy.SCOPE_FILE, 0o644)
    os.chmod(staging, 0o755)
    retired = None
    if destination.exists():
        retired = destination.with_name(f".{PLUGIN}-old-{os.getpid()}")
        os.replace(destination, retired)
    os.replace(staging, destination)
    if retired is not None:
        shutil.rmtree(retired, ignore_errors=True)
    return destination


def activation_steps(profile, hermes_home):
    flag = "" if profile == "default" else f"-p {profile} "
    return (f"Installed {target(hermes_home)}.\n"
            f"Enable it in this bot's profile only (never in Dolores' default profile): "
            f"`hermes {flag}config get plugins.enabled`, then\n"
            f"  hermes {flag}config set plugins.enabled '[<existing names>, hermes-auto-guard]'\n"
            "A profile the gateway has not served a session yet loads it on first use; otherwise `hermes-auto "
            f"guard-reload {profile}` (the gateway's reload-plugins for that profile only; other profiles' "
            "conversations keep running). `hermes-auto doctor` shows the bot ready once it is active.")
