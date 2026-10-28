"""Install the hermes-auto-guard Hermes plugin (shipped in `guard_plugin/`) into a Hermes home."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

PLUGIN = "hermes-auto-guard"
FILES = ("plugin.yaml", "__init__.py", "policy.py")
SOURCE = Path(__file__).with_name("guard_plugin")


def target(hermes_home):
    return Path(hermes_home).expanduser() / "plugins" / PLUGIN


def differences(hermes_home):
    """Files of the installed plugin that differ from this package's copy (all of them when absent)."""
    installed = target(hermes_home)
    return [name for name in FILES
            if not (installed / name).is_file() or (installed / name).read_bytes() != (SOURCE / name).read_bytes()]


def install(hermes_home):
    """Copy the plugin into <hermes_home>/plugins/hermes-auto-guard, replacing the directory atomically.

    Does not enable it and does not touch the running gateway: enabling is a `hermes config set`, loading
    a plugin reload of the gateway (see `activation_steps`)."""
    destination = target(hermes_home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{PLUGIN}-", dir=destination.parent))
    for name in FILES:
        shutil.copyfile(SOURCE / name, staging / name)
        os.chmod(staging / name, 0o644)
    os.chmod(staging, 0o755)
    retired = None
    if destination.exists():
        retired = destination.with_name(f".{PLUGIN}-old-{os.getpid()}")
        os.replace(destination, retired)
    os.replace(staging, destination)
    if retired is not None:
        shutil.rmtree(retired, ignore_errors=True)
    return destination


def activation_steps(hermes_home):
    return (f"Installed {target(hermes_home)}.\n"
            "Enable it without dropping the plugins already enabled (read them first with "
            "`hermes config get plugins.enabled`):\n"
            "  hermes config set plugins.enabled '[<existing names>, hermes-auto-guard]'\n"
            "Keep its toolset out of your own sessions (only hermes-auto's sessions name it):\n"
            "  hermes config set known_plugin_toolsets.cli '[<existing names>, hermes-auto]'\n"
            "Then load it into the running gateway: `hermes plugins reload` if your Hermes has it, otherwise "
            "restart the gateway (Hermes.app). `hermes-auto doctor` shows guard.ok = true once it is active; "
            "until then sessions with third-party content are refused.")
