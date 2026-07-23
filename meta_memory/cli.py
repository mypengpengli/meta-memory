"""Compact public command line for Meta Memory."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import AppConfig, load_config, save_config, slug
from .legacy import bootstrap


AGENTS = ["claude-code", "codex", "openclaw", "custom", "all"]


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep command help readable and make meaningful defaults visible."""

    def _get_help_string(self, action: argparse.Action) -> str | None:
        text = action.help
        default = action.default
        if (
            text
            and "%(default)" not in text
            and default is not None
            and default is not False
            and default != ""
            and default is not argparse.SUPPRESS
            and bool(action.option_strings)
        ):
            return f"{text} (default: %(default)s)"
        return text


def _set_help(parser: argparse.ArgumentParser, *, description: str, examples: str = "") -> None:
    parser.description = description
    parser.epilog = examples or None


def _polish_help(parser: argparse.ArgumentParser) -> None:
    """Apply the same help formatting to every nested command parser."""
    parser.formatter_class = _HelpFormatter
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            for child in {id(item): item for item in choices.values()}.values():
                _polish_help(child)


def _rename_subcommand_help(action: Any, labels: dict[str, str]) -> None:
    """Replace terse argparse labels without changing command compatibility."""
    for choice in getattr(action, "_choices_actions", []):
        command = str(getattr(choice, "dest", ""))
        if command in labels:
            choice.help = labels[command]


def _emit(value: Any, *, output_format: str = "json") -> None:
    if output_format == "auto":
        output_format = "text" if sys.stdout.isatty() else "json"
    if output_format == "text":
        from .ux_overview import human_text

        try:
            print(human_text(value))
        except UnicodeEncodeError:
            print(human_text(value).encode("ascii", "backslashreplace").decode("ascii"))
        return
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
    from .scheduler import install_schedule
    from .skill_installer import install_agents

    interactive = not args.non_interactive and sys.stdin.isatty()
    name = args.name or (_ask("你的名称", config.user_name) if interactive else config.user_name)
    store = args.store or (_ask("记忆保存位置", str(config.store)) if interactive else str(config.store))
    maintenance = args.maintenance or (_ask("安装每 10 分钟自动整理？(yes/no)", "yes" if config.maintenance_enabled else "no") if interactive else ("yes" if config.maintenance_enabled else "no"))
    dream_enabled = args.dream or (_ask("安装夜间 Dream？(yes/no)", "yes" if config.dream_enabled else "no") if interactive else ("yes" if config.dream_enabled else "no"))
    agents = args.agents or ([] if not interactive else [item.strip() for item in _ask("接入 Agent（codex,claude-code,openclaw，逗号分隔）", "").split(",") if item.strip()])
    # ``nargs='*'`` intentionally makes an empty setup selection explicit;
    # report it as an action item instead of claiming an Agent was installed.
    if args.agents is not None:
        agents = list(args.agents)
    unknown = [item for item in agents if item not in AGENTS]
    if unknown:
        raise ValueError(f"Unsupported Agent selection: {', '.join(unknown)}")
    custom_options = bool(args.skill_dir or args.agent_id or args.host_file or args.no_host_file)
    if custom_options and "custom" not in agents:
        raise ValueError("--skill-dir, --agent-id, --host-file and --no-host-file require --agents custom.")
    config.user_name = name
    config.user_id = slug(name, "user")
    # A generated launcher can run from any Agent project directory. Persist
    # one absolute path now so a relative setup argument never creates a new
    # memory store merely because the host's cwd changed.
    config.store = Path(store).expanduser().resolve()
    config.maintenance_enabled = _yn(maintenance)
    config.dream_enabled = _yn(dream_enabled)
    config.dream_heartbeat_enabled = config.maintenance_enabled
    config.dream_deep_enabled = config.dream_enabled
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
    schedule_selected = (config.maintenance_enabled or config.dream_enabled) and not args.no_schedule
    if schedule_selected:
        try:
            scheduling = install_schedule(config)
        except (OSError, RuntimeError, ValueError) as exc:
            # Configuration, store initialization, and Agent installation have
            # already succeeded.  Keep that useful work and surface scheduler
            # permissions/platform problems as a recoverable setup action.
            scheduling = {
                "status": "error",
                "error": str(exc),
                "next_action": "meta-memory schedule install",
            }
    else:
        scheduling = {"status": "skipped", "reason": "not selected"}
    no_agent = not installed
    agent_issues = [
        item for item in installed
        if str(item.get("status") or "ok") not in {"ok", "ready"}
    ]
    schedule_issue = str(scheduling.get("status") or "") == "error"
    next_action = None
    warning = None
    if no_agent:
        next_action = "meta-memory install-agent codex"
        warning = "No Agent integration was installed."
    elif agent_issues:
        first = agent_issues[0]
        next_action = str(first.get("next_action") or f"meta-memory agent verify {first.get('agent') or first.get('agent_id') or ''}").strip()
        warning = str(first.get("manual_next_step") or "One or more Agent integrations need attention.")
    elif schedule_issue:
        next_action = "meta-memory schedule install"
        warning = "The core runtime and Agent integration were installed, but background scheduling needs attention."
    return {
        "status": "needs_action" if no_agent or agent_issues or schedule_issue else "ok", "config": str(config.path), "store": initialized,
        "agents": installed, "schedule": scheduling, "doctor": doctor(config.store),
        "next_action": next_action,
        "warning": warning,
    }


def _json_file(value: str, *, default: Any) -> Any:
    if not str(value or "").strip():
        return default
    path = Path(value).expanduser().resolve()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON file is invalid: {path}: {exc}") from exc


def _local_world_identity(config: AppConfig, args: argparse.Namespace) -> tuple[str, str, str]:
    from .project_detection import resolve_project
    from .runtime import origin_agent_id

    project = resolve_project(config, getattr(args, "project", "auto"), getattr(args, "cwd", None))
    subject = str(getattr(args, "subject_id", "") or config.subject_id)
    return project.workspace_id, subject, origin_agent_id(getattr(args, "agent_id", ""))


def _dispatch_shared(config: AppConfig, args: argparse.Namespace) -> dict[str, Any]:
    from .shared_memory import (
        ensure_audience,
        ensure_channel,
        expire_time_bounded,
        grant_audience_member,
        list_activity_feed,
        list_channels,
        list_temporal_states,
        publish_activity,
        publish_temporal_state,
        revoke_audience_member,
    )

    workspace, subject, agent = _local_world_identity(config, args)
    action = args.shared_command
    if action == "init":
        audience = ensure_audience(
            config.store,
            profile_id=config.profile_id,
            audience_type=args.type,
            audience_key=args.key,
            label=args.label,
            metadata=_json_file(args.metadata_file, default={}),
            profile_wide=not args.restricted,
        )
        channel = ensure_channel(
            config.store,
            profile_id=config.profile_id,
            channel_type=args.channel_type or args.type,
            channel_key=args.channel_key or args.key,
            audience_id=str(audience["audience_id"]),
            subject_id=subject,
            workspace_id=workspace,
            owner_agent_id=agent,
            label=args.label,
        )
        members = list(args.member_agent or [])
        if args.restricted and agent not in members:
            members.append(agent)
        revoked_profile_wide = False
        if args.restricted:
            revoked_profile_wide = revoke_audience_member(
                config.store,
                profile_id=config.profile_id,
                audience_id=str(audience["audience_id"]),
                member_type="profile",
                member_id=config.profile_id,
            )
        grants = [
            grant_audience_member(
                config.store,
                profile_id=config.profile_id,
                audience_id=str(audience["audience_id"]),
                member_type="agent",
                member_id=member,
            )
            for member in members
        ]
        return {
            "status": "ok",
            "audience": audience,
            "channel": channel,
            "grants": grants,
            "profile_wide_membership_revoked": revoked_profile_wide,
        }
    if action == "grant":
        result = grant_audience_member(
            config.store,
            profile_id=config.profile_id,
            audience_id=args.audience_id,
            member_type=args.member_type,
            member_id=args.member_id,
        )
        return {"status": "ok", "grant": result}
    if action == "revoke":
        removed = revoke_audience_member(
            config.store,
            profile_id=config.profile_id,
            audience_id=args.audience_id,
            member_type=args.member_type,
            member_id=args.member_id,
        )
        return {
            "status": "ok",
            "revoked": removed,
            "audience_id": args.audience_id,
            "member_type": args.member_type,
            "member_id": args.member_id,
        }
    if action == "channels":
        return {
            "status": "ok",
            "channels": list_channels(
                config.store,
                profile_id=config.profile_id,
                audience_id=args.audience_id,
                channel_type=args.type,
                member_type=args.member_type,
                member_id=args.member_id,
            ),
        }
    if action == "publish":
        result = publish_activity(
            config.store,
            profile_id=config.profile_id,
            channel_id=args.channel_id,
            summary=args.summary,
            source_workspace_id=workspace,
            subject_id=subject,
            source_agent_id=agent,
            source_session_id=args.session_id,
            source_ref=args.source_ref,
            confidence=args.confidence,
            activity_kind=args.kind,
            title=args.title,
            payload=_json_file(args.payload_file, default={}),
            importance=args.importance,
            occurred_at=args.occurred_at or None,
            valid_until=args.valid_until or None,
            idempotency_key=args.idempotency_key,
        )
        return {"status": "ok", "activity": result}
    if action == "feed":
        return {
            "status": "ok",
            "activities": list_activity_feed(
                config.store,
                profile_id=config.profile_id,
                channel_id=args.channel_id,
                subject_id=args.subject_id,
                include_history=args.include_history,
                limit=args.limit,
            ),
        }
    if action == "state-set":
        value = _json_file(args.value_file, default={"summary": args.summary})
        result = publish_temporal_state(
            config.store,
            profile_id=config.profile_id,
            channel_id=args.channel_id,
            subject_id=subject,
            state_key=args.state_key,
            value=value,
            summary=args.summary,
            source_workspace_id=workspace,
            source_agent_id=agent,
            source_ref=args.source_ref,
            confidence=args.confidence,
            observed_at=args.observed_at or None,
            valid_from=args.valid_from or None,
            valid_until=args.valid_until or None,
            idempotency_key=args.idempotency_key,
        )
        return {"status": "ok", "state": result}
    if action == "states":
        return {
            "status": "ok",
            "states": list_temporal_states(
                config.store,
                profile_id=config.profile_id,
                channel_id=args.channel_id,
                subject_id=args.subject_id,
                state_key=args.state_key,
                current_only=not args.include_history,
                limit=args.limit,
            ),
        }
    return {"status": "ok", "expired": expire_time_bounded(config.store, profile_id=config.profile_id)}


