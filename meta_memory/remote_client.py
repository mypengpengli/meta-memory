"""Dependency-free client for the hosted Meta Memory turn protocol.

The client deliberately keeps authentication material out of configuration,
launchers, state receipts, and the retry outbox.  A configuration file stores
only the *name* of the environment variable that contains the bearer token.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


REMOTE_CLIENT_CONTRACT_VERSION = "remote-turn-v1"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TRANSIENT_HTTP = {408, 425, 429}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    if "\x00" in text:
        raise ValueError(f"{name} contains an invalid null byte")
    return text


def _required_text(value: object, name: str) -> str:
    """Validate non-empty user text without changing a single character."""

    text = str(value or "")
    if not text.strip():
        raise ValueError(f"{name} is required")
    if "\x00" in text:
        raise ValueError(f"{name} contains an invalid null byte")
    return text


def _validate_url(value: str) -> str:
    url = _required(value, "server URL").rstrip("/")
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        raise ValueError("server URL must not contain credentials")
    if parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("server URL must be an absolute origin without query or fragment")
    local = parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("server URL must use HTTPS (HTTP is allowed only for localhost)")
    return url


def _read_json_file(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required in {path}")
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        for attempt in range(20):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.01 * (attempt + 1))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _file_lock(path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    """Hold a small cross-process advisory lock for one receipt or outbox item."""

    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RemoteSemanticError(f"timed out acquiring local outbox lock: {path.name}") from exc
                time.sleep(0.02)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


class _RejectRedirects(HTTPRedirectHandler):
    """Never forward a bearer token (or a POST body) through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects())


def _open_url(request: Request, *, timeout: float):
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


class RemoteClientError(RuntimeError):
    """Base error that is safe to expose to the Agent."""


class RemoteConfigurationError(RemoteClientError):
    pass


class RemoteTransportError(RemoteClientError):
    """The server may not have returned a definitive response."""


