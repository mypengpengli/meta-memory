"""Exercise the installed console entry from outside the source checkout.

This script intentionally uses only the standard library.  CI runs it after
``pip install .``; every Meta Memory process starts with a temporary working
directory and an isolated config, so imports cannot fall back to repository
files that were omitted from the wheel.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, object]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {command}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command did not return JSON: {command}\n{completed.stdout}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"command returned non-object JSON: {command}")
    return payload


def check_packaged_runtime_resources(*, cwd: Path, env: dict[str, str]) -> None:
    code = (
        "from pathlib import Path; import scripts; "
        "root=Path(scripts.__file__).resolve().parent/'resources'; "
        "required={'classify_memory.md','extract_memory_units.md','default.yaml'}; "
        "missing=sorted(name for name in required if not (root/name).is_file()); "
        "assert not missing, f'missing packaged runtime resources: {missing}'; "
        "import meta_memory; templates=Path(meta_memory.__file__).resolve().parent/'templates'; "
        "assert (templates/'remote-skill.md.template').is_file(), 'missing remote Skill template'"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            "installed runtime resources are incomplete\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


def launcher_command(launcher: str, *arguments: str) -> list[str]:
    if os.name != "nt":
        return [launcher, *arguments]
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise RuntimeError("PowerShell is required for the Windows launcher smoke test")

    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    script = "& " + " ".join(quote(item) for item in (launcher, *arguments))
    return [powershell, "-NoProfile", "-NonInteractive", "-Command", script]


def main() -> None:
    executable = shutil.which("meta-memory")
    if not executable:
        raise RuntimeError("the installed meta-memory console entry is not on PATH")
    with tempfile.TemporaryDirectory(prefix="meta-memory-installed-") as temporary:
        root = Path(temporary)
        setup_cwd = root / "setup-cwd"
        agent_cwd = root / "agent-cwd"
        setup_cwd.mkdir()
        agent_cwd.mkdir()
        config = root / "config.toml"
        skill_root = root / "skill-host"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("META_MEMORY_CONFIG", None)
        base = [executable, "--config", str(config), "--json"]
        check_packaged_runtime_resources(cwd=root, env=env)

        setup = run(
            [
                *base, "setup", "--name", "Installed Smoke", "--store", "relative-store",
                "--maintenance", "no", "--dream", "no", "--agents", "custom",
                "--skill-dir", str(skill_root), "--agent-id", "smoke-agent",
                "--no-host-file", "--no-schedule", "--non-interactive",
            ],
            cwd=setup_cwd,
            env=env,
        )
        agents = setup.get("agents")
        if not isinstance(agents, list) or not agents:
            raise RuntimeError(f"setup did not install the custom Agent: {setup}")
        installed = agents[0]
        if not isinstance(installed, dict) or not bool(installed.get("launcher_verified")):
            raise RuntimeError(f"custom launcher was not verified: {installed}")
        launcher = str(installed.get("launcher") or "")
        if not launcher:
            raise RuntimeError("setup did not return a launcher path")
        expected_store = str((setup_cwd / "relative-store").resolve())
        if str(installed.get("shared_store") or "") != expected_store:
            raise RuntimeError(f"relative store was not pinned at setup: {installed}")

        remote_root = root / "remote-skill-host"
        remote = run(
            [
                *base,
                "install-remote-agent",
                "--agent-id", "remote-smoke",
                "--skill-dir", str(remote_root),
                "--server-url", "http://127.0.0.1:9",
                "--workspace-id", "stable-remote-workspace",
                "--subject-id", "person:installed-smoke",
                "--token-env", "META_MEMORY_INSTALLED_REMOTE_TOKEN",
            ],
            cwd=agent_cwd,
            env=env,
        )
        remote_config = Path(str(remote.get("config") or ""))
        remote_skill = Path(str(remote.get("skill") or ""))
        if not remote_config.is_file() or not remote_skill.is_file():
            raise RuntimeError(f"installed package did not generate the remote Skill: {remote}")
        config_text = remote_config.read_text(encoding="utf-8")
        if "META_MEMORY_INSTALLED_REMOTE_TOKEN" not in config_text or "bearer_token" in config_text:
            raise RuntimeError("remote config did not preserve the token-env-only contract")

        agents_file = root / "agents.json"
        env["META_MEMORY_INSTALLED_SERVER_TOKEN"] = "installed-server-secret"
        server_binding = run(
            [
                *base,
                "init-agents-file",
                "--output", str(agents_file),
                "--agent-id", "remote-smoke",
                "--workspace-id", "stable-remote-workspace",
                "--subject-id", "person:installed-smoke",
                "--token-env", "META_MEMORY_INSTALLED_SERVER_TOKEN",
            ],
            cwd=agent_cwd,
            env=env,
        )
        if server_binding.get("status") != "ok" or not agents_file.is_file():
            raise RuntimeError(f"installed package did not create an agents file: {server_binding}")
        hosted_overview = run(
            [*base, "overview", "--server", "--agents-file", str(agents_file), "--project", "hosted-smoke"],
            cwd=agent_cwd,
            env=env,
        )
        hosted_readiness = hosted_overview.get("readiness", {}).get("hosted_server", {})
        if not isinstance(hosted_readiness, dict) or hosted_readiness.get("status") != "ready":
            raise RuntimeError(f"installed hosted-server overview is not ready: {hosted_overview}")

        before = run(
            launcher_command(
                launcher, "--json", "before", "--project", "installed-smoke", "--session", "smoke-session",
                "--turn", "smoke-turn", "--query", "Remember the installed package lifecycle signal.",
            ),
            cwd=agent_cwd,
            env=env,
        )
        if before.get("turn_id") != "smoke-turn":
            raise RuntimeError(f"before did not persist the requested turn: {before}")
        after = run(
            launcher_command(
                launcher, "--json", "after", "--turn", "smoke-turn", "--assistant", "The lifecycle signal was saved.",
            ),
            cwd=agent_cwd,
            env=env,
        )
        if str(after.get("status")) not in {"ok", "queued"}:
            raise RuntimeError(f"after did not complete the turn: {after}")

        agent_status = run(
            [*base, "agent", "status", "--all", "--verbose", "--project", "installed-smoke", "--cwd", str(root)],
            cwd=root,
            env=env,
        )
        rows = agent_status.get("agents")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"agent status did not find the installed Agent: {agent_status}")
        smoke_rows = [row for row in rows if isinstance(row, dict) and row.get("agent") == "smoke-agent"]
        if len(smoke_rows) != 1:
            raise RuntimeError(f"agent status did not return the smoke Agent exactly once: {rows}")
        if str(smoke_rows[0].get("lifecycle_state") or "") != "active":
            raise RuntimeError(f"agent lifecycle was not observed as active: {smoke_rows[0]}")

        upgrade = run([*base, "agent", "upgrade-status", "--all"], cwd=root, env=env)
        if upgrade.get("status") != "ok":
            raise RuntimeError(f"fresh integration is unexpectedly stale: {upgrade}")
        overview = run(
            [*base, "overview", "--project", "installed-smoke", "--cwd", str(root)],
            cwd=root,
            env=env,
        )
        readiness = overview.get("readiness")
        agent_readiness = readiness.get("agent") if isinstance(readiness, dict) else None
        scheduler_readiness = readiness.get("scheduler") if isinstance(readiness, dict) else None
        if not isinstance(agent_readiness, dict) or agent_readiness.get("status") != "ready":
            raise RuntimeError(f"fresh Agent integration is not ready: {overview}")
        if not isinstance(scheduler_readiness, dict) or scheduler_readiness.get("status") != "disabled":
            raise RuntimeError(f"disabled fresh scheduler was misreported: {overview}")
        doctor = run([*base, "doctor"], cwd=root, env=env)
        if doctor.get("status") != "ok" or "026" not in list(doctor.get("migrations") or []):
            raise RuntimeError(f"installed migrations are incomplete: {doctor}")

        print(json.dumps({"status": "ok", "agent": "smoke-agent", "integration": "ready"}))


if __name__ == "__main__":
    main()