def _dispatch_spatial(config: AppConfig, args: argparse.Namespace) -> dict[str, Any]:
    from .spatial import (
        create_map_version,
        get_asset,
        get_map,
        get_spatial_observation,
        list_assets,
        list_maps,
        list_spatial_observations,
        read_asset,
        record_spatial_observation,
        remove_asset,
        search_spatial_observations,
        store_asset,
    )

    workspace, subject, agent = _local_world_identity(config, args)
    if args.command == "asset":
        if args.asset_command == "add":
            source = Path(args.file).expanduser().resolve()
            if not source.is_file():
                raise ValueError(f"Asset file does not exist: {source}")
            with source.open("rb") as stream:
                item = store_asset(
                    config.store,
                    stream,
                    profile_id=config.profile_id,
                    media_type=args.media_type,
                    original_name=source.name,
                    metadata=_json_file(args.metadata_file, default={}),
                    max_bytes=max(1, args.max_mb) * 1024 * 1024,
                )
            return {"status": "ok", "asset": item}
        if args.asset_command == "list":
            return {"status": "ok", "assets": list_assets(config.store, profile_id=config.profile_id, media_type=args.media_type, limit=args.limit)}
        if args.asset_command == "show":
            item = get_asset(config.store, profile_id=config.profile_id, asset_id=args.asset_id)
            if not item:
                raise ValueError("Asset not found.")
            return {"status": "ok", "asset": item}
        if args.asset_command == "export":
            content = read_asset(config.store, profile_id=config.profile_id, asset_id=args.asset_id, max_bytes=max(1, args.max_mb) * 1024 * 1024)
            target = Path(args.output).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return {"status": "ok", "asset_id": args.asset_id, "output": str(target), "byte_size": len(content)}
        return {"status": "ok", "asset": remove_asset(config.store, profile_id=config.profile_id, asset_id=args.asset_id, force=args.force)}
    if args.command == "map":
        if args.map_command == "add":
            item = create_map_version(
                config.store,
                profile_id=config.profile_id,
                channel_id=args.channel_id,
                map_id=args.map_id,
                coordinate_frame=args.coordinate_frame,
                version=args.version,
                name=args.name,
                asset_id=args.asset_id,
                source_workspace_id=workspace,
                source_agent_id=agent,
                captured_at=args.captured_at or None,
                metadata=_json_file(args.metadata_file, default={}),
                idempotency_key=args.idempotency_key,
            )
            return {"status": "ok", "map": item}
        if args.map_command == "list":
            return {"status": "ok", "maps": list_maps(config.store, profile_id=config.profile_id, channel_id=args.channel_id, latest_only=not args.include_history, limit=args.limit)}
        item = get_map(config.store, profile_id=config.profile_id, map_id=args.map_id, version=args.version)
        if not item:
            raise ValueError("Map not found.")
        return {"status": "ok", "map": item}
    if args.spatial_command == "add":
        objects = _json_file(args.objects_file, default=[])
        if not isinstance(objects, list):
            raise ValueError("--objects-file must contain a JSON array")
        item = record_spatial_observation(
            config.store,
            profile_id=config.profile_id,
            channel_id=args.channel_id,
            workspace_id=workspace,
            subject_id=subject,
            source_agent_id=agent,
            map_id=args.map_id,
            map_version=args.map_version,
            asset_id=args.asset_id,
            location_id=args.location_id,
            location_text=args.location_text,
            caption=args.caption,
            ocr_text=args.ocr_text,
            objects=objects,
            confidence=args.confidence,
            observed_at=args.observed_at or None,
            valid_until=args.valid_until or None,
            visibility_scope=args.visibility_scope,
            idempotency_key=args.idempotency_key,
            metadata=_json_file(args.metadata_file, default={}),
        )
        return {"status": "ok", "observation": item}
    if args.spatial_command == "show":
        item = get_spatial_observation(config.store, profile_id=config.profile_id, observation_id=args.observation_id)
        if not item:
            raise ValueError("Spatial observation not found.")
        return {"status": "ok", "observation": item}
    if args.spatial_command == "search":
        rows = search_spatial_observations(
            config.store,
            profile_id=config.profile_id,
            query=args.query,
            channel_id=args.channel_id,
            map_id=args.map_id,
            workspace_id=workspace,
            viewer_agent_id=agent,
            current_only=not args.include_history,
            limit=args.limit,
        )
    else:
        rows = list_spatial_observations(
            config.store,
            profile_id=config.profile_id,
            channel_id=args.channel_id,
            map_id=args.map_id,
            workspace_id=workspace,
            viewer_agent_id=agent,
            current_only=not args.include_history,
            limit=args.limit,
        )
    return {"status": "ok", "observations": rows}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meta-memory",
        usage="meta-memory [GLOBAL OPTION] COMMAND [OPTION]",
        description="A local or hosted durable memory runtime for AI agents. Start locally with setup; deploy hosted service with the included Compose stack or serve.",
        epilog="""Start here:
  meta-memory setup --agents codex
  meta-memory overview

Everyday tasks:
  meta-memory remember --content "A fact worth keeping"
  meta-memory search "keyword"
  meta-memory inbox list

Discover a command:
  meta-memory COMMAND --help
  meta-memory memory --help

Remote and robot use:
  meta-memory init-agents-file --help
  meta-memory serve --help
  meta-memory install-remote-agent --help
  meta-memory shared --help
  meta-memory spatial --help

Hosted Docker deployment (from a repository checkout):
  sh docker/bootstrap.sh
  docker compose up -d --build meta-memory worker
  Guide: docs/container-deployment.md
""",
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("--config", help="Configuration file path")
    parser.add_argument("--agent-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--format", choices=["auto", "json", "text"], default="auto", help="Output format; auto is text in a terminal and JSON when piped")
    parser.add_argument("--json", dest="output_format_json", action="store_true", help="Force machine-readable JSON output.")
    from . import __version__
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True, title="Commands by task", description="Choose the command that matches what you want to do. Run `meta-memory COMMAND --help` for examples and arguments.", metavar="COMMAND")

    setup = commands.add_parser("setup", help="Initialize a store and optionally install tasks and Agent skills")
    setup.add_argument("--name", help="Name stored in the local profile")
    setup.add_argument("--store", help="Directory for the local memory store")
    setup.add_argument("--maintenance", choices=["yes", "no"], help="Enable the incremental background heartbeat")
    setup.add_argument("--dream", choices=["yes", "no"], help="Enable the daily deep Dream report")
    setup.add_argument("--agents", nargs="*", choices=AGENTS, help="Agents to connect now, for example: codex claude-code")
    setup.add_argument("--skill-dir", help="Parent Skill directory required with --agents custom")
    setup.add_argument("--agent-id", help="Stable lowercase id required with --agents custom")
    setup.add_argument("--no-schedule", action="store_true", help="Save setup without installing background tasks")
    setup.add_argument("--non-interactive", action="store_true", help="Use defaults instead of prompts")
    setup_host = setup.add_mutually_exclusive_group()
    setup_host.add_argument("--host-file", help="Host instruction file for a custom Agent")
    setup_host.add_argument("--no-host-file", action="store_true", help="Do not modify a host instruction file")

    install = commands.add_parser("install-agent", help="Install the Meta Memory Skill for one or more Agents")
    install.add_argument("agent", nargs="*", choices=AGENTS, help="Built-in Agent name, custom, or all detected built-ins")
    install.add_argument("--all", action="store_true", help="Install every detected built-in Agent")
    install.add_argument("--skill-dir", help="Parent Skill directory required by a custom Agent")
    install.add_argument("--agent-id", help="Stable lowercase id required by a custom Agent")
    install_host = install.add_mutually_exclusive_group()
    install_host.add_argument("--host-file", help="Host instruction file to update for a custom Agent")
    install_host.add_argument("--no-host-file", action="store_true", help="Generate the Skill without modifying a host instruction file")

    install_remote = commands.add_parser("install-remote-agent", help="Generate a Skill for an Agent that reaches a hosted Meta Memory server")
    install_remote.add_argument("--agent-id", required=True, help="Stable lowercase identity bound to this Agent's server token")
    install_remote.add_argument("--skill-dir", required=True, help="Parent directory where the host loads Skills")
    install_remote.add_argument("--server-url", required=True, help="HTTPS origin of the central service; HTTP is accepted only on localhost")
    install_remote.add_argument("--workspace-id", required=True, help="Stable workspace id; never derive this from the server cwd")
    install_remote.add_argument("--subject-id", required=True, help="Stable primary person or subject id")
    install_remote.add_argument("--audience-id", default="", help="Optional household/person/device audience id")
    install_remote.add_argument("--channel-id", default="", help="Optional shared activity/state/spatial channel id")
    install_remote.add_argument("--token-env", default="META_MEMORY_TOKEN", help="Environment-variable name containing the bearer token")
    install_remote.add_argument("--outbox-dir", help="Durable local retry directory; defaults below ~/.meta-memory/remote")
    install_remote.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds")

    serve_cmd = commands.add_parser("serve", help="Run the central API for remote Agents and shared memory")
    serve_cmd.add_argument("--agents-file", required=True, help="Private token-to-Agent bindings JSON")
    serve_cmd.add_argument("--store", help="Central memory-data directory; defaults to the configured store")
    serve_cmd.add_argument("--host", default="127.0.0.1", help="Bind address")
    serve_cmd.add_argument("--port", type=int, default=8765, help="Bind port")
    serve_cmd.add_argument("--max-asset-mb", type=int, default=64, help="Maximum raw asset size")
    serve_cmd.add_argument("--asset-chunk-mb", type=int, default=2, help="Resumable-upload chunk size")
    serve_cmd.add_argument("--tls-cert", help="PEM certificate for native HTTPS")
    serve_cmd.add_argument("--tls-key", help="PEM private key used with --tls-cert")
    serve_cmd.add_argument(
        "--access-log",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Write one safe JSON access-log record per request (default: enabled; env META_MEMORY_HTTP_ACCESS_LOG)",
    )
    serve_cmd.add_argument(
        "--shutdown-timeout",
        type=float,
        default=None,
        help="Seconds to drain active requests after SIGINT/SIGTERM (default: 10; env META_MEMORY_HTTP_SHUTDOWN_TIMEOUT)",
    )

    init_agents = commands.add_parser("init-agents-file", help="Create or extend the server binding file for a remote Agent")
    init_agents.add_argument("--output", required=True, help="Destination agents.json path used by `meta-memory serve`")
    init_agents.add_argument("--agent-id", required=True, help="Stable identity that must match install-remote-agent")
    init_agents.add_argument("--profile-id", default="", help="Profile from `shared init`; defaults to the configured profile")
    init_agents.add_argument("--workspace-id", action="append", required=True, help="Allowed stable workspace id; repeat to allow more")
    init_agents.add_argument("--subject-id", action="append", required=True, help="Allowed person/subject id; repeat for family members")
    init_agents.add_argument("--audience-id", action="append", default=[], help="Allowed audience or channel id; repeat as needed")
    init_agents.add_argument("--token-env", default="META_MEMORY_TOKEN", help="Environment variable holding the same token on server and Agent")
    init_agents.add_argument("--replace-agent", action="store_true", help="Replace this Agent entry while preserving all other entries")

    project = commands.add_parser("project", help="Bind the current directory to a project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_set = project_commands.add_parser("set", help="Bind this directory to a project name")
    project_set.add_argument("name"); project_set.add_argument("--cwd")
    project_current_cmd = project_commands.add_parser("current", help="Show the current resolved project")
    project_current_cmd.add_argument("--project", default="auto"); project_current_cmd.add_argument("--cwd")
    project_commands.add_parser("list", help="List configured project bindings")
    project_rename_cmd = project_commands.add_parser("rename", help="Rename a bound project and migrate its local scope")
    project_rename_cmd.add_argument("old"); project_rename_cmd.add_argument("new")
    project_unbind_cmd = project_commands.add_parser("unbind", help="Remove the current directory binding without deleting memory")
    project_unbind_cmd.add_argument("--cwd"); project_unbind_cmd.add_argument("--all", action="store_true")
    project_stats_cmd = project_commands.add_parser("stats", help="Show scoped project memory counts")
    project_stats_cmd.add_argument("--project", default="auto"); project_stats_cmd.add_argument("--cwd")

    before_cmd = commands.add_parser("before", help="Load bounded relevant context before answering")
    before_cmd.add_argument("--project", default="auto", help="Project name, or auto for the current directory")
    before_cmd.add_argument("--session", default="auto", help="Session id, or auto for a local rotating session")
    before_cmd.add_argument("--turn", default="", help="Optional caller-supplied stable turn id")
    before_query = before_cmd.add_mutually_exclusive_group(required=True)
    before_query.add_argument("--query", help="User request text")
    before_query.add_argument("--query-file", help="UTF-8 file containing the exact user request")
    before_cmd.add_argument("--cwd", help="Directory used to resolve an automatic project")

    after_cmd = commands.add_parser("after", help="Finish a durable turn with the assistant draft before sending it")
    after_cmd.add_argument("--turn", default="", help="Turn id returned by before (recommended)")
    after_cmd.add_argument("--project", default="auto", help="Project name used only by the legacy after form")
    after_cmd.add_argument("--session", default="", help="Session id used only by the legacy after form")
    after_user = after_cmd.add_mutually_exclusive_group()
    after_user.add_argument("--user", help="Legacy inline user request")
    after_user.add_argument("--user-file", help="Legacy UTF-8 user-request file")
    after_answer = after_cmd.add_mutually_exclusive_group(required=True)
    after_answer.add_argument("--assistant", help="Exact final answer text")
    after_answer.add_argument("--assistant-file", help="UTF-8 file containing the exact unsent final answer")
    after_cmd.add_argument("--cwd", help="Directory used only by the legacy after form")

    remember_cmd = commands.add_parser("remember", help="Explicitly save a sourced memory")
    remember_cmd.add_argument("--project", default="auto"); remember_cmd.add_argument("--session", default="auto"); remember_cmd.add_argument("--scope", choices=["auto", "user", "project"], default="auto"); remember_cmd.add_argument("--source-kind", choices=["user", "agent-observation", "tool-result", "resource"], default="user", help=argparse.SUPPRESS); remember_cmd.add_argument("--source-ref", default="", help=argparse.SUPPRESS); remember_cmd.add_argument("--title", default=""); remember_cmd.add_argument("--content"); remember_cmd.add_argument("--content-file"); remember_cmd.add_argument("--cwd")

    correct_cmd = commands.add_parser("correct", help="Correct a claim immediately while preserving its history")
    correct_cmd.add_argument("--memory", required=True); correct_cmd.add_argument("--content"); correct_cmd.add_argument("--content-file")

    search_cmd = commands.add_parser("search", help="Search current user/project memory")
    search_cmd.add_argument("query"); search_cmd.add_argument("--project", default="auto"); search_cmd.add_argument("--limit", type=int); search_cmd.add_argument("--cwd"); search_cmd.add_argument("--claims-only", action="store_true")
    history_cmd = commands.add_parser("history", help="Search, browse, or show prior sessions")
    history_cmd.add_argument("history_args", nargs="*"); history_cmd.add_argument("--project", default="auto"); history_cmd.add_argument("--cwd"); history_cmd.add_argument("--detail", action="store_true"); history_cmd.add_argument("--last", type=int, default=8); history_cmd.add_argument("--limit", type=int, default=20)

    inbox_cmd = commands.add_parser("inbox", aliases=["review"], help="Review queued memory proposals and feedback")
    inbox_commands = inbox_cmd.add_subparsers(dest="inbox_command", required=True)
    inbox_list_cmd = inbox_commands.add_parser("list", help="List reviewable proposals")
    inbox_list_cmd.add_argument("--project", default="auto"); inbox_list_cmd.add_argument("--cwd"); inbox_list_cmd.add_argument("--status", default="pending"); inbox_list_cmd.add_argument("--kind", choices=["memory", "skill", "all"], default="memory"); inbox_list_cmd.add_argument("--limit", type=int, default=50); inbox_list_cmd.add_argument("--all-projects", action="store_true")
    inbox_show_cmd = inbox_commands.add_parser("show", help="Show one proposal and its planned change")
    inbox_show_cmd.add_argument("proposal_id"); inbox_show_cmd.add_argument("--kind", choices=["memory", "skill"], default="memory")
    inbox_approve_cmd = inbox_commands.add_parser("approve", help="Approve and apply a memory proposal")
    inbox_approve_cmd.add_argument("proposal_id"); inbox_approve_cmd.add_argument("--kind", choices=["memory", "skill"], default="memory")
    inbox_reject_cmd = inbox_commands.add_parser("reject", help="Reject a pending proposal")
    inbox_reject_cmd.add_argument("proposal_id"); inbox_reject_cmd.add_argument("--kind", choices=["memory", "skill"], default="memory"); inbox_reject_cmd.add_argument("--note", default="")
    inbox_feedback_cmd = inbox_commands.add_parser("feedback", help="Record usefulness or correction feedback for a Claim")
    inbox_feedback_cmd.add_argument("--memory", required=True); inbox_feedback_cmd.add_argument("--type", required=True, choices=["used", "helpful", "user_confirmed", "unhelpful", "outdated", "incorrect", "irrelevant"]); inbox_feedback_cmd.add_argument("--note", default=""); inbox_feedback_cmd.add_argument("--retrieval-id", default="")

    memory_cmd = commands.add_parser("memory", help="Inspect and manage long-term Claims")
    memory_commands = memory_cmd.add_subparsers(dest="memory_command", required=True)
    memory_list_cmd = memory_commands.add_parser("list", help="List Claims in the selected project")
    memory_list_cmd.add_argument("--project", default="auto"); memory_list_cmd.add_argument("--cwd"); memory_list_cmd.add_argument("--limit", type=int, default=50); memory_list_cmd.add_argument("--status", default=""); memory_list_cmd.add_argument("--kind", default=""); memory_list_cmd.add_argument("--all-projects", action="store_true")
    memory_recent_cmd = memory_commands.add_parser("recent", help="List recently updated active Claims")
    memory_recent_cmd.add_argument("--project", default="auto"); memory_recent_cmd.add_argument("--cwd"); memory_recent_cmd.add_argument("--limit", type=int, default=20); memory_recent_cmd.add_argument("--all-projects", action="store_true")
    memory_show_cmd = memory_commands.add_parser("show", help="Show a Claim, sources, versions, and feedback")
    memory_show_cmd.add_argument("memory_id"); memory_show_cmd.add_argument("--project", default="auto"); memory_show_cmd.add_argument("--cwd"); memory_show_cmd.add_argument("--all-projects", action="store_true")
    memory_search_cmd = memory_commands.add_parser("search", help="Search memory evidence")
    memory_search_cmd.add_argument("query"); memory_search_cmd.add_argument("--project", default="auto"); memory_search_cmd.add_argument("--cwd"); memory_search_cmd.add_argument("--limit", type=int); memory_search_cmd.add_argument("--claims-only", action="store_true")
    memory_correct_cmd = memory_commands.add_parser("correct", help="Correct a Claim while preserving its evidence")
    memory_correct_cmd.add_argument("memory_id"); memory_correct_cmd.add_argument("--content"); memory_correct_cmd.add_argument("--content-file")
    memory_feedback_cmd = memory_commands.add_parser("feedback", help="Record useful, outdated, or incorrect Claim feedback")
    memory_feedback_cmd.add_argument("memory_id"); memory_feedback_cmd.add_argument("--type", required=True, choices=["used", "helpful", "user_confirmed", "unhelpful", "outdated", "incorrect", "irrelevant"]); memory_feedback_cmd.add_argument("--note", default=""); memory_feedback_cmd.add_argument("--retrieval-id", default="")
    for action, help_text in [("archive", "Stop recalling a Claim but retain its history"), ("forget", "Remove a Claim from active and derived memory")]:
        child = memory_commands.add_parser(action, help=help_text)
        child.add_argument("memory_id"); child.add_argument("--project", default="auto"); child.add_argument("--cwd"); child.add_argument("--all-projects", action="store_true")
    memory_export_cmd = memory_commands.add_parser("export", help="Export Claims as JSON or Markdown")
    memory_export_cmd.add_argument("--project", default="auto"); memory_export_cmd.add_argument("--cwd"); memory_export_cmd.add_argument("--output"); memory_export_cmd.add_argument("--format", choices=["json", "markdown"], default="json"); memory_export_cmd.add_argument("--status", default=""); memory_export_cmd.add_argument("--all-projects", action="store_true")

    session_cmd = commands.add_parser("session", help="Inspect, rotate, or close the local automatic session")
    session_commands = session_cmd.add_subparsers(dest="session_command", required=True)
    for name, help_text in [("new", "Rotate a locally derived automatic session"), ("current", "Show the current automatic session"), ("close", "Close the current automatic session")]:
        child = session_commands.add_parser(name, help=help_text)
        child.add_argument("--project", default="auto"); child.add_argument("--session", default="auto"); child.add_argument("--cwd")

    overview_cmd = commands.add_parser("overview", help="Show one-screen readiness and next action")
    overview_cmd.add_argument("--project", default="auto", help="Project name, or auto for the current directory")
    overview_cmd.add_argument("--cwd", help="Directory used to resolve the project")
    overview_cmd.add_argument("--server", action="store_true", help="Evaluate hosted-server readiness instead of local Agent installation")
    overview_cmd.add_argument("--agents-file", default="", help="With --server, validate this token-to-Agent bindings file")
    status_cmd = commands.add_parser("status", help="Show local store status plus an operational overview")
    status_cmd.add_argument("--project", default="auto", help="Project name, or auto for the current directory")
    status_cmd.add_argument("--cwd", help="Directory used to resolve the project")
    commands.add_parser("doctor", help="Run a non-mutating health check")
    agent_cmd = commands.add_parser("agent", help="Inspect or verify a local Agent integration")
    agent_commands = agent_cmd.add_subparsers(dest="agent_command", required=True)
    agent_status_cmd = agent_commands.add_parser("status", help="Show current Agent runtime status")
    agent_status_cmd.add_argument("--all", action="store_true", help="Show all installed, detected, or runtime-observed Agents")
    agent_status_cmd.add_argument("--project", default="auto", help="Project whose lifecycle activity should be checked")
    agent_status_cmd.add_argument("--cwd", help="Directory used to resolve an automatic project")
    agent_status_cmd.add_argument("--verbose", action="store_true", help="Include paths, verification scope, and runtime details")
    agent_verify_cmd = agent_commands.add_parser("verify", help="Verify one installed Agent launcher and shared runtime")
    agent_verify_cmd.add_argument("agent_id", help="Installed Agent id")
    agent_verify_cmd.add_argument("--project", default="auto", help="Project whose lifecycle activity should be reported")
    agent_verify_cmd.add_argument("--cwd", help="Directory used to resolve an automatic project")
    agent_sync_cmd = agent_commands.add_parser("sync", help="Regenerate installed Agent integrations from the current contract")
    agent_sync_cmd.add_argument("agents", nargs="*", help="Installed Agent ids to refresh")
    agent_sync_cmd.add_argument("--all", action="store_true", help="Refresh every registered Agent")
    agent_sync_cmd.add_argument("--no-verify", action="store_true", help="Regenerate files without probing launchers")
    agent_repair_cmd = agent_commands.add_parser("repair", help="Alias for agent sync")
    agent_repair_cmd.add_argument("agents", nargs="*"); agent_repair_cmd.add_argument("--all", action="store_true"); agent_repair_cmd.add_argument("--no-verify", action="store_true")
    agent_uninstall_cmd = agent_commands.add_parser("uninstall", help="Remove one managed Agent integration")
    agent_uninstall_cmd.add_argument("agent_id")
    agent_upgrade_cmd = agent_commands.add_parser("upgrade-status", help="Check Skill/launcher contract and template freshness")
    agent_upgrade_cmd.add_argument("agent_id", nargs="?", help="Installed Agent id; omitted means the default selection")
    agent_upgrade_cmd.add_argument("--all", action="store_true", help="Check every registered Agent")
    config_cmd = commands.add_parser("config", help="Read or update supported local runtime configuration")
    config_commands = config_cmd.add_subparsers(dest="config_command", required=True)
    config_get = config_commands.add_parser("get", help="Read one supported configuration key"); config_get.add_argument("key")
    config_commands.add_parser("list", help="List supported public configuration keys")
    config_describe = config_commands.add_parser("describe", help="Describe one supported configuration key"); config_describe.add_argument("key")
    config_set = config_commands.add_parser("set", help="Set one supported configuration key"); config_set.add_argument("key"); config_set.add_argument("value"); config_set.add_argument("--apply", action="store_true", help="Install/refresh the scheduler even when it was not installed before")
    maintenance = commands.add_parser("maintain", help="Run the single periodic maintenance cycle")
    maintenance.add_argument("--max-jobs", type=int, default=20)

    schedule = commands.add_parser("schedule", help="Install, inspect, remove, or run local maintenance schedules")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_commands.add_parser("install", help="Install enabled platform-native schedules")
    schedule_commands.add_parser("status", help="Show installed scheduler state")
    schedule_commands.add_parser("remove", help="Remove Meta Memory schedules without touching other jobs")
    schedule_commands.add_parser("run-maintain", help="Run maintain through the scheduler launcher now")
    schedule_commands.add_parser("run-dream", help="Run Dream through the scheduler launcher now")
    dream = commands.add_parser("dream", help="Run Dream heartbeat, deep synthesis, or inspect Dream state")
    dream.add_argument("--scan-days", type=int)
    dream_commands = dream.add_subparsers(dest="dream_command")
    dream_commands.add_parser("heartbeat", help="Run the lightweight incremental memory heartbeat now")
    dream_deep_cmd = dream_commands.add_parser("deep", help="Run the deep daily synthesis now")
    dream_deep_cmd.add_argument("--scan-days", dest="deep_scan_days", type=int); dream_deep_cmd.add_argument("--dry-run", action="store_true")
    dream_commands.add_parser("status", help="Show Dream heartbeat and deep-synthesis state")
    dream_list_cmd = dream_commands.add_parser("list", help="List recent deep Dream runs")
    dream_list_cmd.add_argument("--limit", type=int, default=30); dream_list_cmd.add_argument("--include-archived", action="store_true")
    dream_show_cmd = dream_commands.add_parser("show", help="Show one Dream run or latest")
    dream_show_cmd.add_argument("run_id", nargs="?", default="latest"); dream_show_cmd.add_argument("--no-content", action="store_true")
    dream_archive_cmd = dream_commands.add_parser("archive", help="Archive generated reports for one Dream run")
    dream_archive_cmd.add_argument("run_id")

    turn_cmd = commands.add_parser("turn", help="Inspect and manage durable turn lifecycle state")
    turn_commands = turn_cmd.add_subparsers(dest="turn_command", required=True)
    turn_list_cmd = turn_commands.add_parser("list", help="List recent durable turns")
    turn_list_cmd.add_argument("--unfinished", action="store_true"); turn_list_cmd.add_argument("--project", default="auto"); turn_list_cmd.add_argument("--cwd"); turn_list_cmd.add_argument("--all-agents", action="store_true"); turn_list_cmd.add_argument("--limit", type=int, default=20)
    turn_show_cmd = turn_commands.add_parser("show", help="Show turn lifecycle metadata")
    turn_show_cmd.add_argument("turn_id")
    turn_touch_cmd = turn_commands.add_parser("touch", help="Renew a long-running turn")
    turn_touch_cmd.add_argument("turn_id"); turn_touch_cmd.add_argument("--note", default="")
    turn_reopen_cmd = turn_commands.add_parser("reopen", help="Reopen an abandoned turn")
    turn_reopen_cmd.add_argument("turn_id"); turn_reopen_cmd.add_argument("--reason", default="")
    turn_complete_cmd = turn_commands.add_parser("complete", help="Complete an abandoned turn with a late final response")
    turn_complete_cmd.add_argument("turn_id"); turn_complete_cmd.add_argument("--assistant"); turn_complete_cmd.add_argument("--assistant-file")

    recovery_cmd = commands.add_parser("recovery", help="Inspect or replay deferred turn completions")
    recovery_commands = recovery_cmd.add_subparsers(dest="recovery_command", required=True)
    recovery_status_cmd = recovery_commands.add_parser("status", help="Show spool and unfinished-turn recovery state")
    recovery_status_cmd.add_argument("--project", default="auto"); recovery_status_cmd.add_argument("--cwd")
    recovery_replay_cmd = recovery_commands.add_parser("replay", help="Replay deferred completions and recover expired turns")
    recovery_replay_cmd.add_argument("--limit", type=int, default=100)

    backup = commands.add_parser("backup", help="Create a consistent ZIP backup")
    backup.add_argument("--output", help="Destination ZIP path; defaults to a timestamped file beside the configured store")
    restore = commands.add_parser("restore", help="Restore a portable backup and update the local configuration")
    restore.add_argument("archive", help="Portable ZIP created by `meta-memory backup`")
    restore.add_argument("--destination", help="New or replaceable store directory; defaults to the configured store")
    restore.add_argument("--force", action="store_true", help="Allow replacement of an existing destination after validation")
    imported = commands.add_parser("import", help="Import a local file or directory as source evidence")
    imported.add_argument("file"); imported.add_argument("--project", default="auto"); imported.add_argument("--cwd"); imported.add_argument("--recursive", action="store_true"); imported.add_argument("--changed-only", action="store_true")
    resource_cmd = commands.add_parser("resource", help="List, inspect, refresh, remove, or export imported sources")
    resource_commands = resource_cmd.add_subparsers(dest="resource_command", required=True)
    resource_list_cmd = resource_commands.add_parser("list", help="List imported source evidence")
    resource_list_cmd.add_argument("--project", default="auto"); resource_list_cmd.add_argument("--cwd"); resource_list_cmd.add_argument("--limit", type=int, default=100); resource_list_cmd.add_argument("--all-projects", action="store_true")
    resource_show_cmd = resource_commands.add_parser("show", help="Show source metadata and bounded chunks")
    resource_show_cmd.add_argument("resource_id"); resource_show_cmd.add_argument("--project", default="auto"); resource_show_cmd.add_argument("--cwd"); resource_show_cmd.add_argument("--all-projects", action="store_true"); resource_show_cmd.add_argument("--chunk-limit", type=int, default=5)
    resource_refresh_cmd = resource_commands.add_parser("refresh", help="Re-import a source from its original path")
    resource_refresh_cmd.add_argument("resource_id"); resource_refresh_cmd.add_argument("--project", default="auto"); resource_refresh_cmd.add_argument("--cwd")
    resource_remove_cmd = resource_commands.add_parser("remove", help="Remove indexed source evidence while retaining raw audit history")
    resource_remove_cmd.add_argument("resource_id"); resource_remove_cmd.add_argument("--project", default="auto"); resource_remove_cmd.add_argument("--cwd"); resource_remove_cmd.add_argument("--all-projects", action="store_true")
    resource_export_cmd = resource_commands.add_parser("export", help="Export resource metadata as JSON or Markdown")
    resource_export_cmd.add_argument("--project", default="auto"); resource_export_cmd.add_argument("--cwd"); resource_export_cmd.add_argument("--output"); resource_export_cmd.add_argument("--format", choices=["json", "markdown"], default="json"); resource_export_cmd.add_argument("--all-projects", action="store_true")

    shared_cmd = commands.add_parser("shared", help="Manage household/person/device channels, activity, and expiring state")
    shared_commands = shared_cmd.add_subparsers(dest="shared_command", required=True)
    shared_init = shared_commands.add_parser("init", help="Create one audience and its shared channel")
    world_types = ["user", "household", "person", "project", "device", "agent", "session", "event"]
    shared_init.add_argument("--type", required=True, choices=world_types, help="Audience type, usually household, person, or device")
    shared_init.add_argument("--key", required=True, help="Stable audience key such as family-home; reuse it to get the same audience")
    shared_init.add_argument("--channel-type", choices=world_types, help="Optional channel type when it differs from the audience")
    shared_init.add_argument("--channel-key", default="", help="Stable channel key; defaults to --key")
    shared_init.add_argument("--label", default="", help="Human-readable label")
    shared_init.add_argument("--restricted", action="store_true", help="Grant only listed/current Agents instead of the whole profile")
    shared_init.add_argument("--member-agent", action="append", default=[], help="Agent allowed in a restricted audience; repeat as needed")
    shared_init.add_argument("--metadata-file", default="", help="UTF-8 JSON object with optional audience metadata")
    shared_init.add_argument("--subject-id", default="", help="Optional primary subject; defaults to the configured user")
    shared_init.add_argument("--project", default="auto", help="Source project/workspace for the channel")
    shared_init.add_argument("--cwd", help="Directory used when --project is auto")
    shared_grant = shared_commands.add_parser("grant", help="Grant an Agent or subject access to an audience")
    shared_grant.add_argument("--audience-id", required=True, help="Audience id returned by shared init")
    shared_grant.add_argument("--member-type", required=True, choices=["profile", "agent", "subject"], help="Kind of member receiving access")
    shared_grant.add_argument("--member-id", required=True, help="Stable profile, Agent, or subject id")
    shared_grant.add_argument("--project", default="auto", help="Project context for the operation")
    shared_grant.add_argument("--cwd", help="Directory used when --project is auto")
    shared_revoke = shared_commands.add_parser("revoke", help="Remove one profile, Agent, or subject from an audience")
    shared_revoke.add_argument("--audience-id", required=True, help="Audience id returned by shared init")
    shared_revoke.add_argument("--member-type", required=True, choices=["profile", "agent", "subject"], help="Kind of membership to remove")
    shared_revoke.add_argument("--member-id", required=True, help="Stable profile, Agent, or subject id")
    shared_revoke.add_argument("--project", default="auto", help="Project context for the operation")
    shared_revoke.add_argument("--cwd", help="Directory used when --project is auto")
    shared_channels = shared_commands.add_parser("channels", help="List active shared channels")
    shared_channels.add_argument("--audience-id", default="", help="Limit results to one audience")
    shared_channels.add_argument("--type", default="", choices=["", *world_types], help="Limit results to one channel type")
    shared_channels.add_argument("--member-type", default="", choices=["", "profile", "agent", "subject"], help="Filter channels visible to this member kind")
    shared_channels.add_argument("--member-id", default="", help="Member id used with --member-type")
    shared_channels.add_argument("--project", default="auto", help="Project context for the operation")
    shared_channels.add_argument("--cwd", help="Directory used when --project is auto")
    shared_publish = shared_commands.add_parser("publish", help="Publish one curated cross-workspace activity")
    shared_publish.add_argument("--channel-id", required=True, help="Destination channel id returned by shared init")
    shared_publish.add_argument("--summary", required=True, help="Short curated event summary; do not paste a raw transcript")
    shared_publish.add_argument("--title", default="", help="Optional display title")
    shared_publish.add_argument("--kind", default="update", help="Stable event kind such as repair-alert or family-update")
    shared_publish.add_argument("--importance", type=float, default=0.5, help="Retrieval importance from 0 to 1")
    shared_publish.add_argument("--occurred-at", default="", help="ISO 8601 event time with offset; defaults to now")
    shared_publish.add_argument("--valid-until", default="", help="Optional ISO 8601 expiry later than occurred-at")
    shared_publish.add_argument("--source-ref", default="", help="Inspectable source/event reference")
    shared_publish.add_argument("--confidence", type=float, help="Optional source confidence from 0 to 1")
    shared_publish.add_argument("--payload-file", default="", help="UTF-8 JSON object with small structured details")
    shared_publish.add_argument("--idempotency-key", default="", help="Stable retry key; reuse only for the same event")
    shared_publish.add_argument("--session-id", default="", help="Optional source session id")
    shared_publish.add_argument("--subject-id", default="", help="Person/subject this event concerns; blank means channel-wide")
    shared_publish.add_argument("--project", default="auto", help="Source project/workspace")
    shared_publish.add_argument("--cwd", help="Directory used when --project is auto")
    shared_feed = shared_commands.add_parser("feed", help="Read a bounded shared activity feed")
    shared_feed.add_argument("--channel-id", required=True, help="Channel to read")
    shared_feed.add_argument("--subject-id", default="", help="Include channel-wide and this subject's activities")
    shared_feed.add_argument("--include-history", action="store_true", help="Include expired and superseded activities")
    shared_feed.add_argument("--limit", type=int, default=50, help="Maximum rows, bounded to 1000")
    shared_feed.add_argument("--project", default="auto", help="Project context for the request")
    shared_feed.add_argument("--cwd", help="Directory used when --project is auto")
    shared_state = shared_commands.add_parser("state-set", help="Publish a current value that supersedes older state")
    shared_state.add_argument("--channel-id", required=True, help="Destination channel id")
    shared_state.add_argument("--state-key", required=True, help="Stable key such as last_seen, device.health, or repair.status")
    shared_state.add_argument("--summary", required=True, help="Short human-readable current state")
    shared_state.add_argument("--value-file", default="", help="UTF-8 JSON value; defaults to an object containing summary")
    shared_state.add_argument("--source-ref", required=True, help="Evidence reference such as camera:event-42")
    shared_state.add_argument("--confidence", type=float, help="Optional confidence from 0 to 1")
    shared_state.add_argument("--observed-at", default="", help="ISO 8601 observation time with offset; defaults to now")
    shared_state.add_argument("--valid-from", default="", help="Optional future start; scheduled state does not replace current state early")
    shared_state.add_argument("--valid-until", default="", help="Optional ISO 8601 expiry later than valid-from")
    shared_state.add_argument("--idempotency-key", default="", help="Stable retry key for exactly this update")
    shared_state.add_argument("--subject-id", default="", help="Stable person/device subject; defaults to the configured user")
    shared_state.add_argument("--project", default="auto", help="Source project/workspace")
    shared_state.add_argument("--cwd", help="Directory used when --project is auto")
    shared_states = shared_commands.add_parser("states", help="List current or historical shared state")
    shared_states.add_argument("--channel-id", default="", help="Optional channel filter")
    shared_states.add_argument("--subject-id", default="", help="Optional stable person/device subject filter")
    shared_states.add_argument("--state-key", default="", help="Optional state key filter")
    shared_states.add_argument("--include-history", action="store_true", help="Include scheduled, expired, and superseded rows")
    shared_states.add_argument("--limit", type=int, default=50, help="Maximum rows, bounded to 1000")
    shared_states.add_argument("--project", default="auto", help="Project context for the request")
    shared_states.add_argument("--cwd", help="Directory used when --project is auto")
    shared_expire = shared_commands.add_parser("expire", help="Materialize expired activity, state, and observations now")
    shared_expire.add_argument("--project", default="auto", help="Project context for the operation")
    shared_expire.add_argument("--cwd", help="Directory used when --project is auto")

    asset_cmd = commands.add_parser("asset", help="Store, inspect, export, or remove binary image/map assets")
    asset_commands = asset_cmd.add_subparsers(dest="asset_command", required=True)
    asset_add = asset_commands.add_parser("add", help="Stream one local binary into content-addressed storage")
    asset_add.add_argument("file", help="Local image, map, point cloud, or other binary file")
    asset_add.add_argument("--media-type", default="application/octet-stream", help="MIME type such as image/jpeg")
    asset_add.add_argument("--metadata-file", default="", help="UTF-8 JSON object with capture/source metadata")
    asset_add.add_argument("--max-mb", type=int, default=64, help="Reject files larger than this many MiB")
    asset_add.add_argument("--project", default="auto", help="Source project/workspace")
    asset_add.add_argument("--cwd", help="Directory used when --project is auto")
    asset_list = asset_commands.add_parser("list", help="List stored binary assets")
    asset_list.add_argument("--media-type", default="", help="Optional exact MIME-type filter")
    asset_list.add_argument("--limit", type=int, default=50, help="Maximum rows, bounded to 1000")
    asset_list.add_argument("--project", default="auto", help="Project context for the request")
    asset_list.add_argument("--cwd", help="Directory used when --project is auto")
    asset_show = asset_commands.add_parser("show", help="Show immutable asset metadata")
    asset_show.add_argument("asset_id", help="Asset id returned by asset add")
    asset_show.add_argument("--project", default="auto", help="Project context for the request")
    asset_show.add_argument("--cwd", help="Directory used when --project is auto")
    asset_export = asset_commands.add_parser("export", help="Verify and copy raw asset bytes to a file")
    asset_export.add_argument("asset_id", help="Asset id to verify and copy")
    asset_export.add_argument("--output", required=True, help="Destination file path")
    asset_export.add_argument("--max-mb", type=int, default=64, help="Refuse to read more than this many MiB")
    asset_export.add_argument("--project", default="auto", help="Project context for the request")
    asset_export.add_argument("--cwd", help="Directory used when --project is auto")
    asset_remove = asset_commands.add_parser("remove", help="Retire one asset; referenced assets require --force")
    asset_remove.add_argument("asset_id", help="Asset id to retire")
    asset_remove.add_argument("--force", action="store_true", help="Also retire map/observation references to this asset")
    asset_remove.add_argument("--project", default="auto", help="Project context for the operation")
    asset_remove.add_argument("--cwd", help="Directory used when --project is auto")

    map_cmd = commands.add_parser("map", help="Manage stable map identities and immutable versions")
    map_commands = map_cmd.add_subparsers(dest="map_command", required=True)
    map_add = map_commands.add_parser("add", help="Create the next immutable version of a map")
    map_add.add_argument("--channel-id", required=True, help="Channel that permanently owns this stable map id")
    map_add.add_argument("--map-id", required=True, help="Stable map identity such as home-floor-1")
    map_add.add_argument("--coordinate-frame", required=True, help="Coordinate frame such as map, odom, or floor-plan-pixels")
    map_add.add_argument("--version", type=int, help="Explicit increasing version; defaults to latest + 1")
    map_add.add_argument("--name", default="", help="Human-readable version name")
    map_add.add_argument("--asset-id", default="", help="Optional raw occupancy-grid/image/point-cloud asset")
    map_add.add_argument("--captured-at", default="", help="ISO 8601 capture time with offset; defaults to now")
    map_add.add_argument("--metadata-file", default="", help="UTF-8 JSON object with topology, dimensions, or transform metadata")
    map_add.add_argument("--idempotency-key", default="", help="Stable retry key for this exact version")
    map_add.add_argument("--subject-id", default="", help="Optional source subject; defaults to configured user")
    map_add.add_argument("--project", default="auto", help="Source project/workspace")
    map_add.add_argument("--cwd", help="Directory used when --project is auto")
    map_list = map_commands.add_parser("list", help="List latest or all map versions")
    map_list.add_argument("--channel-id", default="", help="Optional owning-channel filter")
    map_list.add_argument("--include-history", action="store_true", help="Show every immutable version instead of latest only")
    map_list.add_argument("--limit", type=int, default=50, help="Maximum rows, bounded to 1000")
    map_list.add_argument("--project", default="auto", help="Project context for the request")
    map_list.add_argument("--cwd", help="Directory used when --project is auto")
    map_show = map_commands.add_parser("show", help="Show one map version or the latest")
    map_show.add_argument("map_id", help="Stable map id")
    map_show.add_argument("--version", type=int, help="Specific immutable version; defaults to latest")
    map_show.add_argument("--project", default="auto", help="Project context for the request")
    map_show.add_argument("--cwd", help="Directory used when --project is auto")

    spatial_cmd = commands.add_parser("spatial", help="Record and search semantic observations linked to maps/assets")
    spatial_commands = spatial_cmd.add_subparsers(dest="spatial_command", required=True)
    spatial_add = spatial_commands.add_parser("add", help="Record one timestamped spatial observation")
    spatial_add.add_argument("--channel-id", required=True, help="Shared channel that owns this observation")
    spatial_add.add_argument("--map-id", default="", help="Optional stable map id; latest version is used unless --map-version is set")
    spatial_add.add_argument("--map-version", type=int, help="Optional immutable map version")
    spatial_add.add_argument("--asset-id", default="", help="Optional raw image/scan asset id")
    spatial_add.add_argument("--location-id", default="", help="Stable semantic location such as room:kitchen")
    spatial_add.add_argument("--location-text", default="", help="Human-readable location description")
    spatial_add.add_argument("--caption", default="", help="Agent-produced visual/spatial description")
    spatial_add.add_argument("--ocr-text", default="", help="Agent/tool-produced OCR text; Meta Memory does not run OCR")
    spatial_add.add_argument("--objects-file", default="", help="UTF-8 JSON array of recognized objects")
    spatial_add.add_argument("--confidence", type=float, help="Optional confidence from 0 to 1")
    spatial_add.add_argument("--observed-at", default="", help="ISO 8601 observation time with offset; defaults to now")
    spatial_add.add_argument("--valid-until", default="", help="Optional expiry later than observed-at")
    spatial_add.add_argument("--visibility-scope", choices=["channel", "workspace", "agent", "global"], default="channel", help="Who may retrieve the semantic observation")
    spatial_add.add_argument("--metadata-file", default="", help="UTF-8 JSON object with transforms or sensor details")
    spatial_add.add_argument("--idempotency-key", default="", help="Stable retry key for this exact observation")
    spatial_add.add_argument("--subject-id", default="", help="Person/device concerned; blank/default may represent the shared environment")
    spatial_add.add_argument("--project", default="auto", help="Source project/workspace")
    spatial_add.add_argument("--cwd", help="Directory used when --project is auto")
    spatial_list = spatial_commands.add_parser("list", help="List current semantic observations")
    spatial_list.add_argument("--channel-id", default="", help="Optional channel filter")
    spatial_list.add_argument("--map-id", default="", help="Optional stable map filter")
    spatial_list.add_argument("--include-history", action="store_true", help="Include expired and superseded observations")
    spatial_list.add_argument("--limit", type=int, default=50, help="Maximum rows, bounded to 1000")
    spatial_list.add_argument("--project", default="auto", help="Project context for the request")
    spatial_list.add_argument("--cwd", help="Directory used when --project is auto")
    spatial_show = spatial_commands.add_parser("show", help="Show one observation with provenance and links")
    spatial_show.add_argument("observation_id", help="Observation id returned by spatial add/search")
    spatial_show.add_argument("--project", default="auto", help="Project context for the request")
    spatial_show.add_argument("--cwd", help="Directory used when --project is auto")
    spatial_search = spatial_commands.add_parser("search", help="Search captions, OCR, objects, and locations")
    spatial_search.add_argument("query", help="Whitespace-separated terms matched across location, caption, OCR, and objects")
    spatial_search.add_argument("--channel-id", default="", help="Optional channel filter")
    spatial_search.add_argument("--map-id", default="", help="Optional stable map filter")
    spatial_search.add_argument("--include-history", action="store_true", help="Include expired and superseded observations")
    spatial_search.add_argument("--limit", type=int, default=50, help="Maximum rows, bounded to 1000")
    spatial_search.add_argument("--project", default="auto", help="Project context for the request")
    spatial_search.add_argument("--cwd", help="Directory used when --project is auto")
    _rename_subcommand_help(commands, {
        "setup": "First-time setup: save config, connect Agents, and install schedules",
        "install-agent": "Connect an Agent after setup or install one explicitly",
        "install-remote-agent": "Generate a Skill for an Agent on another computer",
        "init-agents-file": "Create or extend the hosted server Agent bindings",
        "serve": "Run the central HTTP/HTTPS service for remote Agents",
        "overview": "Start here: one-screen readiness, activity, and next actions",
        "status": "Detailed machine-readable status plus the overview dashboard",
        "doctor": "Read-only health check for the local store",
        "remember": "Save one explicit fact or preference now",
        "search": "Find relevant durable memory in the current project",
        "memory": "Browse, correct, archive, forget, or export durable Claims",
        "inbox": "Review automatic memory proposals and Claim feedback",
        "history": "Find completed session summaries and continue prior work",
        "project": "See or change the project boundary used for memory",
        "import": "Index local notes or documents as source evidence",
        "resource": "Browse and refresh imported source evidence",
        "shared": "Manage household/person/device channels and expiring state",
        "asset": "Store raw images, maps, and other binary assets",
        "map": "Manage stable maps and immutable map versions",
        "spatial": "Record or search semantic map and image observations",
        "agent": "Check, repair, or refresh Agent integrations",
        "dream": "Run or inspect background memory consolidation",
        "schedule": "Install, inspect, or remove local background tasks",
        "turn": "Inspect or recover durable conversation turns",
        "recovery": "Replay deferred completions after an interruption",
        "session": "Inspect, rotate, or close a local automatic session",
        "config": "Read or update supported runtime settings",
        "maintain": "Run one periodic maintenance cycle now",
        "backup": "Create a portable, consistent backup",
        "restore": "Restore a portable backup into a local store",
        "correct": "Correct a Claim immediately while preserving history",
    })
    _rename_subcommand_help(project_commands, {
        "set": "Bind this directory to a memorable project name",
        "current": "Show the project resolved for this directory",
        "list": "List saved directory-to-project bindings",
        "rename": "Rename a project and migrate its local scope",
        "unbind": "Remove a directory binding without deleting memory",
        "stats": "Show memory and session counts for one project",
    })
    _rename_subcommand_help(memory_commands, {
        "list": "List Claims with optional status and kind filters",
        "recent": "Show recently updated active Claims",
        "show": "Show a Claim with sources, versions, and feedback",
        "search": "Search Claims and indexed evidence",
        "correct": "Correct a Claim without losing its history",
        "feedback": "Mark a Claim as helpful, outdated, or incorrect",
        "archive": "Stop recalling a Claim while retaining its history",
        "forget": "Remove a Claim from active and derived memory",
        "export": "Export Claims as JSON or Markdown",
    })
    _rename_subcommand_help(inbox_commands, {
        "list": "List pending proposals or feedback requiring a decision",
        "show": "Inspect one proposal before deciding",
        "approve": "Apply an approved memory proposal",
        "reject": "Reject a proposal with an optional note",
        "feedback": "Record whether a recalled Claim was useful or wrong",
    })
    _rename_subcommand_help(agent_commands, {
        "status": "Show Agent integration and recent runtime activity",
        "verify": "Verify one installed launcher and shared runtime",
        "sync": "Regenerate installed integrations from this version",
        "repair": "Alias for sync",
        "uninstall": "Remove one managed Agent integration",
        "upgrade-status": "Find stale Skills or launchers after an upgrade",
    })
    _rename_subcommand_help(dream_commands, {
        "heartbeat": "Run lightweight incremental consolidation now",
        "deep": "Run or preview the daily inferred synthesis",
        "status": "Show heartbeat and deep-synthesis state",
        "list": "List recent deep Dream runs",
        "show": "Show one Dream run or the latest report",
        "archive": "Archive reports from one Dream run",
    })
    _rename_subcommand_help(schedule_commands, {
        "install": "Install enabled platform-native background tasks",
        "status": "Show whether expected background tasks are installed",
        "remove": "Remove only Meta Memory background tasks",
        "run-maintain": "Run the heartbeat launcher now",
        "run-dream": "Run the deep Dream launcher now",
    })

    _set_help(setup, description="Create or save the local configuration, optionally install Agent Skills, and install enabled schedules.", examples="""Examples:
  meta-memory setup --agents codex
  meta-memory setup --name Ada --agents codex claude-code
  meta-memory setup --agents custom --agent-id my-agent --skill-dir ~/.my-agent/skills --no-host-file
  meta-memory setup --non-interactive --no-schedule
""")
    _set_help(install, description="Generate an Agent-specific Skill and launcher. Built-ins have known paths; any compatible local CLI Agent can use the custom form.", examples="""Examples:
  meta-memory install-agent codex
  meta-memory install-agent --all
  meta-memory install-agent custom --agent-id my-agent --skill-dir ~/.my-agent/skills --no-host-file
  meta-memory install-agent custom --agent-id my-agent --skill-dir ~/.my-agent/skills --host-file ~/.my-agent/AGENTS.md
""")
    _set_help(install_remote, description="Generate a non-secret Skill and launcher for an Agent on another device. The launcher pins stable identity and reads the bearer token only from an environment variable.", examples="""Example:
  meta-memory install-remote-agent --agent-id home-robot --skill-dir ~/.robot/skills --server-url https://memory.example.com --workspace-id home --subject-id person:owner --audience-id <audience-id> --channel-id <channel-id> --token-env META_MEMORY_TOKEN_ROBOT
""")
    _set_help(serve_cmd, description="Run the one authoritative SQLite service. /healthz is liveness; /readyz verifies bindings, schema, and persistent directories. Bind localhost behind a TLS reverse proxy, or supply a certificate/key for native HTTPS.", examples="""Examples:
  meta-memory serve --agents-file ~/.meta-memory/agents.json
  meta-memory serve --agents-file ~/.meta-memory/agents.json --host 0.0.0.0 --tls-cert cert.pem --tls-key key.pem
  meta-memory serve --agents-file ~/.meta-memory/agents.json --no-access-log --shutdown-timeout 30
""")
    _set_help(init_agents, description="Create a usable agents.json from the installed package. It stores only token environment-variable names, never bearer-token values.", examples="""Example:
  meta-memory init-agents-file --output ~/.meta-memory/agents.json --agent-id home-robot --workspace-id home --subject-id person:owner --subject-id person:child --audience-id <audience-id> --audience-id <channel-id> --token-env META_MEMORY_TOKEN_ROBOT
""")
    _set_help(before_cmd, description="Begin and durably record a Turn, then return bounded relevant context. Agent integrations must run this before drafting.", examples="""Examples:
  meta-memory before --query "What did we decide?"
  meta-memory before --query-file request.txt --turn stable-turn-id
""")
    _set_help(after_cmd, description="Persist the exact completed draft for the Turn before it is sent. Reuse the turn id returned by before.", examples="""Examples:
  meta-memory after --turn <turn-id> --assistant-file response.txt
  meta-memory after --turn <turn-id> --assistant "Exact final answer"
""")
    _set_help(overview_cmd, description="Show the current project, setup readiness, memory queue, recent activity, and exact next commands. Server mode validates the on-disk store and Agent bindings; use the running service's /readyz endpoint for live container readiness.", examples="""Examples:
  meta-memory overview
  meta-memory overview --project my-project
  meta-memory overview --server --agents-file ~/.meta-memory/agents.json
  meta-memory --json overview
""")
    _set_help(backup, description="Create a self-verifying portable ZIP of the configured local store. Docker operators should use `docker compose run --rm --no-deps worker meta-memory-backup` so the matching Agent-binding sidecars and retention policy are included.", examples="""Examples:
  meta-memory backup --output ~/meta-memory-backup.zip
  meta-memory --json backup --output ./backups/manual.zip

Docker deployment:
  docker compose run --rm --no-deps worker meta-memory-backup
""")
    _set_help(restore, description="Validate a portable ZIP, restore it through a staging directory, and update the selected local configuration. Restoring can replace an existing destination only with --force. Docker operators should use `sh docker/restore.sh <filename.zip>` so services, the protection backup, and Agent bindings are handled together.", examples="""Examples:
  meta-memory restore ~/meta-memory-backup.zip --destination ~/.meta-memory-restored
  meta-memory restore ~/meta-memory-backup.zip --destination ~/.meta-memory --force

Docker deployment:
  sh docker/restore.sh meta-memory-YYYYMMDDTHHMMSSZ.zip
""")
    _set_help(memory_cmd, description="Manage durable Claims. Use `memory show` before correcting, archiving, or forgetting a Claim.", examples="""Examples:
  meta-memory memory recent
  meta-memory memory search "release process"
  meta-memory memory show <claim-id>
""")
    _set_help(inbox_cmd, description="Automatic memory changes stay reviewable. Inspect a proposal before approving or rejecting it.", examples="""Examples:
  meta-memory inbox list
  meta-memory inbox show <proposal-id>
  meta-memory inbox approve <proposal-id>
""")
    _set_help(project, description="A project is the memory boundary for the current directory. `auto` uses the Git root when available.", examples="""Examples:
  meta-memory project current
  meta-memory project set my-project
  meta-memory project stats
""")
    _set_help(agent_cmd, description="Inspect installed Agent Skills and launchers. Run sync after moving the repository or upgrading Meta Memory.", examples="""Examples:
  meta-memory agent status --all --verbose
  meta-memory agent upgrade-status --all
  meta-memory agent sync --all
""")
    _set_help(agent_status_cmd, description="Separate installation files, launcher verification, contract freshness, and observed before/after activity for one project.", examples="""Examples:
  meta-memory agent status --all --verbose
  meta-memory agent status --all --project my-project
""")
    _set_help(agent_verify_cmd, description="Run the generated launcher against the shared config and store. This does not prove the host loaded the Skill; complete a real Agent turn to verify activation.", examples="""Examples:
  meta-memory agent verify codex
  meta-memory agent verify my-agent --project my-project
""")
    _set_help(agent_sync_cmd, description="Regenerate Skills and launchers from the installed package. Use after an upgrade, Python move, or contract change.", examples="""Examples:
  meta-memory agent sync --all
  meta-memory agent sync codex my-agent
""")
    _set_help(dream, description="Dream consolidates completed work. Heartbeat is lightweight; deep synthesis is an auditable report and supports preview.", examples="""Examples:
  meta-memory dream heartbeat
  meta-memory dream deep --scan-days 7 --dry-run
  meta-memory dream status
""")
    _set_help(schedule, description="Install only Meta Memory's enabled background tasks. Do not use it to manage unrelated system jobs.", examples="""Examples:
  meta-memory schedule install
  meta-memory schedule status
  meta-memory schedule run-maintain
""")
    _set_help(turn_cmd, description="Turns protect the before/after lifecycle used by Agent integrations. Use recovery after an interruption.", examples="""Examples:
  meta-memory turn list --unfinished
  meta-memory turn touch <turn-id>
  meta-memory turn reopen <turn-id>
""")
    _set_help(resource_cmd, description="Imported files remain source evidence. They are searchable but are not automatically promoted to durable user facts.", examples="""Examples:
  meta-memory resource list
  meta-memory resource show <resource-id>
  meta-memory resource refresh <resource-id>
""")
    _set_help(history_cmd, description="Search completed session summaries first; use detail only when a concrete prior task needs more context.", examples="""Examples:
  meta-memory history recent
  meta-memory history "deployment"
  meta-memory history show <session-id>
""")
    _set_help(shared_cmd, description="Publish only useful shared summaries and current state. Raw conversations and device diagnostics remain in their original workspace/Agent scope.", examples="""Examples:
  meta-memory shared init --type household --key home --label "Family home" --restricted --member-agent home-robot --member-agent family-planner
  meta-memory shared publish --channel-id <id> --summary "Refrigerator is not cooling"
  meta-memory shared state-set --channel-id <id> --state-key last_seen --subject-id person:child --summary "Last seen at playground" --source-ref robot:event-42 --valid-until <future-ISO8601-with-offset>
""")
    _set_help(asset_cmd, description="Store raw bytes outside SQLite with SHA-256 deduplication. SQLite retains immutable metadata and map/observation links.", examples="""Examples:
  meta-memory asset add room.jpg --media-type image/jpeg
  meta-memory asset show <asset-id>
  meta-memory asset export <asset-id> --output recovered.jpg
""")
    _set_help(map_cmd, description="A stable map id has immutable increasing versions, a coordinate frame, capture time, and an optional raw asset.", examples="""Examples:
  meta-memory map add --channel-id <id> --map-id home-floor-1 --coordinate-frame map --asset-id <asset-id>
  meta-memory map list --channel-id <id>
""")
    _set_help(spatial_cmd, description="Store Agent/tool-produced captions, OCR, objects, locations, timestamps, confidence, and provenance; Meta Memory does not perform vision, OCR, SLAM, or route planning itself.", examples="""Examples:
  meta-memory spatial add --channel-id <id> --map-id home-floor-1 --asset-id <asset-id> --location-text kitchen --caption "Water under sink"
  meta-memory spatial search "water sink" --channel-id <id>
""")
    _polish_help(parser)
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    # Public CLI commands may import legacy-backed modules which use absolute
    # imports such as ``from _common import ...``.  Keep this at the one
    # dispatch boundary so programmatic callers and the console entry point
    # both have the compatibility path before any command-specific import.
    bootstrap()
    config = load_config(args.config)
    command = args.command
    if command in {"before", "after", "remember", "correct", "search", "history", "memory", "session", "overview", "status", "agent", "import", "resource", "turn", "recovery"}:
        from .runtime import after, before, correct, origin_agent_id, read_text, remember, search
    if args.command == "setup": return _setup(config, args)
    if args.command == "serve":
        from .http_api import serve

        root = Path(args.store).expanduser().resolve() if args.store else config.store
        serve(
            host=args.host,
            port=args.port,
            store=root,
            agents_file=args.agents_file,
            config=config,
            max_asset_bytes=max(1, int(args.max_asset_mb)) * 1024 * 1024,
            asset_chunk_bytes=max(1, int(args.asset_chunk_mb)) * 1024 * 1024,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
            access_log=args.access_log,
            shutdown_timeout=args.shutdown_timeout,
        )
        return {"status": "stopped"}
    if args.command == "init-agents-file":
        from .server_config import write_agent_binding

        return write_agent_binding(
            args.output,
            profile_id=args.profile_id or config.profile_id,
            agent_id=args.agent_id,
            token_env=args.token_env,
            workspaces=args.workspace_id,
            subject_ids=args.subject_id,
            audiences=args.audience_id,
            replace_agent=args.replace_agent,
        )
    if args.command == "install-remote-agent":
        from .remote_installer import install_remote_agent

        result = install_remote_agent(
            args.agent_id,
            args.skill_dir,
            args.server_url,
            args.workspace_id,
            args.subject_id,
            audience_id=args.audience_id,
            channel_id=args.channel_id,
            token_env=args.token_env,
            outbox_dir=args.outbox_dir,
            timeout_seconds=args.timeout,
        )
        if os.environ.get(args.token_env, "").strip():
            from .remote_client import RemoteConfig, RemoteMemoryClient

            remote = RemoteConfig.load(str(result["config"]))
            result["connectivity"] = RemoteMemoryClient(remote).status()
        else:
            result["connectivity"] = {
                "status": "not_checked",
                "reason": f"{args.token_env} is not set in this process",
            }
        return result
    if args.command == "install-agent":
        from .skill_installer import install_agents
        agents = (["all"] if args.all else []) + list(args.agent or [])
        if not agents:
            raise ValueError("Provide an agent name or --all.")
        custom_options = bool(args.skill_dir or args.agent_id or args.host_file or args.no_host_file)
        if custom_options and "custom" not in agents:
            raise ValueError("--skill-dir, --agent-id, --host-file and --no-host-file require install-agent custom.")
        installed = install_agents(
            agents,
            config=config,
            custom_skill_dir=args.skill_dir,
            custom_agent_id=args.agent_id,
            custom_host_file=args.host_file,
            no_host_file=args.no_host_file,
        )
        issues = [item for item in installed if str(item.get("status") or "ok") != "ok"]
        next_action = (
            str(issues[0].get("next_action") or "meta-memory agent status --all --verbose")
            if issues
            else None if installed
            else "Name an Agent explicitly, e.g. meta-memory install-agent codex."
        )
        return {
            "status": "needs_action" if issues or not installed else "ok",
            "installed": installed,
            "next_action": next_action,
        }
    if args.command == "project":
        from .project_detection import bind_project
        from .ux_projects import project_current, project_list, project_rename, project_stats, project_unbind
        if args.project_command == "set":
            context = bind_project(config, args.name, args.cwd); save_config(config)
            return {"status": "ok", "project": context.project_id, "root": str(context.root), "config": str(config.path)}
        if args.project_command == "current": return project_current(config, project_name=args.project, start=args.cwd)
        if args.project_command == "list": return project_list(config)
        if args.project_command == "rename": return project_rename(config, old=args.old, new=args.new)
        if args.project_command == "unbind": return project_unbind(config, start=args.cwd, all_bindings=args.all)
        return project_stats(config, project_name=args.project, start=args.cwd)
    if args.command == "before": return before(config, query=read_text(args.query, args.query_file, preserve=True), session=args.session, project_name=args.project, start=args.cwd, agent_id=args.agent_id, turn_uid=args.turn)
    if args.command == "after": return after(config, turn_uid=args.turn, user_text=read_text(args.user, args.user_file, preserve=True), assistant_text=read_text(args.assistant, args.assistant_file, preserve=True), session=args.session, project_name=args.project, start=args.cwd, agent_id=origin_agent_id(args.agent_id))
    if args.command == "remember": return remember(config, content=read_text(args.content, args.content_file), title=args.title, session=args.session, project_name=args.project, start=args.cwd, agent_id=args.agent_id, scope=args.scope, source_kind=args.source_kind, source_ref=args.source_ref)
    if args.command == "correct": return correct(config, memory_id=args.memory, content=read_text(args.content, args.content_file), agent_id=args.agent_id)
    if args.command == "search":
        result = search(config, query=args.query, project_name=args.project, start=args.cwd, limit=args.limit, agent_id=args.agent_id)
        if args.claims_only:
            result["results"] = [item for item in result.get("results", []) if str(item.get("memory_kind") or "") not in {"resource", "dream"} and bool(item.get("id") or item.get("memory_id"))]
            result["claims_only"] = True
        return result
    if args.command == "history":
        from .ux_history import history_recent, history_search, history_show
        parts = list(args.history_args or [])
        if not parts:
            raise ValueError("Use `history <query>`, `history recent`, `history search <query>`, or `history show <session-id>`.")
        action = str(parts[0]).casefold()
        if action == "recent": return history_recent(config, project_name=args.project, start=args.cwd, limit=args.limit, agent_id=args.agent_id)
        if action == "show":
            if len(parts) < 2: raise ValueError("history show requires a session id.")
            return history_show(config, session_id=parts[1], project_name=args.project, start=args.cwd, agent_id=args.agent_id, last=args.last)
        query = " ".join(parts[1:] if action == "search" else parts).strip()
        return history_search(config, query=query, project_name=args.project, start=args.cwd, agent_id=args.agent_id, detail=args.detail)
    if args.command in {"inbox", "review"}:
        from .ux_inbox import inbox_approve, inbox_feedback, inbox_list, inbox_reject, inbox_show
        if args.inbox_command == "list": return inbox_list(config, project_name=args.project, start=args.cwd, status=args.status, kind=args.kind, limit=args.limit, all_projects=args.all_projects)
        if args.inbox_command == "show": return inbox_show(config, proposal_id=args.proposal_id, kind=args.kind)
        if args.inbox_command == "approve": return inbox_approve(config, proposal_id=args.proposal_id, kind=args.kind, agent_id=args.agent_id)
        if args.inbox_command == "reject": return inbox_reject(config, proposal_id=args.proposal_id, kind=args.kind, note=args.note)
        return inbox_feedback(config, memory_id=args.memory, feedback_type=args.type, note=args.note, retrieval_id=args.retrieval_id, agent_id=args.agent_id)
    if args.command == "memory":
        from .ux_memory import memory_archive, memory_export, memory_forget, memory_list, memory_recent, memory_show
        if args.memory_command == "list": return memory_list(config, project_name=args.project, start=args.cwd, limit=args.limit, status=args.status, kind=args.kind, all_projects=args.all_projects)
        if args.memory_command == "recent": return memory_recent(config, project_name=args.project, start=args.cwd, limit=args.limit, all_projects=args.all_projects)
        if args.memory_command == "show": return memory_show(config, memory_id=args.memory_id, project_name=args.project, start=args.cwd, all_projects=args.all_projects)
        if args.memory_command == "search":
            result = search(config, query=args.query, project_name=args.project, start=args.cwd, limit=args.limit, agent_id=args.agent_id)
            if args.claims_only:
                result["results"] = [item for item in result.get("results", []) if str(item.get("memory_kind") or "") not in {"resource", "dream"} and bool(item.get("id") or item.get("memory_id"))]
                result["claims_only"] = True
            return result
        if args.memory_command == "correct": return correct(config, memory_id=args.memory_id, content=read_text(args.content, args.content_file), agent_id=args.agent_id)
        if args.memory_command == "feedback":
            from .ux_inbox import inbox_feedback

            return inbox_feedback(config, memory_id=args.memory_id, feedback_type=args.type, note=args.note, retrieval_id=args.retrieval_id, agent_id=args.agent_id)
        if args.memory_command == "archive": return memory_archive(config, memory_id=args.memory_id, project_name=args.project, start=args.cwd, all_projects=args.all_projects)
        if args.memory_command == "forget": return memory_forget(config, memory_id=args.memory_id, project_name=args.project, start=args.cwd, all_projects=args.all_projects)
        return memory_export(config, project_name=args.project, start=args.cwd, output=args.output, format=args.format, status=args.status, all_projects=args.all_projects)
    if args.command == "session":
        from .session_manager import close_session as close_cached_session, new_session, resolve_session
        from .project_detection import resolve_project
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
    if args.command == "overview":
        from .ux_overview import overview

        return overview(
            config, project_name=args.project, start=args.cwd, agent_id=args.agent_id,
            server=args.server, agents_file=args.agents_file,
        )
    if args.command == "status":
        from .maintenance import status
        from .ux_overview import overview

        # Keep the long-standing top-level ``status: ok`` contract used by
        # installed launchers, while adding the human-facing overview beneath
        # it instead of changing the machine result to needs_action.
        result = status(config)
        result["overview"] = overview(config, project_name=args.project, start=args.cwd, agent_id=args.agent_id)
        return result
    if args.command == "doctor":
        from .maintenance import status

        return status(config)["health"]
    if args.command == "agent":
        from .agent_status import agent_status, verify_agent
        from .skill_installer import agent_upgrade_status, sync_agents, uninstall_agent
        if args.agent_command == "verify":
            return verify_agent(config, agent_id=args.agent_id, project_name=args.project, start=args.cwd)
        if args.agent_command in {"sync", "repair"}:
            return sync_agents(config, agents=args.agents, all_agents=args.all, verify=not args.no_verify)
        if args.agent_command == "uninstall": return uninstall_agent(config, args.agent_id)
        if args.agent_command == "upgrade-status": return agent_upgrade_status(config, agent_id=args.agent_id or "", all_agents=args.all)
        return agent_status(config, agent_id=origin_agent_id(args.agent_id), all_agents=args.all, installed_default=not args.all, project_name=args.project, start=args.cwd, verbose=args.verbose)
    if args.command == "config":
        from .config_commands import describe_config_value, get_config_value, list_config_values, set_config_value
        if args.config_command == "get": return get_config_value(config, args.key)
        if args.config_command == "list": return list_config_values(config)
        if args.config_command == "describe": return describe_config_value(config, args.key)
        return set_config_value(config, args.key, args.value, apply_schedule=args.apply)
    if args.command == "maintain":
        from .maintenance import maintain

        return maintain(config, max_jobs=args.max_jobs)
    if args.command == "schedule":
        from .scheduler import schedule_install, schedule_remove, schedule_run, schedule_status

        if args.schedule_command == "install": return schedule_install(config)
        if args.schedule_command == "status": return schedule_status(config)
        if args.schedule_command == "remove": return schedule_remove(config)
        return schedule_run(config, "maintain" if args.schedule_command == "run-maintain" else "dream")
    if args.command == "dream":
        from .dream import archive_dream_run, list_dream_runs, preview_dream, show_dream_run
        from .dream_heartbeat import dream_status, run_deep, run_heartbeat

        if args.dream_command == "heartbeat": return run_heartbeat(config)
        if args.dream_command == "status": return dream_status(config)
        if args.dream_command == "list": return list_dream_runs(config, limit=args.limit, include_archived=args.include_archived)
        if args.dream_command == "show": return show_dream_run(config, run_id=args.run_id, include_content=not args.no_content)
        if args.dream_command == "archive": return archive_dream_run(config, run_id=args.run_id)
        days = getattr(args, "deep_scan_days", None) or args.scan_days
        if args.dry_run: return preview_dream(config, scan_days=days)
        return run_deep(config, scan_days=days)
    if args.command == "turn":
        from .project_detection import resolve_project
        from .turn_service import complete_late_turn, get_turn, list_turns, reopen_turn, touch_turn

        stable_agent = "" if getattr(args, "all_agents", False) else origin_agent_id(args.agent_id)
        if args.turn_command == "list":
            project = resolve_project(config, args.project, args.cwd)
            statuses = ("started", "abandoned") if args.unfinished else None
            return list_turns(config, agent_id=stable_agent, workspace_id=project.workspace_id, statuses=statuses, limit=args.limit)
        if args.turn_command == "show": return get_turn(config, turn_uid=args.turn_id, agent_id=origin_agent_id(args.agent_id))
        if args.turn_command == "touch": return touch_turn(config, turn_uid=args.turn_id, agent_id=origin_agent_id(args.agent_id), note=args.note)
        if args.turn_command == "complete": return complete_late_turn(config, turn_uid=args.turn_id, assistant_text=read_text(args.assistant, args.assistant_file, preserve=True), agent_id=origin_agent_id(args.agent_id))
        return reopen_turn(config, turn_uid=args.turn_id, agent_id=origin_agent_id(args.agent_id), reason=args.reason)
    if args.command == "recovery":
        from .project_detection import resolve_project
        from .spool import pending_dir, replay_spool
        from .turn_recovery import recover_expired_turns
        from .turn_service import list_turns

        if args.recovery_command == "replay":
            replayed = replay_spool(config, limit=args.limit)
            recovered = recover_expired_turns(config, limit=args.limit)
            return {"status": "ok", "spool": replayed, "turn_recovery": recovered}
        project = resolve_project(config, args.project, args.cwd)
        turns = list_turns(config, workspace_id=project.workspace_id, statuses=("started", "abandoned"), limit=50)
        spool = pending_dir(config)
        return {"status": "ok", "project": project.project_id, "pending_spool": len(list(spool.glob("*.json"))) if spool.is_dir() else 0, "unfinished_turns": turns.get("turns", []), "next_action": "meta-memory recovery replay" if (turns.get("turns") or spool.is_dir() and any(spool.glob("*.json"))) else None}
    if args.command == "backup":
        from .backup import backup_app

        return backup_app(config, args.output)
    if args.command == "restore":
        from .backup import restore_app

        return restore_app(config, args.archive, args.destination, force=args.force)
    if args.command == "import":
        from .importer import import_file, import_paths

        if not args.recursive and not args.changed_only and Path(args.file).expanduser().is_file():
            return import_file(config, file_path=args.file, project_name=args.project, start=args.cwd, agent_id=origin_agent_id(args.agent_id))
        return import_paths(config, path=args.file, recursive=args.recursive, changed_only=args.changed_only, project_name=args.project, start=args.cwd, agent_id=origin_agent_id(args.agent_id))
    if args.command == "resource":
        from .importer import resource_export, resource_list, resource_refresh, resource_remove, resource_show

        if args.resource_command == "list": return resource_list(config, project_name=args.project, start=args.cwd, limit=args.limit, all_projects=args.all_projects)
        if args.resource_command == "show": return resource_show(config, resource_id=args.resource_id, project_name=args.project, start=args.cwd, all_projects=args.all_projects, chunk_limit=args.chunk_limit)
        if args.resource_command == "refresh": return resource_refresh(config, resource_id=args.resource_id, project_name=args.project, start=args.cwd, agent_id=origin_agent_id(args.agent_id))
        if args.resource_command == "remove": return resource_remove(config, resource_id=args.resource_id, project_name=args.project, start=args.cwd, all_projects=args.all_projects)
        return resource_export(config, project_name=args.project, start=args.cwd, output=args.output, format=args.format, all_projects=args.all_projects)
    if args.command == "shared":
        return _dispatch_shared(config, args)
    if args.command in {"asset", "map", "spatial"}:
        return _dispatch_spatial(config, args)
    raise ValueError(f"Unsupported command: {args.command}")


def _normalise_output_args(argv: list[str]) -> list[str]:
    """Accept ``--json`` in the natural post-command position as well."""
    values = list(argv)
    if "--json" in values:
        values = [item for item in values if item != "--json"]
        values.insert(0, "--json")
    # A command-level export uses ``--format markdown``.  Only lift formats
    # that belong to the global output contract.
    for index, item in enumerate(list(values)):
        if item == "--format" and index + 1 < len(values) and values[index + 1] in {"auto", "json", "text"}:
            value = values[index + 1]
            del values[index:index + 2]
            values[0:0] = ["--format", value]
            break
    return values


def main(argv: list[str] | None = None) -> None:
    # Do this before parser/dispatch work so ``python -m meta_memory.cli`` and
    # the installed ``meta-memory`` console entry share the same cold-start
    # behavior.  ``bootstrap`` is idempotent, and ``dispatch`` repeats it for
    # callers that use the dispatch API directly.
    bootstrap()
    raw = list(sys.argv[1:] if argv is None else argv)
    # Launchers are commonly executed through ``cmd.exe`` while their caller
    # captures stdout as UTF-8.  Pin non-interactive output to UTF-8 before
    # any localized scheduler/detail text is serialized into JSON.
    if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass
    try:
        parsed = build_parser().parse_args(_normalise_output_args(raw))
        result = dispatch(parsed)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        _emit({"status": "error", "error": str(exc)})
        raise SystemExit(2) from exc
    output_format = "json" if bool(getattr(parsed, "output_format_json", False)) else str(getattr(parsed, "format", "auto"))
    if output_format not in {"auto", "json", "text"}:
        output_format = "auto"
    _emit(result, output_format=output_format)


if __name__ == "__main__":
    main()