class RemoteSemanticError(RemoteClientError):
    """The server definitively rejected the operation."""

    def __init__(self, message: str, *, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RemoteConfig:
    url: str
    token_env: str
    agent_id: str
    workspace_id: str
    subject_id: str
    outbox_dir: Path
    audience_id: str = ""
    channel_id: str = ""
    timeout_seconds: float = 20.0
    session_id: str = ""
    config_path: Path | None = None
    # Immutable, non-secret installation identity used only to bind durable
    # local records.  Effective workspace/subject/audience/channel fields may
    # be deliberate per-request overrides and must not change this owner.
    configured_binding: tuple[tuple[str, str], ...] = ()

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        agent_id: str = "",
        workspace_id: str = "",
        subject_id: str = "",
        session_id: str = "",
        audience_id: str = "",
        channel_id: str = "",
    ) -> "RemoteConfig":
        selected = path or os.environ.get("META_MEMORY_REMOTE_CONFIG", "")
        config_path = Path(selected).expanduser().resolve() if selected else None
        data: dict[str, Any] = {}
        if config_path is not None:
            if not config_path.is_file():
                raise RemoteConfigurationError(f"remote config does not exist: {config_path}")
            try:
                data = _read_json_file(config_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise RemoteConfigurationError(f"cannot read remote config: {exc}") from exc
        # A literal token in a file is always a configuration error, even when
        # an environment override would otherwise hide it.
        if any(key in data for key in ("token", "bearer_token", "api_key")):
            raise RemoteConfigurationError("remote config must not contain a token; store only token_env")
        token_env = str(data.get("token_env") or "META_MEMORY_TOKEN").strip()
        if not _ENV_NAME.fullmatch(token_env):
            raise RemoteConfigurationError("token_env must be a valid environment variable name")
        # Once a config file is selected it is an identity boundary.  Ambient
        # META_MEMORY_* variables must not silently redirect its bearer token
        # or move its outbox.  Explicit method/CLI arguments remain deliberate
        # overrides for per-request workspace/subject/session selection.
        pinned = config_path is not None

        def configured(key: str, environment: str, explicit: str = "") -> str:
            if explicit:
                return explicit
            if pinned:
                return str(data.get(key) or "")
            return os.environ.get(environment, "").strip() or str(data.get(key) or "")

        url_value = configured("url", "META_MEMORY_URL")
        chosen_agent = configured("agent_id", "META_MEMORY_AGENT_ID", agent_id)
        chosen_workspace = configured("workspace_id", "META_MEMORY_WORKSPACE_ID", workspace_id)
        chosen_subject = configured("subject_id", "META_MEMORY_SUBJECT_ID", subject_id)
        chosen_session = configured("session_id", "META_MEMORY_SESSION_ID", session_id)
        chosen_audience = configured("audience_id", "META_MEMORY_AUDIENCE_ID", audience_id)
        chosen_channel = configured("channel_id", "META_MEMORY_CHANNEL_ID", channel_id)
        raw_outbox = configured("outbox_dir", "META_MEMORY_OUTBOX")
        if pinned:
            configured_agent = str(data.get("agent_id") or "").strip()
            configured_workspace = str(data.get("workspace_id") or "").strip()
            configured_subject = str(data.get("subject_id") or "").strip()
            configured_audience = str(data.get("audience_id") or "").strip()
            configured_channel = str(data.get("channel_id") or "").strip()
        else:
            # In environment-only installations the META_MEMORY_* defaults
            # are the immutable launcher binding, while explicit arguments
            # are still one-request overrides.  Fall back to the effective
            # value only when no ambient default exists (programmatic use).
            configured_agent = os.environ.get("META_MEMORY_AGENT_ID", "").strip() or str(data.get("agent_id") or "").strip() or chosen_agent
            configured_workspace = os.environ.get("META_MEMORY_WORKSPACE_ID", "").strip() or str(data.get("workspace_id") or "").strip() or chosen_workspace
            configured_subject = os.environ.get("META_MEMORY_SUBJECT_ID", "").strip() or str(data.get("subject_id") or "").strip() or chosen_subject
            configured_audience = os.environ.get("META_MEMORY_AUDIENCE_ID", "").strip() or str(data.get("audience_id") or "").strip() or chosen_audience
            configured_channel = os.environ.get("META_MEMORY_CHANNEL_ID", "").strip() or str(data.get("channel_id") or "").strip() or chosen_channel
        binding_values = {
            "origin": url_value,
            "agent_id": configured_agent,
            "workspace_id": configured_workspace,
            "subject_id": configured_subject,
            "audience_id": configured_audience,
            "channel_id": configured_channel,
        }
        if raw_outbox:
            outbox = Path(raw_outbox).expanduser()
            if not outbox.is_absolute() and config_path is not None:
                outbox = config_path.parent / outbox
            outbox = outbox.resolve()
        else:
            safe_agent = re.sub(r"[^a-zA-Z0-9._-]", "-", configured_agent).strip("-") or "remote-agent"
            scope = _sha256(f"{url_value}\0{configured_workspace}\0{configured_subject}")[:16]
            outbox = (Path.home() / ".meta-memory" / "remote" / safe_agent / scope / "outbox").resolve()
        try:
            timeout_value = (
                data.get("timeout_seconds")
                if pinned
                else os.environ.get("META_MEMORY_TIMEOUT", "") or data.get("timeout_seconds")
            )
            timeout = float(timeout_value or 20)
        except (TypeError, ValueError) as exc:
            raise RemoteConfigurationError("timeout_seconds must be a number") from exc
        try:
            return cls(
                url=_validate_url(url_value),
                token_env=token_env,
                agent_id=_required(chosen_agent, "agent_id"),
                workspace_id=_required(chosen_workspace, "workspace_id"),
                subject_id=_required(chosen_subject, "subject_id"),
                audience_id=str(chosen_audience or "").strip(),
                channel_id=str(chosen_channel or "").strip(),
                session_id=str(chosen_session or "").strip(),
                outbox_dir=outbox,
                timeout_seconds=max(1.0, min(120.0, timeout)),
                config_path=config_path,
                configured_binding=tuple((key, str(value or "").strip()) for key, value in binding_values.items()),
            )
        except ValueError as exc:
            raise RemoteConfigurationError(str(exc)) from exc

    def token(self) -> str:
        token = os.environ.get(self.token_env, "").strip()
        if not token:
            raise RemoteConfigurationError(
                f"authentication token is unavailable; set environment variable {self.token_env}"
            )
        return token

    def binding(self) -> dict[str, str]:
        """Return the non-secret identity that owns local durable records."""

        if self.configured_binding:
            return dict(self.configured_binding)
        return {
            "origin": self.url,
            "agent_id": self.agent_id,
            "workspace_id": self.workspace_id,
            "subject_id": self.subject_id,
            "audience_id": self.audience_id,
            "channel_id": self.channel_id,
        }

    def identity(
        self,
        *,
        workspace_id: str = "",
        subject_id: str = "",
        session_id: str = "",
        audience_id: str = "",
        channel_id: str = "",
        require_session: bool = True,
    ) -> dict[str, str]:
        identity = {
            "agent_id": self.agent_id,
            "workspace_id": _required(workspace_id or self.workspace_id, "workspace_id"),
            "subject_id": _required(subject_id or self.subject_id, "subject_id"),
            "session_id": str(session_id or self.session_id).strip(),
            "audience_id": str(audience_id or self.audience_id).strip(),
            "channel_id": str(channel_id or self.channel_id).strip(),
        }
        if require_session:
            _required(identity["session_id"], "session_id")
        return identity


class RemoteOutbox:
    """Atomic, per-operation outbox with per-Turn state receipts."""

    _BINDING_FIELDS = ("origin", "agent_id", "workspace_id", "subject_id", "audience_id", "channel_id")

    def __init__(self, root: Path, *, binding: Mapping[str, str] | None = None):
        self.root = Path(root).expanduser().resolve()
        self.pending = self.root / "pending"
        self.states = self.root / "turns"
        self.uploads = self.root / "uploads"
        self.binding = {
            key: str((binding or {}).get(key) or "").strip()
            for key in self._BINDING_FIELDS
        }
        self.binding_fingerprint = _sha256(
            json.dumps(self.binding, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    @staticmethod
    def _key_path_component(value: str) -> str:
        return _sha256(value)[:24]

    def state_path(self, turn_id: str) -> Path:
        return self.states / f"turn-{self._key_path_component(turn_id)}.json"

    def _stamp(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Installation binding and per-request identity are deliberately
        # separate.  A Turn may use an administrator-approved subject,
        # workspace, or session override; stamping the configured defaults at
        # the top level used to overwrite that exact request identity in Turn
        # receipts.  Keep the immutable installation binding in its own field
        # and use its fingerprint to prevent replay through another origin or
        # Agent configuration.
        payload["config_binding"] = dict(self.binding)
        payload["binding_fingerprint"] = self.binding_fingerprint
        return payload

    def _validate_binding(self, value: Mapping[str, Any], *, label: str, require: bool = True) -> None:
        fingerprint = str(value.get("binding_fingerprint") or "")
        if require and not fingerprint:
            raise RemoteSemanticError(f"{label} predates origin binding and cannot be replayed safely")
        if fingerprint and fingerprint != self.binding_fingerprint:
            raise RemoteSemanticError(f"{label} belongs to a different remote server or identity")
        recorded_binding = value.get("config_binding")
        if isinstance(recorded_binding, Mapping):
            for key in self._BINDING_FIELDS:
                recorded = str(recorded_binding.get(key) or "").strip()
                if recorded != self.binding[key]:
                    raise RemoteSemanticError(f"{label} belongs to a different configured {key}")
        else:
            # Version-2 receipts stored the installation binding directly on
            # the record.  Continue to validate those rows so default-scope
            # pending work remains recoverable after upgrade.
            for key in self._BINDING_FIELDS:
                recorded = str(value.get(key) or "").strip()
                if recorded and recorded != self.binding[key]:
                    raise RemoteSemanticError(f"{label} belongs to a different {key}")

    @staticmethod
    def _ack_path(path: Path) -> Path:
        return path.with_name(path.name + ".ack")

    @contextmanager
    def locked(self, path: Path) -> Iterator[None]:
        with _file_lock(path):
            yield

    def _load_state_unlocked(self, turn_id: str) -> dict[str, Any] | None:
        path = self.state_path(turn_id)
        if not path.is_file():
            return None
        try:
            value = _read_json_file(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RemoteSemanticError(f"local Turn receipt is unreadable: {exc}") from exc
        if str(value.get("turn_id") or "") != turn_id:
            raise RemoteSemanticError("local Turn receipt identity mismatch")
        # Successful receipts from the first prerelease did not carry an
        # origin.  They can be inspected, but save_state upgrades them before
        # they can create a replayable operation.  Bound receipts must match.
        self._validate_binding(value, label="local Turn receipt", require=False)
        return value

    def load_state(self, turn_id: str) -> dict[str, Any] | None:
        path = self.state_path(turn_id)
        with self.locked(path):
            return self._load_state_unlocked(turn_id)

    def save_state(self, turn_id: str, **values: Any) -> dict[str, Any]:
        path = self.state_path(turn_id)
        with self.locked(path):
            state = self._load_state_unlocked(turn_id) or {"version": 2, "turn_id": turn_id, "created_at": _now()}
            state.update(values)
            state["version"] = 2
            state["updated_at"] = _now()
            self._stamp(state)
            _atomic_json(path, state)
            return state

    def _pending_path(self, operation: str, idempotency_key: str) -> Path:
        priority = "00" if operation == "before" else "10" if operation == "after" else "50"
        return self.pending / f"{priority}-{operation}-{self._key_path_component(idempotency_key)}.json"

    def enqueue(
        self,
        *,
        operation: str,
        method: str,
        path: str,
        payload: dict[str, Any],
        turn_id: str,
        idempotency_key: str,
        error: str,
        answer_sha256: str = "",
    ) -> dict[str, Any]:
        target = self._pending_path(operation, idempotency_key)
        with self.locked(target):
            existing: dict[str, Any] = {}
            if target.is_file():
                try:
                    existing = _read_json_file(target)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    raise RemoteSemanticError(
                        f"existing local outbox item is corrupt and was preserved: {target.name}"
                    ) from exc
            if existing:
                self._validate_binding(existing, label="local outbox item")
            if existing and existing.get("payload") != payload:
                raise RemoteSemanticError("an outbox item with this idempotency key has different content")
            record = {
                "version": 2,
                "operation": operation,
                "method": method,
                "path": path,
                "payload": payload,
                "turn_id": turn_id,
                "idempotency_key": idempotency_key,
                "answer_sha256": answer_sha256,
                "created_at": existing.get("created_at") or _now(),
                "updated_at": _now(),
                "attempt_count": int(existing.get("attempt_count") or 0),
                "last_error": str(error)[:1000],
            }
            self._stamp(record)
            _atomic_json(target, record)
            self._ack_path(target).unlink(missing_ok=True)
        return {"outbox_id": target.stem, "outbox_path": str(target), "answer_sha256": answer_sha256}

    def pending_records(self, *, turn_id: str = "", operation: str = "") -> list[tuple[Path, dict[str, Any]]]:
        rows: list[tuple[Path, dict[str, Any]]] = []
        if not self.pending.is_dir():
            return rows
        for path in sorted(self.pending.glob("*.json")):
            if self._ack_path(path).is_file():
                continue
            try:
                value = _read_json_file(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                # Corruption is actionable durable state, never equivalent to
                # an empty outbox.  Keep the source file untouched for repair.
                if turn_id or operation:
                    continue
                rows.append(
                    (
                        path,
                        {
                            "version": 0,
                            "operation": "corrupt",
                            "blocked": True,
                            "corrupt": True,
                            "last_error": f"unreadable local outbox item: {exc}",
                        },
                    )
                )
                continue
            if turn_id and str(value.get("turn_id") or "") != turn_id:
                continue
            if operation and str(value.get("operation") or "") != operation:
                continue
            rows.append((path, value))
        return rows

    def pending_count(self) -> int:
        return len(self.pending_records())

    def validate_pending(self, record: Mapping[str, Any]) -> None:
        if bool(record.get("corrupt")):
            raise RemoteSemanticError(str(record.get("last_error") or "local outbox item is corrupt"))
        self._validate_binding(record, label="local outbox item")

    def acknowledge(self, path: Path) -> None:
        """Make an acknowledged item invisible before best-effort deletion."""

        marker = self._ack_path(path)
        _atomic_json(marker, {"version": 1, "acknowledged_at": _now(), "outbox_item": path.name})
        for attempt in range(20):
            try:
                path.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
                return
            except PermissionError:
                time.sleep(0.01 * (attempt + 1))
        # The marker is authoritative.  A later status/recovery scan ignores
        # the stale data file instead of replaying an already acknowledged op.

    def mark_blocked(self, path: Path, detail: str) -> None:
        with self.locked(path):
            record = _read_json_file(path)
            self._validate_binding(record, label="local outbox item")
            record["blocked"] = True
            record["attempt_count"] = int(record.get("attempt_count") or 0) + 1
            record["last_error"] = str(detail)[:1000]
            record["updated_at"] = _now()
            _atomic_json(path, record)

    def upload_path(self, fingerprint: str) -> Path:
        return self.uploads / f"asset-{self._key_path_component(fingerprint)}.json"

    def load_upload(self, fingerprint: str) -> dict[str, Any] | None:
        path = self.upload_path(fingerprint)
        with self.locked(path):
            if not path.is_file():
                return None
            try:
                value = _read_json_file(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise RemoteSemanticError(f"local asset-upload receipt is unreadable: {exc}") from exc
            if str(value.get("fingerprint") or "") != fingerprint:
                raise RemoteSemanticError("local asset-upload receipt identity mismatch")
            self._validate_binding(value, label="local asset-upload receipt")
            return value

    def save_upload(self, fingerprint: str, **values: Any) -> dict[str, Any]:
        path = self.upload_path(fingerprint)
        with self.locked(path):
            if path.is_file():
                try:
                    state = _read_json_file(path)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    raise RemoteSemanticError(f"local asset-upload receipt is unreadable: {exc}") from exc
                self._validate_binding(state, label="local asset-upload receipt")
            else:
                state = {"version": 2, "fingerprint": fingerprint, "created_at": _now()}
            state.update(values)
            state["version"] = 2
            state["updated_at"] = _now()
            self._stamp(state)
            _atomic_json(path, state)
            return state

    def clear_upload(self, fingerprint: str) -> None:
        path = self.upload_path(fingerprint)
        with self.locked(path):
            path.unlink(missing_ok=True)


class RemoteMemoryClient:
    def __init__(self, config: RemoteConfig):
        self.config = config
        self.outbox = RemoteOutbox(config.outbox_dir, binding=config.binding())

    def _safe_detail(self, value: object) -> str:
        detail = str(value or "")[:2000]
        try:
            token = self.config.token()
        except RemoteConfigurationError:
            token = ""
        return detail.replace(token, "[REDACTED]") if token else detail

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = self.config.token()
        url = f"{self.config.url}{path}"
        if query:
            url += "?" + urlencode({key: value for key, value in query.items() if value not in (None, "")})
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=data,
            method=method.upper(),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                **({"Content-Type": "application/json; charset=utf-8"} if data is not None else {}),
                "User-Agent": f"meta-memory-remote/{REMOTE_CLIENT_CONTRACT_VERSION}",
            },
        )
        try:
            with _open_url(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            try:
                raw_error = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw_error = ""
            finally:
                exc.close()
            detail = self._safe_detail(raw_error or f"HTTP {exc.code}")
            if 300 <= exc.code <= 399:
                raise RemoteSemanticError(
                    f"remote redirect refused (HTTP {exc.code}); configure the final server URL",
                    status_code=exc.code,
                ) from exc
            if exc.code in _TRANSIENT_HTTP or 500 <= exc.code <= 599:
                raise RemoteTransportError(f"remote service unavailable (HTTP {exc.code})") from exc
            raise RemoteSemanticError(detail, status_code=exc.code) from exc
        except (URLError, TimeoutError, OSError, http.client.IncompleteRead, UnicodeDecodeError) as exc:
            raise RemoteTransportError(self._safe_detail(getattr(exc, "reason", exc))) from exc
        try:
            result = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise RemoteTransportError("remote service returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RemoteTransportError("remote service returned a non-object JSON response")
        return result

    def _request_binary(
        self,
        method: str,
        path: str,
        data: bytes,
        *,
        query: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        token = self.config.token()
        url = f"{self.config.url}{path}?" + urlencode(
            {key: value for key, value in query.items() if value not in (None, "")}
        )
        request = Request(
            url,
            data=data,
            method=method.upper(),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": f"meta-memory-remote/{REMOTE_CLIENT_CONTRACT_VERSION}",
                **(headers or {}),
            },
        )
        try:
            with _open_url(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            try:
                raw_error = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw_error = ""
            finally:
                exc.close()
            detail = self._safe_detail(raw_error or f"HTTP {exc.code}")
            if 300 <= exc.code <= 399:
                raise RemoteSemanticError(
                    f"remote redirect refused (HTTP {exc.code}); configure the final server URL",
                    status_code=exc.code,
                ) from exc
            if exc.code in _TRANSIENT_HTTP or 500 <= exc.code <= 599:
                raise RemoteTransportError(f"remote service unavailable (HTTP {exc.code})") from exc
            raise RemoteSemanticError(detail, status_code=exc.code) from exc
        except (URLError, TimeoutError, OSError, http.client.IncompleteRead, UnicodeDecodeError) as exc:
            raise RemoteTransportError(self._safe_detail(getattr(exc, "reason", exc))) from exc
        try:
            result = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise RemoteTransportError("remote service returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RemoteTransportError("remote service returned a non-object JSON response")
        return result

    def _download_to(
        self,
        path: str,
        *,
        query: dict[str, Any],
        target: Path,
    ) -> tuple[int, str, dict[str, str]]:
        token = self.config.token()
        url = f"{self.config.url}{path}?" + urlencode(
            {key: value for key, value in query.items() if value not in (None, "")}
        )
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {token}",
                "User-Agent": f"meta-memory-remote/{REMOTE_CLIENT_CONTRACT_VERSION}",
            },
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        size = 0
        digest = hashlib.sha256()
        try:
            with _open_url(request, timeout=max(self.config.timeout_seconds, 120.0)) as response:
                headers = {key.casefold(): value for key, value in response.headers.items()}
                with temporary.open("xb") as output:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        output.write(block)
                        digest.update(block)
                        size += len(block)
        except HTTPError as exc:
            temporary.unlink(missing_ok=True)
            try:
                raw_error = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw_error = ""
            finally:
                exc.close()
            detail = self._safe_detail(raw_error or f"HTTP {exc.code}")
            if 300 <= exc.code <= 399:
                raise RemoteSemanticError(
                    f"remote redirect refused (HTTP {exc.code}); configure the final server URL",
                    status_code=exc.code,
                ) from exc
            if exc.code in _TRANSIENT_HTTP or 500 <= exc.code <= 599:
                raise RemoteTransportError(f"remote service unavailable (HTTP {exc.code})") from exc
            raise RemoteSemanticError(detail, status_code=exc.code) from exc
        except (URLError, TimeoutError, OSError, http.client.IncompleteRead, UnicodeDecodeError) as exc:
            temporary.unlink(missing_ok=True)
            raise RemoteTransportError(self._safe_detail(getattr(exc, "reason", exc))) from exc
        try:
            expected_size = int(headers.get("content-length") or size)
            if size != expected_size:
                raise RemoteTransportError("download ended before Content-Length")
            actual_hash = digest.hexdigest()
            expected_hash = str(headers.get("x-content-sha256") or "").strip().casefold()
            if expected_hash and actual_hash != expected_hash:
                raise RemoteTransportError("download SHA-256 verification failed")
            temporary.replace(target)
            return size, actual_hash, headers
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _ensure_server_success(
        result: dict[str, Any],
        *,
        allowed_statuses: Iterable[str] = ("ok",),
    ) -> dict[str, Any]:
        status = str(result.get("status") or "").casefold()
        if status in {"error", "failed", "rejected"} or result.get("error"):
            raise RemoteSemanticError(str(result.get("detail") or result.get("error") or "remote operation rejected"))
        allowed = {str(value).casefold() for value in allowed_statuses}
        if not status or status not in allowed:
            raise RemoteTransportError("remote service returned no definitive acknowledgement status")
        return result

    def _validate_turn_ack(
        self,
        result: dict[str, Any],
        *,
        operation: str,
        turn_id: str,
        answer_sha256: str = "",
    ) -> dict[str, Any]:
        allowed = ("ok", "degraded") if operation == "before" else ("ok", "spooled")
        self._ensure_server_success(result, allowed_statuses=allowed)
        returned = str(result.get("turn_id") or result.get("turn_uid") or "")
        if not returned:
            raise RemoteTransportError("remote service did not acknowledge turn_id")
        if returned != turn_id:
            raise RemoteSemanticError("server returned a different turn_id")
        if operation == "after":
            returned_hash = str(result.get("answer_sha256") or result.get("response_hash") or "").casefold()
            if not returned_hash:
                raise RemoteTransportError("remote service did not acknowledge answer_sha256")
            if returned_hash != answer_sha256.casefold():
                raise RemoteSemanticError("server acknowledged a different answer_sha256")
        return result

    def before(
        self,
        query_text: str,
        *,
        workspace_id: str = "",
        subject_id: str = "",
        session_id: str = "",
        turn_id: str = "",
    ) -> dict[str, Any]:
        text = _required_text(query_text, "query")
        identity = self.config.identity(
            workspace_id=workspace_id, subject_id=subject_id, session_id=session_id, require_session=True
        )
        uid = str(turn_id or uuid.uuid4()).strip()
        _required(uid, "turn_id")
        query_hash = _sha256(text)
        state = self.outbox.load_state(uid)
        if state:
            if state.get("query_sha256") != query_hash:
                raise RemoteSemanticError("this turn_id is already bound to a different user request")
            for key in ("agent_id", "workspace_id", "subject_id", "session_id", "audience_id", "channel_id"):
                if str(state.get(key) or "") != identity[key]:
                    raise RemoteSemanticError(f"this turn_id is already bound to a different {key}")
        idempotency_key = f"turn:{uid}:before:{query_hash}"
        payload = {
            **identity,
            "turn_id": uid,
            "query": text,
            "query_sha256": query_hash,
            "idempotency_key": idempotency_key,
            "client_contract_version": REMOTE_CLIENT_CONTRACT_VERSION,
        }
        # The complete replay payload is always the first durable write.  A
        # crash between local receipt updates can therefore never strand only
        # a hash while losing the user's exact request.
        queued = self.outbox.enqueue(
            operation="before", method="POST", path="/v1/turns/before", payload=payload,
            turn_id=uid, idempotency_key=idempotency_key, error="awaiting remote acknowledgement",
        )
        self.outbox.save_state(uid, **identity, query_sha256=query_hash, before_status="attempting")
        pending_path = Path(str(queued["outbox_path"]))
        try:
            result = self._validate_turn_ack(
                self._request("POST", "/v1/turns/before", payload),
                operation="before",
                turn_id=uid,
            )
            self.outbox.save_state(uid, before_status="ok", before_at=_now())
            self.outbox.acknowledge(pending_path)
            result["turn_id"] = uid
            return result
        except RemoteTransportError as exc:
            queued = self.outbox.enqueue(
                operation="before", method="POST", path="/v1/turns/before", payload=payload,
                turn_id=uid, idempotency_key=idempotency_key, error=self._safe_detail(exc),
            )
            self.outbox.save_state(uid, before_status="local_outbox")
            return {
                "status": "degraded", "turn_id": uid, "hot_context": "", "context": "",
                "shared_context": {"activities": [], "states": [], "spatial": [], "counts": {"activities": 0, "states": 0, "spatial": 0}, "truncated": False},
                "durability": "local_outbox", "reason": "remote_unavailable", **queued,
            }
        except RemoteSemanticError as exc:
            self.outbox.mark_blocked(pending_path, self._safe_detail(exc))
            self.outbox.save_state(uid, before_status="semantic_error")
            raise

    def _replay_pending_before(self, turn_id: str) -> None:
        rows = self.outbox.pending_records(turn_id=turn_id, operation="before")
        for path, record in rows:
            self.outbox.validate_pending(record)
            if bool(record.get("blocked")):
                raise RemoteSemanticError(
                    str(record.get("last_error") or "the pending before operation is blocked")
                )
            try:
                result = self._validate_turn_ack(
                    self._request(str(record.get("method") or "POST"), str(record.get("path") or ""), dict(record.get("payload") or {})),
                    operation="before",
                    turn_id=turn_id,
                )
                self.outbox.save_state(turn_id, before_status="ok", before_at=_now())
                self.outbox.acknowledge(path)
            except RemoteTransportError:
                raise
            except RemoteSemanticError as exc:
                self.outbox.mark_blocked(path, self._safe_detail(exc))
                raise

    def after(self, turn_id: str, assistant_text: str) -> dict[str, Any]:
        uid = _required(turn_id, "turn_id")
        answer = _required_text(assistant_text, "assistant response")
        state = self.outbox.load_state(uid)
        if not state:
            raise RemoteSemanticError("unknown local turn_id; call before and retain its turn_id")
        answer_hash = _sha256(answer)
        prior_hash = str(state.get("answer_sha256") or "")
        if prior_hash and prior_hash != answer_hash:
            raise RemoteSemanticError("this turn_id is already bound to a different exact answer")
        identity = {key: str(state.get(key) or "") for key in ("agent_id", "workspace_id", "subject_id", "session_id", "audience_id", "channel_id")}
        idempotency_key = f"turn:{uid}:after:{answer_hash}"
        path = f"/v1/turns/{quote(uid, safe='')}/after"
        payload = {
            **identity,
            "turn_id": uid,
            "assistant_text": answer,
            "answer_sha256": answer_hash,
            "idempotency_key": idempotency_key,
            "client_contract_version": REMOTE_CLIENT_CONTRACT_VERSION,
        }
        queued = self.outbox.enqueue(
            operation="after", method="POST", path="/v1/recovery/replay", payload=payload, turn_id=uid,
            idempotency_key=idempotency_key, error="awaiting remote acknowledgement", answer_sha256=answer_hash,
        )
        self.outbox.save_state(uid, answer_sha256=answer_hash, after_status="attempting")
        pending_path = Path(str(queued["outbox_path"]))
        try:
            self._replay_pending_before(uid)
            result = self._validate_turn_ack(
                self._request("POST", path, payload),
                operation="after",
                turn_id=uid,
                answer_sha256=answer_hash,
            )
            self.outbox.save_state(uid, after_status=str(result.get("status") or "ok"), after_at=_now())
            self.outbox.acknowledge(pending_path)
            result["turn_id"] = uid
            result["answer_sha256"] = answer_hash
            return result
        except RemoteTransportError as exc:
            queued = self.outbox.enqueue(
                # A late replay may occur after the server has abandoned the
                # Turn.  The recovery endpoint accepts both still-open and
                # abandoned Turns while preserving the same idempotent answer.
                operation="after", method="POST", path="/v1/recovery/replay", payload=payload, turn_id=uid,
                idempotency_key=idempotency_key, error=self._safe_detail(exc), answer_sha256=answer_hash,
            )
            self.outbox.save_state(uid, after_status="local_outbox")
            return {
                "status": "local_outbox", "turn_id": uid, "answer_sha256": answer_hash,
                "durability": "local_outbox", "reason": "remote_acknowledgement_unavailable", **queued,
            }
        except RemoteSemanticError as exc:
            self.outbox.mark_blocked(pending_path, self._safe_detail(exc))
            self.outbox.save_state(uid, after_status="semantic_error")
            raise

    def touch(self, turn_id: str) -> dict[str, Any]:
        uid = _required(turn_id, "turn_id")
        state = self.outbox.load_state(uid)
        if not state:
            raise RemoteSemanticError("unknown local turn_id")
        payload = {
            **{key: str(state.get(key) or "") for key in ("agent_id", "workspace_id", "subject_id", "session_id", "audience_id", "channel_id")},
            "turn_id": uid,
        }
        result = self._ensure_server_success(
            self._request("POST", f"/v1/turns/{quote(uid, safe='')}/touch", payload)
        )
        returned = str(result.get("turn_id") or "")
        if not returned:
            raise RemoteTransportError("remote service did not acknowledge turn_id")
        if returned != uid:
            raise RemoteSemanticError("server returned a different turn_id")
        return result

    def status(self) -> dict[str, Any]:
        identity = self.config.identity(require_session=False)
        try:
            result = self._ensure_server_success(self._request("GET", "/v1/agent/status", query=identity))
            result.setdefault("status", "ok")
        except RemoteTransportError as exc:
            result = {"status": "unavailable", "error": "remote_unavailable", "detail": self._safe_detail(exc)}
        result["remote_url"] = self.config.url
        result["agent_id"] = self.config.agent_id
        result["workspace_id"] = self.config.workspace_id
        records = self.outbox.pending_records()
        result["local_outbox_pending"] = len(records)
        result["local_outbox_blocked"] = sum(1 for _path, item in records if bool(item.get("blocked")))
        result["local_outbox_corrupt"] = sum(1 for _path, item in records if bool(item.get("corrupt")))
        result["client_contract_version"] = REMOTE_CLIENT_CONTRACT_VERSION
        agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
        if agent:
            agent.setdefault("last_before", str(agent.get("last_before_at") or ""))
            agent.setdefault("last_after", str(agent.get("last_after_at") or ""))
            result.setdefault("lifecycle_state", str(agent.get("lifecycle_state") or ""))
            result.setdefault("last_before", str(agent.get("last_before") or ""))
            result.setdefault("last_after", str(agent.get("last_after") or ""))
        return result

    def _validate_operation_ack(
        self,
        result: dict[str, Any],
        *,
        operation: str,
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        turn_id = str(record.get("turn_id") or "")
        if operation in {"before", "after"}:
            return self._validate_turn_ack(
                result,
                operation=operation,
                turn_id=turn_id,
                answer_sha256=str(record.get("answer_sha256") or ""),
            )
        self._ensure_server_success(result)
        expected_keys = {
            "activity": "activity",
            "state": "state",
            "observe": "observation",
            "map": "map",
        }
        key = expected_keys.get(operation)
        if key and not isinstance(result.get(key), dict):
            raise RemoteTransportError(f"remote service did not acknowledge the {operation} record")
        return result

    def replay(self, *, limit: int = 100) -> dict[str, Any]:
        replayed: list[dict[str, Any]] = []
        stopped = False
        rows = self.outbox.pending_records()[: max(1, int(limit))]
        for path, record in rows:
            if bool(record.get("blocked")):
                replayed.append(
                    {"outbox_id": path.stem, "operation": str(record.get("operation") or ""),
                     "status": "blocked", "error": str(record.get("last_error") or "")}
                )
                continue
            try:
                self.outbox.validate_pending(record)
                operation = str(record.get("operation") or "")
                result = self._validate_operation_ack(
                    self._request(
                        str(record.get("method") or "POST"), str(record.get("path") or ""),
                        dict(record.get("payload") or {}),
                    ),
                    operation=operation,
                    record=record,
                )
                turn_id = str(record.get("turn_id") or "")
                if operation == "before":
                    self.outbox.save_state(turn_id, before_status="ok", before_at=_now())
                elif operation == "after":
                    self.outbox.save_state(turn_id, after_status=str(result.get("status") or "ok"), after_at=_now())
                self.outbox.acknowledge(path)
                replayed.append({"outbox_id": path.stem, "operation": operation, "status": "completed"})
            except RemoteTransportError as exc:
                replayed.append({"outbox_id": path.stem, "status": "pending", "error": self._safe_detail(exc)})
                stopped = True
                break
            except RemoteSemanticError as exc:
                try:
                    self.outbox.mark_blocked(path, self._safe_detail(exc))
                except RemoteSemanticError:
                    # A foreign-origin or corrupt item stays untouched and is
                    # still surfaced as blocked to the operator.
                    pass
                replayed.append({"outbox_id": path.stem, "status": "blocked", "error": self._safe_detail(exc)})
        pending = self.outbox.pending_count()
        return {
            "status": "ok" if pending == 0 else "needs_action" if any(row.get("status") == "blocked" for row in replayed) else "deferred",
            "replayed": replayed, "pending": pending, "stopped_on_transport_error": stopped,
        }

    def _identity_for_payload(self, payload: Mapping[str, Any], *, allow_scope_override: bool = True) -> dict[str, str]:
        if not allow_scope_override:
            return self.config.identity(require_session=False)
        return self.config.identity(
            workspace_id=str(payload.get("workspace_id") or ""),
            subject_id=str(payload.get("subject_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            audience_id=str(payload.get("audience_id") or ""),
            channel_id=str(payload.get("channel_id") or ""),
            require_session=False,
        )

    def _bound_body(self, payload: Mapping[str, Any], *, allow_scope_override: bool = True) -> dict[str, Any]:
        # Identity is written last so an arbitrary JSON payload can never
        # override the launcher/config Agent or an already-normalized scope.
        return {**dict(payload), **self._identity_for_payload(payload, allow_scope_override=allow_scope_override)}

    def _durable_write(
        self,
        *,
        operation: str,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        if not str(body.get("idempotency_key") or "").strip():
            canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            body["idempotency_key"] = f"{operation}:{_sha256(canonical)}"
        key = _required(body.get("idempotency_key"), "idempotency_key")
        queued = self.outbox.enqueue(
            operation=operation,
            method="POST",
            path=path,
            payload=body,
            turn_id="",
            idempotency_key=key,
            error="awaiting remote acknowledgement",
        )
        pending_path = Path(str(queued["outbox_path"]))
        record = {
            "operation": operation,
            "turn_id": "",
            "answer_sha256": "",
        }
        try:
            result = self._validate_operation_ack(
                self._request("POST", path, body), operation=operation, record=record
            )
            self.outbox.acknowledge(pending_path)
            return result
        except RemoteTransportError as exc:
            queued = self.outbox.enqueue(
                operation=operation,
                method="POST",
                path=path,
                payload=body,
                turn_id="",
                idempotency_key=key,
                error=self._safe_detail(exc),
            )
            return {
                "status": "local_outbox",
                "durability": "local_outbox",
                "operation": operation,
                "reason": "remote_acknowledgement_unavailable",
                **queued,
            }
        except RemoteSemanticError as exc:
            self.outbox.mark_blocked(pending_path, self._safe_detail(exc))
            raise

    def remember(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._bound_body(payload)
        return self._ensure_server_success(self._request("POST", "/v1/remember", body))

    def observe(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._bound_body(payload)
        body.setdefault("idempotency_key", f"observation:{_sha256(json.dumps(body, sort_keys=True, ensure_ascii=False))}")
        return self._durable_write(operation="observe", path="/v1/spatial-observations", body=body)

    def publish_activity(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._bound_body(payload)
        body.setdefault("idempotency_key", f"activity:{_sha256(json.dumps(body, sort_keys=True, ensure_ascii=False))}")
        return self._durable_write(operation="activity", path="/v1/activities", body=body)

    def publish_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._bound_body(payload)
        body.setdefault("idempotency_key", f"state:{_sha256(json.dumps(body, sort_keys=True, ensure_ascii=False))}")
        return self._durable_write(operation="state", path="/v1/states", body=body)

    def shared(self, action: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = dict(payload or {})
        query = {**filters, **self._identity_for_payload(filters)}
        paths = {"channels": "/v1/channels", "feed": "/v1/activities", "states": "/v1/states"}
        if action not in paths:
            raise ValueError(f"unsupported shared action: {action}")
        return self._ensure_server_success(self._request("GET", paths[action], query=query))

    def spatial(self, action: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = dict(payload or {})
        if action == "get":
            observation_id = _required(filters.pop("observation_id", ""), "observation_id")
            query = {**filters, **self._identity_for_payload(filters)}
            return self._ensure_server_success(
                self._request(
                    "GET", f"/v1/spatial-observations/{quote(observation_id, safe='')}", query=query
                )
            )
        if action not in {"list", "search"}:
            raise ValueError(f"unsupported spatial action: {action}")
        if action == "search":
            _required(filters.get("query"), "query")
        query = {**filters, **self._identity_for_payload(filters)}
        return self._ensure_server_success(self._request("GET", "/v1/spatial-observations", query=query))

    def asset(self, action: str, *, asset_id: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if action == "get":
            identifier = _required(asset_id, "asset_id")
            return self._ensure_server_success(self._request("GET", f"/v1/assets/{quote(identifier, safe='')}", query=self.config.identity(require_session=False)))
        if action == "list":
            return self._ensure_server_success(
                self._request("GET", "/v1/assets", query={**(payload or {}), **self.config.identity(require_session=False)})
            )
        raise ValueError(f"unsupported asset action: {action}")

    def upload_asset(
        self,
        file_path: str | Path,
        *,
        media_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = Path(file_path).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"asset file does not exist: {source}")
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        if size <= 0:
            raise ValueError("asset file is empty")
        sha256 = digest.hexdigest()
        upload_metadata = metadata or {}
        metadata_fingerprint = json.dumps(upload_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = _sha256(
            f"{self.outbox.binding_fingerprint}\0{source}\0{size}\0{source.stat().st_mtime_ns}"
            f"\0{sha256}\0{media_type}\0{metadata_fingerprint}"
        )
        identity = self.config.identity(require_session=False)
        for restart in range(2):
            receipt = self.outbox.load_upload(fingerprint) or {}
            if isinstance(receipt.get("result"), dict):
                return dict(receipt["result"])
            upload_id = str(receipt.get("upload_id") or "")
            chunk_size = int(receipt.get("chunk_size") or 0)
            try:
                if not upload_id:
                    initialized = self._ensure_server_success(self._request(
                        "POST",
                        "/v1/assets/uploads",
                        {
                            **identity,
                            "byte_size": size,
                            "sha256": sha256,
                            "media_type": media_type or "application/octet-stream",
                            "original_name": source.name,
                            "metadata": upload_metadata,
                        },
                    ))
                    upload_id = _required(initialized.get("upload_id"), "server upload_id")
                    chunk_size = max(64 * 1024, int(initialized.get("chunk_size") or 1024 * 1024))
                    receipt = self.outbox.save_upload(
                        fingerprint,
                        upload_id=upload_id,
                        chunk_size=chunk_size,
                        source=str(source),
                        byte_size=size,
                        sha256=sha256,
                        media_type=media_type or "application/octet-stream",
                        metadata_sha256=_sha256(metadata_fingerprint),
                        uploaded_parts=[],
                    )
                uploaded = {int(value) for value in receipt.get("uploaded_parts", []) if str(value).isdigit()}
                with source.open("rb") as stream:
                    index = 0
                    while True:
                        chunk = stream.read(chunk_size)
                        if not chunk:
                            break
                        if index not in uploaded:
                            self._ensure_server_success(self._request_binary(
                                "PUT",
                                f"/v1/assets/uploads/{quote(upload_id, safe='')}/parts/{index}",
                                chunk,
                                query=identity,
                                headers={
                                    "Content-Type": "application/octet-stream",
                                    "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
                                },
                            ))
                            uploaded.add(index)
                            self.outbox.save_upload(fingerprint, uploaded_parts=sorted(uploaded))
                        index += 1
                result = self._ensure_server_success(self._request(
                    "POST",
                    f"/v1/assets/uploads/{quote(upload_id, safe='')}/complete",
                    identity,
                ))
                asset = result.get("asset") if isinstance(result.get("asset"), dict) else {}
                if str(asset.get("sha256") or "") != sha256 or int(asset.get("byte_size") or 0) != size:
                    raise RemoteSemanticError("server completed asset does not match the local file")
                self.outbox.save_upload(fingerprint, completed_at=_now(), result=result)
                return result
            except RemoteSemanticError as exc:
                if exc.status_code == 404 and upload_id and restart == 0:
                    # Server retention may have removed an incomplete upload
                    # while this device was offline.  Reinitialize once using
                    # the same immutable local file fingerprint.
                    self.outbox.clear_upload(fingerprint)
                    continue
                raise
        raise RemoteTransportError("asset upload could not be reinitialized")

    def download_asset(self, asset_id: str, output: str | Path) -> dict[str, Any]:
        identifier = _required(asset_id, "asset_id")
        target = Path(output).expanduser().resolve()
        size, digest, headers = self._download_to(
            f"/v1/assets/{quote(identifier, safe='')}",
            query={**self.config.identity(require_session=False), "download": "1"},
            target=target,
        )
        return {
            "status": "ok",
            "asset_id": identifier,
            "output": str(target),
            "byte_size": size,
            "sha256": digest,
            "media_type": str(headers.get("content-type") or "application/octet-stream"),
        }

    def map(self, action: str, *, map_id: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if action == "get":
            identifier = _required(map_id, "map_id")
            return self._ensure_server_success(self._request("GET", f"/v1/maps/{quote(identifier, safe='')}", query=self.config.identity(require_session=False)))
        if action == "list":
            return self._ensure_server_success(
                self._request("GET", "/v1/maps", query={**(payload or {}), **self.config.identity(require_session=False)})
            )
        if action != "put":
            raise ValueError(f"unsupported map action: {action}")
        body = self._bound_body(payload or {}, allow_scope_override=False)
        return self._durable_write(operation="map", path="/v1/maps", body=body)


def _text_argument(args: argparse.Namespace, direct: str, file_name: str, label: str) -> str:
    value = str(getattr(args, direct, "") or "")
    file_value = str(getattr(args, file_name, "") or "")
    if value and file_value:
        raise ValueError(f"use either --{direct.replace('_', '-')} or --{file_name.replace('_', '-')}, not both")
    if file_value:
        # read_text() performs universal-newline conversion.  Decode bytes
        # directly so the Turn hash and transmitted text match the file.
        value = Path(file_value).expanduser().resolve().read_bytes().decode("utf-8")
    return _required_text(value, label)


def _payload_file(value: str) -> dict[str, Any]:
    return _read_json_file(Path(_required(value, "payload file")).expanduser().resolve())


def _add_identity(parser: argparse.ArgumentParser, *, session: bool = True) -> None:
    parser.add_argument(
        "--workspace-id", default="",
        help="Override the configured workspace for this request.",
    )
    parser.add_argument(
        "--subject-id", default="",
        help="Override the configured person or subject for this request.",
    )
    parser.add_argument(
        "--audience-id", default="",
        help="Override the configured sharing audience for this request.",
    )
    parser.add_argument(
        "--channel-id", default="",
        help="Override the configured shared channel for this request.",
    )
    if session:
        parser.add_argument(
            "--session-id", default="",
            help="Stable conversation or run identifier for this request.",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Use a hosted Meta Memory service from a Skill-capable Agent.")
    parser.add_argument("--config", default="", help="Non-secret remote JSON config; token is read from its token_env.")
    parser.add_argument("--agent-id", default="", help="Stable Agent identity, normally pinned by the generated launcher.")
    commands = parser.add_subparsers(dest="command", required=True)

    before = commands.add_parser("before", help="Begin one durable remote Turn before drafting.")
    _add_identity(before)
    before.add_argument("--turn-id", "--turn", dest="turn_id", default="")
    before_text = before.add_mutually_exclusive_group(required=True)
    before_text.add_argument("--query", default="")
    before_text.add_argument("--query-file", default="")

    after = commands.add_parser("after", help="Persist the exact answer for the same Turn before sending.")
    after.add_argument("--turn-id", "--turn", dest="turn_id", required=True)
    after_text = after.add_mutually_exclusive_group(required=True)
    after_text.add_argument("--assistant", default="")
    after_text.add_argument("--assistant-file", default="")

    touch = commands.add_parser("touch", help="Renew a long-running remote Turn.")
    touch.add_argument("--turn-id", "--turn", dest="turn_id", required=True)
    commands.add_parser("status", help="Check remote authentication, lifecycle evidence, and local outbox.")
    recovery = commands.add_parser("recovery", help="Replay pending local Turn requests through the server recovery route.")
    recovery.add_argument("--limit", type=int, default=100)

    remember = commands.add_parser("remember", help="Write an explicit shared memory.")
    _add_identity(remember)
    remember.add_argument("--title", required=True)
    remember_text = remember.add_mutually_exclusive_group(required=True)
    remember_text.add_argument("--content", default="")
    remember_text.add_argument("--content-file", default="")
    remember.add_argument("--visibility-scope", default="workspace")
    remember.add_argument("--memory-kind", default="")
    remember.add_argument("--source-ref", default="")

    observe = commands.add_parser("observe", help="Record one timestamped spatial observation linked to optional map/image assets.")
    _add_identity(observe)
    observe_text = observe.add_mutually_exclusive_group(required=True)
    observe_text.add_argument("--content", default="")
    observe_text.add_argument("--content-file", default="")
    observe.add_argument(
        "--observation-kind",
        choices=("spatial_observation",),
        default=None,
        help="Compatibility field; household events use activity and changing person/device facts use state.",
    )
    observe.add_argument("--observed-at", required=True)
    observe.add_argument("--valid-until", default="")
    observe.add_argument("--source-ref", required=True)
    observe.add_argument("--map-id", default="")
    observe.add_argument("--asset-id", action="append", default=None)
    observe.add_argument("--location-id", default="")
    observe.add_argument("--location-text", default="")
    observe.add_argument("--ocr-text", default="")
    observe.add_argument("--objects-file", default="")
    observe.add_argument("--confidence", type=float)
    observe.add_argument("--visibility-scope", choices=("channel", "workspace", "agent", "global"), default=None)
    observe.add_argument("--payload-file", default="")

    activity = commands.add_parser("activity", help="Publish one curated cross-workspace activity summary.")
    _add_identity(activity)
    activity_summary = activity.add_mutually_exclusive_group(required=True)
    activity_summary.add_argument("--summary", default="")
    activity_summary.add_argument("--summary-file", default="")
    activity.add_argument("--title", default="")
    activity.add_argument("--kind", default="update")
    activity.add_argument("--occurred-at", required=True)
    activity.add_argument("--valid-until", default="")
    activity.add_argument("--importance", type=float, default=0.5)
    activity.add_argument("--source-ref", default="")
    activity.add_argument("--confidence", type=float)
    activity.add_argument("--payload-file", default="")

    state = commands.add_parser("state", help="Publish a superseding, optionally expiring current state.")
    _add_identity(state)
    state.add_argument("--state-key", required=True)
    state_summary = state.add_mutually_exclusive_group(required=True)
    state_summary.add_argument("--summary", default="")
    state_summary.add_argument("--summary-file", default="")
    state.add_argument("--value-file", default="")
    state.add_argument("--source-ref", required=True)
    state.add_argument("--observed-at", required=True)
    state.add_argument("--valid-from", default="")
    state.add_argument("--valid-until", default="")
    state.add_argument("--confidence", type=float)

    # These resource families use real second-level subcommands.  Besides
    # producing useful ``<family> <action> --help`` output, this prevents an
    # Agent from accidentally passing (for example) download-only flags to an
    # upload or search-only flags to an observation lookup.
    asset = commands.add_parser(
        "asset", help="Upload, inspect, list, or download raw image/map assets.",
        description="Transfer and inspect hosted binary assets.",
    )
    asset_actions = asset.add_subparsers(dest="action", required=True, metavar="ACTION")
    asset_upload = asset_actions.add_parser("upload", help="Upload one local file with resumable chunks.")
    asset_upload.add_argument("--file", required=True, help="Local file to upload.")
    asset_upload.add_argument(
        "--media-type", default="application/octet-stream",
        help="IANA media type stored with the asset (default: application/octet-stream).",
    )
    asset_upload.add_argument(
        "--metadata-file", default="",
        help="Optional UTF-8 JSON object containing asset metadata.",
    )
    asset_get = asset_actions.add_parser("get", help="Read metadata for one asset.")
    asset_get.add_argument("--asset-id", required=True, help="Hosted asset identifier.")
    asset_list = asset_actions.add_parser("list", help="List assets visible to this Agent.")
    asset_list.add_argument("--media-type", default="", help="Optional exact media-type filter.")
    asset_list.add_argument("--limit", type=int, default=100, help="Maximum assets to return (default: 100).")
    asset_download = asset_actions.add_parser("download", help="Download and verify one asset.")
    asset_download.add_argument("--asset-id", required=True, help="Hosted asset identifier.")
    asset_download.add_argument("--output", required=True, help="Local destination file.")

    map_parser = commands.add_parser(
        "map", help="Register a versioned map manifest or retrieve one.",
        description="Publish and inspect versioned spatial map manifests.",
    )
    map_actions = map_parser.add_subparsers(dest="action", required=True, metavar="ACTION")
    map_put = map_actions.add_parser("put", help="Publish a map manifest from JSON.")
    map_put.add_argument(
        "--payload-file", required=True,
        help="UTF-8 JSON object containing map_id, coordinate_frame, and optional asset/version metadata.",
    )
    map_get = map_actions.add_parser("get", help="Read the latest version of one map.")
    map_get.add_argument("--map-id", required=True, help="Stable map identifier.")
    map_list = map_actions.add_parser("list", help="List maps visible in the configured channel.")
    map_list.add_argument(
        "--include-history", action="store_true",
        help="Return historical versions as well as each map's latest version.",
    )
    map_list.add_argument("--limit", type=int, default=100, help="Maximum map versions to return (default: 100).")

    shared = commands.add_parser(
        "shared", help="Read shared channels, activity feed, or temporal states.",
        description="Read curated cross-Agent household or team context.",
    )
    shared_actions = shared.add_subparsers(dest="action", required=True, metavar="ACTION")
    shared_channels = shared_actions.add_parser("channels", help="List channels visible to this Agent.")
    _add_identity(shared_channels, session=False)
    shared_feed = shared_actions.add_parser("feed", help="Read recent shared activities.")
    _add_identity(shared_feed, session=False)
    shared_feed.add_argument("--limit", type=int, default=100, help="Maximum activities to return (default: 100).")
    shared_states = shared_actions.add_parser("states", help="Read current or historical shared states.")
    _add_identity(shared_states, session=False)
    shared_states.add_argument("--state-key", default="", help="Optional exact state-key filter.")
    shared_states.add_argument(
        "--include-history", action="store_true",
        help="Include superseded and expired state records.",
    )
    shared_states.add_argument("--limit", type=int, default=100, help="Maximum states to return (default: 100).")

    spatial = commands.add_parser(
        "spatial", help="List, search, or inspect hosted spatial observations.",
        description="Read current or historical observations from maps, images, OCR, and robot scans.",
    )
    spatial_actions = spatial.add_subparsers(dest="action", required=True, metavar="ACTION")
    spatial_list = spatial_actions.add_parser("list", help="List spatial observations using structured filters.")
    _add_identity(spatial_list, session=False)
    spatial_list.add_argument("--map-id", default="", help="Optional stable map identifier filter.")
    spatial_list.add_argument("--location-id", default="", help="Optional structured location identifier filter.")
    spatial_list.add_argument(
        "--include-history", action="store_true",
        help="Include superseded and expired observations.",
    )
    spatial_list.add_argument("--limit", type=int, default=100, help="Maximum observations to return (default: 100).")
    spatial_search = spatial_actions.add_parser("search", help="Full-text search spatial observations.")
    search_input = spatial_search.add_mutually_exclusive_group(required=True)
    search_input.add_argument("search_query", nargs="?", help="Text to find in captions, OCR, objects, or locations.")
    search_input.add_argument("--query", default="", help="Named alternative to the positional search text.")
    _add_identity(spatial_search, session=False)
    spatial_search.add_argument("--map-id", default="", help="Optional stable map identifier filter.")
    spatial_search.add_argument(
        "--include-history", action="store_true",
        help="Include superseded and expired observations.",
    )
    spatial_search.add_argument("--limit", type=int, default=100, help="Maximum matches to return (default: 100).")
    spatial_get = spatial_actions.add_parser("get", help="Read one observation by identifier.")
    _add_identity(spatial_get, session=False)
    spatial_get.add_argument("--observation-id", required=True, help="Hosted spatial-observation identifier.")
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    config = RemoteConfig.load(
        args.config or None,
        agent_id=args.agent_id,
        workspace_id=getattr(args, "workspace_id", ""),
        subject_id=getattr(args, "subject_id", ""),
        session_id=getattr(args, "session_id", ""),
        audience_id=getattr(args, "audience_id", ""),
        channel_id=getattr(args, "channel_id", ""),
    )
    client = RemoteMemoryClient(config)
    if args.command == "before":
        return client.before(
            _text_argument(args, "query", "query_file", "query"), workspace_id=args.workspace_id,
            subject_id=args.subject_id, session_id=args.session_id, turn_id=args.turn_id,
        )
    if args.command == "after":
        return client.after(args.turn_id, _text_argument(args, "assistant", "assistant_file", "assistant response"))
    if args.command == "touch":
        return client.touch(args.turn_id)
    if args.command == "status":
        return client.status()
    if args.command == "recovery":
        return client.replay(limit=args.limit)
    if args.command == "remember":
        return client.remember({
            "title": args.title,
            "content": _text_argument(args, "content", "content_file", "content"),
            "workspace_id": args.workspace_id,
            "subject_id": args.subject_id,
            "session_id": args.session_id,
            "visibility_scope": args.visibility_scope,
            "force_kind": args.memory_kind or None,
            "source_ref": args.source_ref,
        })
    if args.command == "observe":
        extra = _payload_file(args.payload_file) if args.payload_file else {}
        objects_value: list[Any] | None = None
        if args.objects_file:
            raw_objects = json.loads(Path(args.objects_file).expanduser().resolve().read_text(encoding="utf-8"))
            if not isinstance(raw_objects, list):
                raise ValueError("--objects-file must contain a JSON array")
            objects_value = raw_objects
        observation: dict[str, Any] = {
            **extra,
            "content": _text_argument(args, "content", "content_file", "observation content"),
            "workspace_id": args.workspace_id,
            "subject_id": args.subject_id,
            "session_id": args.session_id,
            "observed_at": args.observed_at,
            "source_ref": args.source_ref,
        }
        for key, value in (
            ("observation_kind", args.observation_kind),
            ("valid_until", args.valid_until),
            ("map_id", args.map_id),
            ("asset_ids", args.asset_id),
            ("location_id", args.location_id),
            ("location_text", args.location_text),
            ("ocr_text", args.ocr_text),
            ("objects", objects_value),
            ("confidence", args.confidence),
            ("visibility_scope", args.visibility_scope),
        ):
            if value not in (None, "", []):
                observation[key] = value
        return client.observe(observation)
    if args.command == "activity":
        return client.publish_activity({
            "workspace_id": args.workspace_id,
            "subject_id": args.subject_id,
            "session_id": args.session_id,
            "summary": _text_argument(args, "summary", "summary_file", "activity summary"),
            "title": args.title,
            "activity_kind": args.kind,
            "occurred_at": args.occurred_at,
            "valid_until": args.valid_until,
            "importance": args.importance,
            "source_ref": args.source_ref,
            "confidence": args.confidence,
            "payload": _payload_file(args.payload_file) if args.payload_file else {},
        })
    if args.command == "state":
        summary = _text_argument(args, "summary", "summary_file", "state summary")
        value: Any = {"summary": summary}
        if args.value_file:
            value = json.loads(Path(args.value_file).expanduser().resolve().read_text(encoding="utf-8"))
        return client.publish_state({
            "workspace_id": args.workspace_id,
            "subject_id": args.subject_id,
            "session_id": args.session_id,
            "state_key": args.state_key,
            "summary": summary,
            "value": value,
            "source_ref": args.source_ref,
            "observed_at": args.observed_at,
            "valid_from": args.valid_from,
            "valid_until": args.valid_until,
            "confidence": args.confidence,
        })
    if args.command == "asset":
        if args.action == "upload":
            metadata = _payload_file(args.metadata_file) if args.metadata_file else {}
            return client.upload_asset(
                args.file,
                media_type=args.media_type or "application/octet-stream",
                metadata=metadata,
            )
        if args.action == "download":
            return client.download_asset(args.asset_id, args.output)
        if args.action == "get":
            return client.asset("get", asset_id=args.asset_id)
        filters = {"limit": args.limit}
        if args.media_type:
            filters["media_type"] = args.media_type
        return client.asset("list", payload=filters)
    if args.command == "map":
        payload = _payload_file(args.payload_file) if getattr(args, "payload_file", "") else {}
        if args.action == "list":
            payload.setdefault("limit", args.limit)
            if args.include_history:
                payload.setdefault("include_history", "1")
        return client.map(args.action, map_id=getattr(args, "map_id", ""), payload=payload)
    if args.command == "shared":
        filters: dict[str, Any] = {
            "workspace_id": getattr(args, "workspace_id", ""),
            "subject_id": getattr(args, "subject_id", ""),
            "audience_id": getattr(args, "audience_id", ""),
            "channel_id": getattr(args, "channel_id", ""),
        }
        if hasattr(args, "limit"):
            filters["limit"] = args.limit
        if getattr(args, "state_key", ""):
            filters["state_key"] = args.state_key
        if getattr(args, "include_history", False):
            filters["include_history"] = "1"
        return client.shared(args.action, payload=filters)
    if args.command == "spatial":
        filters = {
            "workspace_id": getattr(args, "workspace_id", ""),
            "subject_id": getattr(args, "subject_id", ""),
            "audience_id": getattr(args, "audience_id", ""),
            "channel_id": getattr(args, "channel_id", ""),
        }
        if hasattr(args, "limit"):
            filters["limit"] = args.limit
        search_query = getattr(args, "query", "") or getattr(args, "search_query", "")
        if search_query:
            filters["query"] = search_query
        if getattr(args, "observation_id", ""):
            filters["observation_id"] = args.observation_id
        if getattr(args, "map_id", ""):
            filters["map_id"] = args.map_id
        if getattr(args, "location_id", ""):
            filters["location_id"] = args.location_id
        if getattr(args, "include_history", False):
            filters["include_history"] = "1"
        return client.spatial(args.action, payload=filters)
    raise ValueError(f"unsupported command: {args.command}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = _run(args)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if str(result.get("status") or "ok") not in {"error", "failed"} else 2
    except RemoteSemanticError as exc:
        print(json.dumps({"status": "error", "error": "semantic_error", "detail": str(exc), "status_code": exc.status_code}, ensure_ascii=False))
        return 2
    except (RemoteConfigurationError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": "client_error", "detail": str(exc)}, ensure_ascii=False))
        return 2
    except RemoteTransportError as exc:
        print(json.dumps({"status": "unavailable", "error": "remote_unavailable", "detail": str(exc)}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
