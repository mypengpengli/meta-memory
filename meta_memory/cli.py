"""Compact public command line for Meta Memory."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .backup import backup_app, restore_app
from .config import AppConfig, load_config, save_config, slug
from .dream import run_dream
from .importer import import_file
from .legacy import bootstrap
from .maintenance import maintain, status
from .project_detection import bind_project, resolve_project
from .runtime import after, before, correct, history, origin_agent_id, read_text, remember, search
from .scheduler import install_schedule, schedule_install, schedule_remove, schedule_run, schedule_status
from .skill_installer import install_agents


AGENTS = ["claude-code", "codex", "openclaw", "custom", "all"]


def _emit(value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    try:
        print(text)
    except UnicodeEncodeError:
        # Older Windows consoles can inherit a non-UTF-8 code page.  A
        # successful command must still return valid JSON rather than fail
        # while displaying a recalled Chinese or other Unicode value.
        print(json.dumps(value, ensure_ascii=True, indent=2, default=str))


def _yn(value: str) -> bool:
    return value.strip().casefold() in {"y", "yes", "true", "1", "是"}


def _ask(prompt: str, default: str) -> str:
    if not sys.stdin.isatty():
        return default
    value = input(f"{prompt} [{default}]: ").strip()
    return value or default


def _setup(config: AppConfig, args: argparse.Namespace) -> dict[str, Any]:
    interactive = not args.non_interactive and sys.stdin.isatty()
    name = args.name or (_ask("你的名称", config.user_name) if interactive else config.user_name)
    store = args.store or (_ask("记忆保存位置", str(config.store)) if interactive else str(config.store))
    maintenance = args.maintenance or (_ask("安装每 5 分钟自动整理？(yes/no)", "yes" if config.maintenance_enabled else "no") if interactive else ("yes" if config.maintenance_enabled else "no"))
    dream_enabled = args.dream or (_ask("安装夜间 Dream？(yes/no)", "yes" if config.dream_enabled else "no") if interactive else ("yes" if config.dream_enabled else "no"))
    agents = args.agents or ([] if not interactive else [item.strip() for item in _ask("接入 Agent（codex,claude-code,openclaw，逗号分隔）", "").split(",") if item.strip()])
    config.user_name = name
    config.user_id = slug(name, "user")
    config.store = Path(store).expanduser()
    config.maintenance_enabled = _yn(maintenance)
    config.dream_enabled = _yn(dream_enabled)
    save_config(config)
    from .legacy import bootstrap
    bootstrap()
    from _common import ensure_store_ready
    from doctor import doctor
    initialized = ensure_store_ready(config.store)
    installed = install_agents(
        agents,
        config=config,
        custom_skill_dir=args.skill_dir,
        custom_agent_id=args.agent_id,
        custom_host_file=args.host_file,
        no_host_file=args.no_host_file,
    ) if agents else []
    scheduling = install_schedule(config) if (config.maintenance_enabled or config.dream_enabled) and not args.no_schedule else {"status": "skipped", "reason": "not selected"}
    return {"status": "ok", "config": str(config.path), "store": initialized, "agents": installed, "schedule": scheduling, "doctor": doctor(config.store)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meta-memory", description="Shared local long-term memory for AI agents.")
    parser.add_argument("--config", help="Configuration path; defaults to ~/.meta-memory/config.toml")
    parser.add_argument("--agent-id", default="", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup", help="Initialize a store and optionally install tasks and Agent skills")
    setup.add_argument("--name"); setup.add_argument("--store"); setup.add_argument("--maintenance", choices=["yes", "no"]); setup.add_argument("--dream", choices=["yes", "no"])
    setup.add_argument("--agents", nargs="+", choices=AGENTS); setup.add_argument("--skill-dir"); setup.add_argument("--agent-id"); setup.add_argument("--no-schedule", action="store_true"); setup.add_argument("--non-interactive", action="store_true")
    setup_host = setup.add_mutually_exclusive_group(); setup_host.add_argument("--host-file"); setup_host.add_argument("--no-host-file", action="store_true")

    install = commands.add_parser("install-agent", help="Install the Meta Memory Skill for one Agent")
    install.add_argument("agent", nargs="?", choices=AGENTS); install.add_argument("--all", action="store_true"); install.add_argument("--skill-dir"); install.add_argument("--agent-id")
    install_host = install.add_mutually_exclusive_group(); install_host.add_argument("--host-file"); install_host.add_argument("--no-host-file", action="store_true")

    project = commands.add_parser("project", help="Bind the current directory to a project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_set = project_commands.add_parser("set", help="Bind this directory to a project name")
    project_set.add_argument("name"); project_set.add_argument("--cwd")

    before_cmd = commands.add_parser("before", help="Load bounded relevant context before answering")
    before_cmd.add_argument("--project", default="auto"); before_cmd.add_argument("--session", default="auto"); before_cmd.add_argument("--turn", default=""); before_cmd.add_argument("--query"); before_cmd.add_argument("--query-file"); before_cmd.add_argument("--cwd")

    after_cmd = commands.add_parser("after", help="Finish a durable turn with the assistant draft before sending it")
    after_cmd.add_argument("--turn", default=""); after_cmd.add_argument("--project", default="auto"); after_cmd.add_argument("--session", default=""); after_cmd.add_argument("--user"); after_cmd.add_argument("--user-file"); after_cmd.add_argument("--assistant"); after_cmd.add_argument("--assistant-file"); after_cmd.add_argument("--cwd")

    remember_cmd = commands.add_parser("remember", help="Explicitly save a sourced memory")
    remember_cmd.add_argument("--project", default="auto"); remember_cmd.add_argument("--session", default="auto"); remember_cmd.add_argument("--scope", choices=["auto", "user", "project"], default="auto"); remember_cmd.add_argument("--source-kind", choices=["user", "agent-observation", "tool-result", "resource"], default="user", help=argparse.SUPPRESS); remember_cmd.add_argument("--source-ref", default="", help=argparse.SUPPRESS); remember_cmd.add_argument("--title", default=""); remember_cmd.add_argument("--content"); remember_cmd.add_argument("--content-file"); remember_cmd.add_argument("--cwd")

    correct_cmd = commands.add_parser("correct", help="Correct a claim immediately while preserving its history")
    correct_cmd.add_argument("--memory", required=True); correct_cmd.add_argument("--content"); correct_cmd.add_argument("--content-file")

    search_cmd = commands.add_parser("search", help="Search current user/project memory")
    search_cmd.add_argument("query"); search_cmd.add_argument("--project", default="auto"); search_cmd.add_argument("--limit", type=int); search_cmd.add_argument("--cwd")
    history_cmd = commands.add_parser("history", help="Search prior session messages")
    history_cmd.add_argument("query"); history_cmd.add_argument("--project", default="auto"); history_cmd.add_argument("--cwd")

    session_cmd = commands.add_parser("session", help="Inspect, rotate, or close the local automatic session")
    session_commands = session_cmd.add_subparsers(dest="session_command", required=True)
    for name, help_text in [("new", "Rotate a locally derived automatic session"), ("current", "Show the current automatic session"), ("close", "Close the current automatic session")]:
        child = session_commands.add_parser(name, help=help_text)
        child.add_argument("--project", default="auto"); child.add_argument("--session", default="auto"); child.add_argument("--cwd")

    commands.add_parser("status", help="Show local store status")
    commands.add_parser("doctor", help="Run a non-mutating health check")
    maintenance = commands.add_parser("maintain", help="Run the single periodic maintenance cycle")
    maintenance.add_argument("--max-jobs", type=int, default=20)

    schedule = commands.add_parser("schedule", help="Install, inspect, remove, or run local maintenance schedules")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_commands.add_parser("install", help="Install enabled platform-native schedules")
    schedule_commands.add_parser("status", help="Show installed scheduler state")
    schedule_commands.add_parser("remove", help="Remove Meta Memory schedules without touching other jobs")
    schedule_commands.add_parser("run-maintain", help="Run maintain through the scheduler launcher now")
    schedule_commands.add_parser("run-dream", help="Run Dream through the scheduler launcher now")
    dream = commands.add_parser("dream", help="Create a safe nightly inferred summary")
    dream.add_argument("--scan-days", type=int)

    backup = commands.add_parser("backup", help="Create a consistent ZIP backup")
    backup.add_argument("--output")
    restore = commands.add_parser("restore", help="Restore a portable backup and update the local configuration")
    restore.add_argument("archive"); restore.add_argument("--destination"); restore.add_argument("--force", action="store_true")
    imported = commands.add_parser("import", help="Import a local file as source evidence")
    imported.add_argument("file"); imported.add_argument("--project", default="auto"); imported.add_argument("--cwd")
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    if args.command == "setup": return _setup(config, args)
    if args.command == "install-agent":
        agents = ["all"] if args.all else ([args.agent] if args.agent else [])
        if not agents:
            raise ValueError("Provide an agent name or --all.")
        return {
            "status": "ok",
            "installed": install_agents(
                agents,
                config=config,
                custom_skill_dir=args.skill_dir,
                custom_agent_id=args.agent_id,
                custom_host_file=args.host_file,
                no_host_file=args.no_host_file,
            ),
        }
    if args.command == "project":
        context = bind_project(config, args.name, args.cwd); save_config(config)
        return {"status": "ok", "project": context.project_id, "root": str(context.root), "config": str(config.path)}
    if args.command == "before": return before(config, query=read_text(args.query, args.query_file), session=args.session, project_name=args.project, start=args.cwd, agent_id=args.agent_id, turn_uid=args.turn)
    if args.command == "after": return after(config, turn_uid=args.turn, user_text=read_text(args.user, args.user_file), assistant_text=read_text(args.assistant, args.assistant_file), session=args.session, project_name=args.project, start=args.cwd, agent_id=origin_agent_id(args.agent_id))
    if args.command == "remember": return remember(config, content=read_text(args.content, args.content_file), title=args.title, session=args.session, project_name=args.project, start=args.cwd, agent_id=args.agent_id, scope=args.scope, source_kind=args.source_kind, source_ref=args.source_ref)
    if args.command == "correct": return correct(config, memory_id=args.memory, content=read_text(args.content, args.content_file), agent_id=args.agent_id)
    if args.command == "search": return search(config, query=args.query, project_name=args.project, start=args.cwd, limit=args.limit, agent_id=args.agent_id)
    if args.command == "history": return history(config, query=args.query, project_name=args.project, start=args.cwd, agent_id=args.agent_id)
    if args.command == "session":
        from .session_manager import close_session as close_cached_session, new_session, resolve_session
        project = resolve_project(config, args.project, args.cwd)
        agent = origin_agent_id(args.agent_id)
        if args.session_command == "new":
            value = new_session(config, requested=args.session, agent_id=agent, project=project)
        elif args.session_command == "current":
            value = resolve_session(config, requested=args.session, agent_id=agent, project=project)
        else:
            value = close_cached_session(config, requested=args.session, agent_id=agent, project=project)
            if value and value.session_id:
                bootstrap()
                from session_archive import close_session as close_archived_session
                close_archived_session(config.store, value.session_id, subject_id=config.subject_id, profile_id=config.profile_id, workspace_id=project.workspace_id, origin_agent_id=agent)
        return {"status": "ok", "project": project.project_id, "agent_id": agent, "session": None if value is None else {"id": value.session_id, "source": value.source, "reused": value.reused}}
    if args.command == "status": return status(config)
    if args.command == "doctor": return status(config)["health"]
    if args.command == "maintain": return maintain(config, max_jobs=args.max_jobs)
    if args.command == "schedule":
        if args.schedule_command == "install": return schedule_install(config)
        if args.schedule_command == "status": return schedule_status(config)
        if args.schedule_command == "remove": return schedule_remove(config)
        return schedule_run(config, "maintain" if args.schedule_command == "run-maintain" else "dream")
    if args.command == "dream": return run_dream(config, scan_days=args.scan_days)
    if args.command == "backup": return backup_app(config, args.output)
    if args.command == "restore": return restore_app(config, args.archive, args.destination, force=args.force)
    if args.command == "import": return import_file(config, file_path=args.file, project_name=args.project, start=args.cwd, agent_id=origin_agent_id(args.agent_id))
    raise ValueError(f"Unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> None:
    try:
        result = dispatch(build_parser().parse_args(argv))
    except (OSError, ValueError, RuntimeError) as exc:
        _emit({"status": "error", "error": str(exc)})
        raise SystemExit(2) from exc
    _emit(result)


if __name__ == "__main__":
    main()
