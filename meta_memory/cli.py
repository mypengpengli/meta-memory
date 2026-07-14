"""Compact public command line for Meta Memory."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .backup import backup_store, restore_store
from .config import AppConfig, load_config, save_config, slug
from .dream import run_dream
from .importer import import_file
from .maintenance import maintain, status
from .project_detection import bind_project
from .runtime import after, before, correct, history, read_text, remember, search
from .scheduler import install_schedule
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
    installed = install_agents(agents, custom_skill_dir=args.skill_dir) if agents else []
    scheduling = install_schedule(config) if (config.maintenance_enabled or config.dream_enabled) and not args.no_schedule else {"status": "skipped", "reason": "not selected"}
    return {"status": "ok", "config": str(config.path), "store": initialized, "agents": installed, "schedule": scheduling, "doctor": doctor(config.store)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meta-memory", description="Shared local long-term memory for AI agents.")
    parser.add_argument("--config", help="Configuration path; defaults to ~/.meta-memory/config.toml")
    parser.add_argument("--agent-id", default="", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup", help="Initialize a store and optionally install tasks and Agent skills")
    setup.add_argument("--name"); setup.add_argument("--store"); setup.add_argument("--maintenance", choices=["yes", "no"]); setup.add_argument("--dream", choices=["yes", "no"])
    setup.add_argument("--agents", nargs="+", choices=AGENTS); setup.add_argument("--skill-dir"); setup.add_argument("--no-schedule", action="store_true"); setup.add_argument("--non-interactive", action="store_true")

    install = commands.add_parser("install-agent", help="Install the Meta Memory Skill for one Agent")
    install.add_argument("agent", choices=AGENTS); install.add_argument("--skill-dir")

    project = commands.add_parser("project", help="Bind the current directory to a project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_set = project_commands.add_parser("set", help="Bind this directory to a project name")
    project_set.add_argument("name"); project_set.add_argument("--cwd")

    before_cmd = commands.add_parser("before", help="Load bounded relevant context before answering")
    before_cmd.add_argument("--project", default="auto"); before_cmd.add_argument("--session", default="auto"); before_cmd.add_argument("--turn", default=""); before_cmd.add_argument("--query"); before_cmd.add_argument("--query-file"); before_cmd.add_argument("--cwd")

    after_cmd = commands.add_parser("after", help="Save one user/assistant turn after answering")
    after_cmd.add_argument("--turn", default=""); after_cmd.add_argument("--project", default="auto"); after_cmd.add_argument("--session", default=""); after_cmd.add_argument("--user"); after_cmd.add_argument("--user-file"); after_cmd.add_argument("--assistant"); after_cmd.add_argument("--assistant-file"); after_cmd.add_argument("--cwd")

    remember_cmd = commands.add_parser("remember", help="Explicitly save a sourced memory")
    remember_cmd.add_argument("--project", default="auto"); remember_cmd.add_argument("--session", default=""); remember_cmd.add_argument("--title", default=""); remember_cmd.add_argument("--content"); remember_cmd.add_argument("--content-file"); remember_cmd.add_argument("--cwd")

    correct_cmd = commands.add_parser("correct", help="Report a memory as incorrect and stage a reviewed correction")
    correct_cmd.add_argument("--memory", required=True); correct_cmd.add_argument("--content"); correct_cmd.add_argument("--content-file")

    search_cmd = commands.add_parser("search", help="Search current user/project memory")
    search_cmd.add_argument("query"); search_cmd.add_argument("--project", default="auto"); search_cmd.add_argument("--limit", type=int); search_cmd.add_argument("--cwd")
    history_cmd = commands.add_parser("history", help="Search prior session messages")
    history_cmd.add_argument("query"); history_cmd.add_argument("--project", default="auto"); history_cmd.add_argument("--cwd")

    commands.add_parser("status", help="Show local store status")
    commands.add_parser("doctor", help="Run a non-mutating health check")
    maintenance = commands.add_parser("maintain", help="Run the single periodic maintenance cycle")
    maintenance.add_argument("--max-jobs", type=int, default=20)
    dream = commands.add_parser("dream", help="Create a safe nightly inferred summary")
    dream.add_argument("--scan-days", type=int)

    backup = commands.add_parser("backup", help="Create a consistent ZIP backup")
    backup.add_argument("--output")
    restore = commands.add_parser("restore", help="Restore a backup into the configured store")
    restore.add_argument("archive"); restore.add_argument("--destination"); restore.add_argument("--force", action="store_true")
    imported = commands.add_parser("import", help="Import a local file as source evidence")
    imported.add_argument("file"); imported.add_argument("--project", default="auto"); imported.add_argument("--session", default=""); imported.add_argument("--cwd")
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    if args.command == "setup": return _setup(config, args)
    if args.command == "install-agent": return {"status": "ok", "installed": install_agents([args.agent], custom_skill_dir=args.skill_dir)}
    if args.command == "project":
        context = bind_project(config, args.name, args.cwd); save_config(config)
        return {"status": "ok", "project": context.project_id, "root": str(context.root), "config": str(config.path)}
    if args.command == "before": return before(config, query=read_text(args.query, args.query_file), session=args.session, project_name=args.project, start=args.cwd, agent_id=args.agent_id, turn_uid=args.turn)
    if args.command == "after": return after(config, turn_uid=args.turn, user_text=read_text(args.user, args.user_file), assistant_text=read_text(args.assistant, args.assistant_file), session=args.session, project_name=args.project, start=args.cwd, agent_id=args.agent_id)
    if args.command == "remember": return remember(config, content=read_text(args.content, args.content_file), title=args.title, session=args.session, project_name=args.project, start=args.cwd)
    if args.command == "correct": return correct(config, memory_id=args.memory, content=read_text(args.content, args.content_file))
    if args.command == "search": return search(config, query=args.query, project_name=args.project, start=args.cwd, limit=args.limit)
    if args.command == "history": return history(config, query=args.query, project_name=args.project, start=args.cwd)
    if args.command == "status": return status(config)
    if args.command == "doctor": return status(config)["health"]
    if args.command == "maintain": return maintain(config, max_jobs=args.max_jobs)
    if args.command == "dream": return run_dream(config, scan_days=args.scan_days)
    if args.command == "backup": return backup_store(config.store, args.output)
    if args.command == "restore": return restore_store(args.archive, args.destination or config.store, force=args.force)
    if args.command == "import": return import_file(config, file_path=args.file, project_name=args.project, session=args.session, start=args.cwd)
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
