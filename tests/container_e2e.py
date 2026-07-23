"""Real Docker image acceptance test for hosted Meta Memory.

This file is intentionally not named ``test_*.py``: the normal cross-platform
unit-test matrix must not require Docker.  The dedicated Linux CI job builds
the image once and invokes this script explicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    # CI installs the client first, matching a real remote Agent host.
    from meta_memory.remote_client import RemoteConfig, RemoteMemoryClient
except ModuleNotFoundError:
    # Also permit an explicit developer run from an uninstalled checkout.
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from meta_memory.remote_client import RemoteConfig, RemoteMemoryClient


TOKEN_ENV = "META_MEMORY_TOKEN_CONTAINER_E2E"
TOKEN = "container-e2e-token-that-is-not-written-to-config"
AGENT_ID = "container-agent"
WORKSPACE_ID = "container-e2e-workspace"
SUBJECT_ID = "person:owner"
RUNTIME_UID = str(os.getuid()) if hasattr(os, "getuid") and os.getuid() > 0 else "10001"
RUNTIME_GID = str(os.getgid()) if hasattr(os, "getgid") and os.getgid() > 0 else "10001"


def _run(*arguments: str, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(arguments),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and result.returncode:
        command = " ".join(arguments)
        raise RuntimeError(
            f"command failed ({result.returncode}): {command}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _mount(source: Path, destination: str, *, readonly: bool = False) -> str:
    suffix = ",readonly" if readonly else ""
    return f"type=bind,src={source.resolve()},dst={destination}{suffix}"


def _container_command(
    image: str,
    data_dir: Path,
    config_dir: Path,
    backup_dir: Path,
    *command: str,
) -> subprocess.CompletedProcess[str]:
    return _run(
        "docker",
        "run",
        "--rm",
        "--mount",
        _mount(data_dir, "/data"),
        "--mount",
        _mount(config_dir, "/config"),
        "--mount",
        _mount(backup_dir, "/backups"),
        "--env",
        f"{TOKEN_ENV}={TOKEN}",
        "--env",
        f"META_MEMORY_TOKEN_ENV={TOKEN_ENV}",
        "--env",
        f"META_MEMORY_BOOTSTRAP_AGENT_ID={AGENT_ID}",
        "--env",
        f"META_MEMORY_BOOTSTRAP_WORKSPACE_ID={WORKSPACE_ID}",
        "--env",
        f"META_MEMORY_BOOTSTRAP_SUBJECT_ID={SUBJECT_ID}",
        "--env",
        "META_MEMORY_PROFILE_NAME=Container E2E",
        "--env",
        f"META_MEMORY_UID={RUNTIME_UID}",
        "--env",
        f"META_MEMORY_GID={RUNTIME_GID}",
        image,
        *command,
        timeout=180,
    )


def _container_cli(
    image: str,
    data_dir: Path,
    config_dir: Path,
    backup_dir: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return _container_command(
        image,
        data_dir,
        config_dir,
        backup_dir,
        "meta-memory",
        "--config",
        "/config/config.toml",
        *arguments,
    )


def _json_output(result: subprocess.CompletedProcess[str], label: str) -> dict[str, object]:
    # A completely empty bind mount causes the entrypoint to print setup and
    # first-binding JSON before executing the requested command.  Decode the
    # stream and return the requested command's final document.
    decoder = json.JSONDecoder()
    offset = 0
    values: list[object] = []
    try:
        while offset < len(result.stdout):
            while offset < len(result.stdout) and result.stdout[offset].isspace():
                offset += 1
            if offset >= len(result.stdout):
                break
            value, offset = decoder.raw_decode(result.stdout, offset)
            values.append(value)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{label} returned invalid JSON output: {result.stdout!r}") from exc
    if not values or not isinstance(values[-1], dict):
        raise AssertionError(f"{label} returned no final JSON object")
    return values[-1]


def _http_json(url: str, *, timeout: float = 5) -> tuple[int, dict[str, object], dict[str, str]]:
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            headers = {key.casefold(): value for key, value in response.headers.items()}
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        status = int(exc.code)
        headers = {key.casefold(): value for key, value in exc.headers.items()}
        payload = json.loads(exc.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"{url} returned non-object JSON")
    return status, payload, headers


def _published_port(container_name: str) -> int:
    result = _run("docker", "port", container_name, "8765/tcp")
    # Docker can return either 127.0.0.1:49153 or [::]:49153.
    endpoint = result.stdout.strip().splitlines()[0]
    try:
        return int(endpoint.rsplit(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise AssertionError(f"cannot parse published Docker port: {endpoint!r}") from exc


def _wait_ready(base_url: str, container_name: str, *, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            health_status, health, health_headers = _http_json(f"{base_url}/healthz")
            ready_status, ready, ready_headers = _http_json(f"{base_url}/readyz")
            if health_status == 200 and health.get("status") == "ok" and ready_status == 200:
                if ready.get("status") != "ready":
                    raise AssertionError(f"unexpected readiness payload: {ready}")
                checks = ready.get("checks")
                if not isinstance(checks, dict):
                    raise AssertionError(f"readiness has no checks: {ready}")
                expected = {"agent_bindings", "database", "store", "assets"}
                if not expected.issubset(checks):
                    raise AssertionError(f"readiness is missing checks: {ready}")
                # The request id is part of the hosted observability contract.
                if not health_headers.get("x-request-id") or not ready_headers.get("x-request-id"):
                    raise AssertionError("health/readiness response is missing X-Request-ID")
                return
            last_error = f"health={health_status}:{health}; ready={ready_status}:{ready}"
        except (AssertionError, json.JSONDecodeError, OSError, URLError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    log_result = _run("docker", "logs", container_name, check=False)
    logs = f"{log_result.stdout}\n{log_result.stderr}"
    raise AssertionError(f"container did not become ready: {last_error}\ncontainer logs:\n{logs}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_server(
    *,
    image: str,
    container_name: str,
    data_dir: Path,
    config_dir: Path,
    host_port: int,
) -> str:
    _run(
        "docker",
        "run",
        "--detach",
        "--name",
        container_name,
        "--env",
        f"{TOKEN_ENV}={TOKEN}",
        "--env",
        f"META_MEMORY_UID={RUNTIME_UID}",
        "--env",
        f"META_MEMORY_GID={RUNTIME_GID}",
        "--publish",
        f"127.0.0.1:{host_port}:8765",
        "--mount",
        _mount(data_dir, "/data"),
        "--mount",
        _mount(config_dir, "/config"),
        image,
        "meta-memory",
        "--config",
        "/config/config.toml",
        "serve",
        "--agents-file",
        "/config/agents.json",
        "--store",
        "/data/store",
        "--host",
        "0.0.0.0",
        "--port",
        "8765",
        timeout=60,
    )
    published = _published_port(container_name)
    if published != host_port:
        raise AssertionError(f"Docker published {published}, expected {host_port}")
    base_url = f"http://127.0.0.1:{host_port}"
    _wait_ready(base_url, container_name)
    return base_url


def _stop_remove(container_name: str) -> None:
    _run("docker", "stop", "--time", "20", container_name, timeout=40, check=False)
    _run("docker", "rm", "--force", container_name, timeout=30, check=False)


@contextmanager
def _token_environment() -> Iterator[None]:
    previous = os.environ.get(TOKEN_ENV)
    os.environ[TOKEN_ENV] = TOKEN
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = previous


def run_acceptance(image: str) -> None:
    _run("docker", "image", "inspect", image)
    container_name = f"meta-memory-e2e-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="meta-memory-container-e2e-") as temporary:
        root = Path(temporary)
        data_dir = root / "data"
        config_dir = root / "config"
        backup_dir = root / "backups"
        outbox_dir = root / "client-outbox"
        host_port = _free_port()
        data_dir.mkdir()
        config_dir.mkdir()
        backup_dir.mkdir()
        # The production image runs without relying on the checkout.  Broad
        # temporary permissions make the test independent of the runner UID.
        data_dir.chmod(0o777)
        config_dir.chmod(0o777)
        backup_dir.chmod(0o777)

        try:
            setup = _container_cli(
                image,
                data_dir,
                config_dir,
                backup_dir,
                "--json",
                "setup",
                "--name",
                "Container E2E",
                "--store",
                "/data/store",
                "--maintenance",
                "no",
                "--dream",
                "no",
                "--non-interactive",
                "--no-schedule",
            )
            setup_json = _json_output(setup, "container setup")
            if not isinstance(setup_json.get("store"), dict):
                raise AssertionError(f"container setup did not initialize the store: {setup_json}")

            shared = _container_cli(
                image,
                data_dir,
                config_dir,
                backup_dir,
                "--json",
                "shared",
                "init",
                "--type",
                "household",
                "--key",
                "container-e2e",
                "--restricted",
                "--member-agent",
                AGENT_ID,
            )
            shared_json = _json_output(shared, "shared init")
            audience = shared_json.get("audience")
            channel = shared_json.get("channel")
            if not isinstance(audience, dict) or not isinstance(channel, dict):
                raise AssertionError(f"shared init did not return audience/channel: {shared_json}")
            profile_id = str(audience.get("profile_id") or "")
            audience_id = str(audience.get("audience_id") or "")
            channel_id = str(channel.get("channel_id") or "")
            if not profile_id or not audience_id or not channel_id:
                raise AssertionError(f"shared init returned incomplete identity: {shared_json}")

            binding = _container_cli(
                image,
                data_dir,
                config_dir,
                backup_dir,
                "--json",
                "init-agents-file",
                "--output",
                "/config/agents.json",
                "--agent-id",
                AGENT_ID,
                "--profile-id",
                profile_id,
                "--workspace-id",
                WORKSPACE_ID,
                "--subject-id",
                SUBJECT_ID,
                "--audience-id",
                audience_id,
                "--audience-id",
                channel_id,
                "--token-env",
                TOKEN_ENV,
                "--replace-agent",
            )
            binding_json = _json_output(binding, "Agent binding")
            if binding_json.get("status") != "ok":
                raise AssertionError(f"Agent binding was not written: {binding_json}")

            base_url = _start_server(
                image=image,
                container_name=container_name,
                data_dir=data_dir,
                config_dir=config_dir,
                host_port=host_port,
            )
            config = RemoteConfig(
                url=base_url,
                token_env=TOKEN_ENV,
                agent_id=AGENT_ID,
                workspace_id=WORKSPACE_ID,
                subject_id=SUBJECT_ID,
                audience_id=audience_id,
                channel_id=channel_id,
                outbox_dir=outbox_dir,
                timeout_seconds=5,
            )
            with _token_environment():
                client = RemoteMemoryClient(config)
                turn_id = str(uuid.uuid4())
                before = client.before(
                    "Does the container remember this exact turn?",
                    session_id="container-e2e-session",
                    turn_id=turn_id,
                )
                if before.get("status") not in {"ok", "degraded"}:
                    raise AssertionError(f"before failed: {before}")
                exact_answer = "  Container answer with preserved whitespace.\r\n"
                after = client.after(turn_id, exact_answer)
                if after.get("status") not in {"ok", "spooled"}:
                    raise AssertionError(f"after failed: {after}")

                asset_source = root / "room-scan.bin"
                asset_source.write_bytes((b"container-room-scan\x00" * 140_000) + b"tail")
                uploaded = client.upload_asset(
                    asset_source,
                    metadata={"source": "container-e2e", "kind": "room-scan"},
                )
                asset = uploaded.get("asset")
                if not isinstance(asset, dict) or not asset.get("asset_id"):
                    raise AssertionError(f"asset upload failed: {uploaded}")

                now = datetime.now(timezone.utc)
                mapped = client.map(
                    "put",
                    payload={
                        "map_id": "container-home-floor",
                        "coordinate_frame": "map",
                        "asset_id": str(asset["asset_id"]),
                        "captured_at": now.isoformat(),
                        "name": "Container test floor",
                        "idempotency_key": "container-e2e-map-v1",
                    },
                )
                if not isinstance(mapped.get("map"), dict):
                    raise AssertionError(f"map write failed: {mapped}")
                observed = client.observe(
                    {
                        "content": "Water is visible below the container test sink.",
                        "observed_at": now.isoformat(),
                        "valid_until": (now + timedelta(days=1)).isoformat(),
                        "source_ref": "container-e2e:camera:1",
                        "map_id": "container-home-floor",
                        "asset_ids": [str(asset["asset_id"])],
                        "location_id": "container-test-sink",
                        "location_text": "Container test kitchen sink",
                        "objects": [{"label": "water", "confidence": 0.99}],
                        "confidence": 0.99,
                    }
                )
                if not isinstance(observed.get("observation"), dict):
                    raise AssertionError(f"spatial observation failed: {observed}")
                found = client.spatial(
                    "search", payload={"query": "container test sink water", "limit": 5}
                )
                observations = found.get("observations")
                if not isinstance(observations, list) or len(observations) != 1:
                    raise AssertionError(f"spatial search failed: {found}")

                status = client.status()
                if status.get("lifecycle_state") != "active" or status.get("local_outbox_pending") != 0:
                    raise AssertionError(f"active status evidence is incomplete: {status}")

                backup_result = _container_command(
                    image,
                    data_dir,
                    config_dir,
                    backup_dir,
                    "meta-memory-backup",
                )
                backup_json = _json_output(backup_result, "container backup")
                backup_name = Path(str(backup_json.get("backup") or "")).name
                if backup_json.get("status") != "ok" or not backup_name.endswith(".zip"):
                    raise AssertionError(f"container backup failed: {backup_json}")
                backup_file = backup_dir / backup_name
                backup_base = backup_file.with_suffix("")
                agents_file = backup_base.with_suffix(".agents.json")
                manifest_file = backup_base.with_suffix(".manifest.json")
                if not all(path.is_file() for path in (backup_file, agents_file, manifest_file)):
                    raise AssertionError(f"container backup sidecars are incomplete: {backup_json}")
                backup_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                if backup_manifest.get("archive") != backup_file.name or backup_manifest.get("agents_file") != agents_file.name:
                    raise AssertionError(f"container backup manifest names are wrong: {backup_manifest}")
                if hashlib.sha256(backup_file.read_bytes()).hexdigest() != backup_manifest.get("archive_sha256"):
                    raise AssertionError("container backup ZIP does not match its outer manifest")
                if hashlib.sha256(agents_file.read_bytes()).hexdigest() != backup_manifest.get("agents_sha256"):
                    raise AssertionError("container Agent sidecar does not match its outer manifest")

                # Stop the authoritative process while retaining bind-mounted
                # data.  A real client must preserve both request and exact
                # answer locally, then replay the same Turn after restart.
                _stop_remove(container_name)
                offline_turn = str(uuid.uuid4())
                degraded = client.before(
                    "This request begins while the server is offline.",
                    session_id="container-e2e-offline",
                    turn_id=offline_turn,
                )
                if degraded.get("durability") != "local_outbox":
                    raise AssertionError(f"offline before was not buffered: {degraded}")
                deferred = client.after(offline_turn, "Exact offline answer.\r\n")
                if deferred.get("status") != "local_outbox":
                    raise AssertionError(f"offline after was not buffered: {deferred}")
                if client.outbox.pending_count() != 2:
                    raise AssertionError("offline Turn did not leave exactly before+after in the outbox")

                base_url = _start_server(
                    image=image,
                    container_name=container_name,
                    data_dir=data_dir,
                    config_dir=config_dir,
                    host_port=host_port,
                )
                if base_url != config.url:
                    raise AssertionError("server origin changed across a persistence restart")
                client = RemoteMemoryClient(config)
                recovered = client.replay()
                if recovered.get("status") != "ok" or recovered.get("pending") != 0:
                    raise AssertionError(f"outbox recovery failed: {recovered}")

                persisted = client.status()
                agent = persisted.get("agent")
                if not isinstance(agent, dict) or int(agent.get("turn_counts", {}).get("completed", 0)) < 2:
                    raise AssertionError(f"Turn data did not survive container restart: {persisted}")
                found_after_restart = client.spatial(
                    "search", payload={"query": "container test sink water", "limit": 5}
                )
                if len(found_after_restart.get("observations", [])) != 1:
                    raise AssertionError(
                        f"asset/map/spatial data did not survive container restart: {found_after_restart}"
                    )
                download = root / "downloaded-room-scan.bin"
                client.download_asset(str(asset["asset_id"]), download)
                if download.read_bytes() != asset_source.read_bytes():
                    raise AssertionError("downloaded persisted asset differs from the uploaded bytes")

                # Restore the pre-offline snapshot into the production store
                # subdirectory.  A bind mount root itself cannot be replaced;
                # /data/store permits same-volume staging and atomic rename.
                _stop_remove(container_name)
                restored_result = _container_cli(
                    image,
                    data_dir,
                    config_dir,
                    backup_dir,
                    "--json",
                    "restore",
                    f"/backups/{backup_name}",
                    "--destination",
                    "/data/store",
                    "--force",
                )
                restored_json = _json_output(restored_result, "container restore")
                if restored_json.get("status") != "ok":
                    raise AssertionError(f"container restore failed: {restored_json}")
                _start_server(
                    image=image,
                    container_name=container_name,
                    data_dir=data_dir,
                    config_dir=config_dir,
                    host_port=host_port,
                )
                client = RemoteMemoryClient(config)
                after_restore = client.status()
                restored_agent = after_restore.get("agent")
                restored_completed = (
                    int(restored_agent.get("turn_counts", {}).get("completed", 0))
                    if isinstance(restored_agent, dict)
                    else -1
                )
                if restored_completed != 1:
                    raise AssertionError(
                        f"restore did not return to the pre-offline snapshot: {after_restore}"
                    )
                restored_download = root / "restored-room-scan.bin"
                client.download_asset(str(asset["asset_id"]), restored_download)
                if restored_download.read_bytes() != asset_source.read_bytes():
                    raise AssertionError("restored asset differs from the backed-up bytes")
        finally:
            _stop_remove(container_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="meta-memory:container-e2e")
    args = parser.parse_args()
    run_acceptance(args.image)
    print(json.dumps({"status": "ok", "image": args.image, "checks": [
        "image_cli", "health", "readiness", "turn", "asset_map_spatial",
        "offline_outbox", "restart_persistence", "recovery", "backup_restore",
    ]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
