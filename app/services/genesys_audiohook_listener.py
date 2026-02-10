from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import os
import time
from array import array
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from app.config import settings as app_settings

try:
    from websockets.legacy.server import WebSocketServerProtocol
    from websockets.legacy.server import serve as ws_serve
except Exception:  # pragma: no cover - surfaced at runtime in command
    WebSocketServerProtocol = Any  # type: ignore[assignment]
    ws_serve = None


logger = logging.getLogger(__name__)


PACKET_TYPE_COMMAND = 0x01
PACKET_TYPE_AUDIO = 0x10
MAX_PACKET_PAYLOAD = 0xFFFFFF


@dataclass(frozen=True)
class GenesysAudioHookListenerConfig:
    host: str
    port: int
    path: str
    target_audio_ingest_url: str
    target_event_ingest_url: str
    target_ingest_token: str
    verify_ssl: bool
    http_timeout_seconds: int
    retry_max_attempts: int
    retry_backoff_seconds: float
    flush_interval_ms: int
    min_chunk_duration_ms: int
    max_chunk_duration_ms: int
    status_path: Path
    dry_run: bool = False

    @classmethod
    def from_settings(
        cls,
        *,
        dry_run: bool = False,
        host: str | None = None,
        port: int | None = None,
        path: str | None = None,
    ) -> GenesysAudioHookListenerConfig:
        return cls(
            host=(host or app_settings.genesys_audiohook_host).strip() or "0.0.0.0",
            port=port if port is not None else int(app_settings.genesys_audiohook_port),
            path=_normalize_path(path or app_settings.genesys_audiohook_path),
            target_audio_ingest_url=str(app_settings.genesys_audiohook_target_audio_ingest_url).strip(),
            target_event_ingest_url=str(app_settings.genesys_audiohook_target_event_ingest_url).strip()
            or str(app_settings.genesys_target_ingest_url).strip(),
            target_ingest_token=(
                str(app_settings.genesys_audiohook_target_ingest_token).strip()
                or str(app_settings.genesys_target_ingest_token).strip()
                or str(app_settings.realtime_ingest_token).strip()
            ),
            verify_ssl=bool(app_settings.genesys_audiohook_verify_ssl),
            http_timeout_seconds=max(5, int(app_settings.genesys_audiohook_http_timeout_seconds)),
            retry_max_attempts=max(1, int(app_settings.genesys_audiohook_retry_max_attempts)),
            retry_backoff_seconds=max(0.2, float(app_settings.genesys_audiohook_retry_backoff_seconds)),
            flush_interval_ms=max(120, int(app_settings.genesys_audiohook_flush_interval_ms)),
            min_chunk_duration_ms=max(80, int(app_settings.genesys_audiohook_min_chunk_duration_ms)),
            max_chunk_duration_ms=max(120, int(app_settings.genesys_audiohook_max_chunk_duration_ms)),
            status_path=Path(app_settings.genesys_audiohook_status_path),
            dry_run=bool(dry_run),
        )

    def with_overrides(self, **kwargs: object) -> GenesysAudioHookListenerConfig:
        return replace(self, **kwargs)


@dataclass
class AudioHookConnection:
    websocket: WebSocketServerProtocol
    path: str
    connection_id: str
    open_command_id: str = ""
    call_id: str = ""
    sample_rate: int = 8000
    channels: int = 1
    channel_labels: list[str] = field(default_factory=list)
    media_format: str = "PCMU"
    opened: bool = False
    seq_counter: int = 0
    audio_buffer: bytearray = field(default_factory=bytearray)
    audio_packet_count: int = 0
    raw_audio_bytes: int = 0
    last_flush_monotonic: float = field(default_factory=time.monotonic)
    opened_at: datetime = field(default_factory=lambda: datetime.utcnow().replace(tzinfo=timezone.utc))
    end_event_emitted: bool = False


class GenesysAudioHookListener:
    def __init__(self, config: GenesysAudioHookListenerConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "call-analytics-genesys-audiohook/1.0"})
        self._stop_event = asyncio.Event()
        self._status_lock = asyncio.Lock()
        now = _utc_iso_now()
        self._status: dict[str, object] = {
            "state": "initialized",
            "updated_at": now,
            "started_at": now,
            "pid": os.getpid(),
            "host": self.config.host,
            "port": self.config.port,
            "path": self.config.path,
            "dry_run": self.config.dry_run,
            "connection_count": 0,
            "active_connections": 0,
            "forwarded_chunks": 0,
            "forwarded_events": 0,
            "forward_failures": 0,
            "audio_packets": 0,
            "audio_bytes": 0,
            "last_error": "",
            "last_call_id": "",
            "last_media_format": "",
        }

    async def stop(self) -> None:
        self._stop_event.set()
        await self._set_status(state="stopping")

    def run_forever(self) -> None:
        if ws_serve is None:
            raise RuntimeError(
                "websockets is not installed. Install dependencies with: pip install -r requirements.txt"
            )

        try:
            asyncio.run(self._serve())
        except KeyboardInterrupt:
            pass

    async def _serve(self) -> None:
        await self._set_status(state="starting")
        self._validate_required_config()
        await self._persist_status(initial=True)

        logger.info(
            "genesys_audiohook_listener_start host=%s port=%s path=%s target_audio=%s dry_run=%s",
            self.config.host,
            self.config.port,
            self.config.path,
            self.config.target_audio_ingest_url,
            self.config.dry_run,
        )

        async with ws_serve(  # type: ignore[misc]
            self._handle_connection,
            self.config.host,
            self.config.port,
            process_request=self._process_http_request,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        ):
            await self._set_status(state="running")
            while not self._stop_event.is_set():
                await asyncio.sleep(0.75)

        await self._set_status(state="stopped")
        logger.info("genesys_audiohook_listener_stopped")

    async def _set_status(self, **updates: object) -> None:
        async with self._status_lock:
            self._status.update(updates)
            self._status["updated_at"] = _utc_iso_now()
        await self._persist_status()

    async def _increment_status(self, key: str, amount: int = 1, *, persist: bool = True) -> None:
        async with self._status_lock:
            current = int(self._status.get(key) or 0)
            self._status[key] = current + amount
            self._status["updated_at"] = _utc_iso_now()
        if persist:
            await self._persist_status()

    async def _bump_active_connections(self, delta: int) -> None:
        async with self._status_lock:
            active = int(self._status.get("active_connections") or 0) + delta
            self._status["active_connections"] = max(0, active)
            self._status["updated_at"] = _utc_iso_now()
        await self._persist_status()

    async def _persist_status(self, *, initial: bool = False) -> None:
        path = self.config.status_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            async with self._status_lock:
                payload = dict(self._status)
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp_path.replace(path)
        except OSError as exc:
            if initial:
                logger.warning("genesys_audiohook_status_init_write_failed path=%s error=%s", path, exc)
            else:
                logger.debug("genesys_audiohook_status_write_failed path=%s error=%s", path, exc)

    def _validate_required_config(self) -> None:
        if not self.config.target_audio_ingest_url and not self.config.dry_run:
            raise RuntimeError("GENESYS_AUDIOHOOK_TARGET_AUDIO_INGEST_URL is required")
        if not self.config.target_event_ingest_url and not self.config.dry_run:
            raise RuntimeError("GENESYS_AUDIOHOOK_TARGET_EVENT_INGEST_URL is required")

    async def _process_http_request(
        self,
        path: str,
        request_headers: Any,
    ) -> tuple[int, list[tuple[str, str]], bytes] | None:
        request_path = _path_without_query(path)
        if request_path != self.config.path:
            return (
                HTTPStatus.NOT_FOUND,
                [("Content-Type", "application/json")],
                json.dumps({"detail": "Not found"}).encode("utf-8"),
            )

        upgrade = str(request_headers.get("Upgrade") or "").strip().lower()
        if upgrade == "websocket":
            return None

        payload = {
            "ok": True,
            "service": "genesys_audiohook_listener",
            "path": self.config.path,
            "timestamp": _utc_iso_now(),
        }
        return (
            HTTPStatus.OK,
            [("Content-Type", "application/json"), ("Cache-Control", "no-store")],
            json.dumps(payload).encode("utf-8"),
        )

    async def _handle_connection(self, websocket: WebSocketServerProtocol, path: str) -> None:
        request_path = _path_without_query(path)
        if request_path != self.config.path:
            await websocket.close(code=1008, reason="Invalid path")
            return

        connection_id = f"{int(time.time() * 1000)}-{id(websocket)}"
        conn = AudioHookConnection(websocket=websocket, path=path, connection_id=connection_id)
        await self._increment_status("connection_count", 1)
        await self._bump_active_connections(1)

        logger.info("genesys_audiohook_ws_connected connection_id=%s path=%s", connection_id, path)

        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    await self._handle_binary_message(conn, message)
                elif isinstance(message, str):
                    await self._handle_command_payload(conn, message.encode("utf-8"), source="text")
        except Exception as exc:
            await self._set_status(last_error=str(exc))
            logger.warning("genesys_audiohook_ws_error connection_id=%s error=%s", connection_id, exc)
        finally:
            await self._flush_audio_buffer(conn, force=True, reason="socket_closed")
            await self._forward_call_end_event(conn, reason="socket_closed")
            await self._bump_active_connections(-1)
            logger.info("genesys_audiohook_ws_disconnected connection_id=%s", connection_id)

    async def _handle_binary_message(self, conn: AudioHookConnection, payload: bytes) -> None:
        packets = _decode_protocol_packets(payload)
        if not packets:
            return

        for packet_type, packet_payload in packets:
            if packet_type == PACKET_TYPE_COMMAND:
                await self._handle_command_payload(conn, packet_payload, source="binary")
            elif packet_type == PACKET_TYPE_AUDIO:
                await self._handle_audio_payload(conn, packet_payload)
            else:
                logger.debug(
                    "genesys_audiohook_packet_ignored connection_id=%s type=0x%02x bytes=%s",
                    conn.connection_id,
                    packet_type,
                    len(packet_payload),
                )

    async def _handle_command_payload(
        self,
        conn: AudioHookConnection,
        payload: bytes,
        *,
        source: str,
    ) -> None:
        try:
            command = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.debug("genesys_audiohook_command_invalid_json connection_id=%s source=%s", conn.connection_id, source)
            return
        if not isinstance(command, dict):
            return

        command_type = str(command.get("type") or "").strip().lower()
        command_id = str(command.get("id") or "").strip()
        seq = _safe_int(command.get("seq"), default=0)
        if command_id:
            conn.open_command_id = command_id
        if seq > conn.seq_counter:
            conn.seq_counter = seq

        if command_type == "open":
            await self._handle_open_command(conn, command)
            return

        if command_type == "ping":
            await self._send_command(
                conn,
                {
                    "version": "2",
                    "type": "pong",
                    "id": command_id or conn.open_command_id,
                    "seq": seq or conn.seq_counter,
                    "parameters": {},
                },
            )
            return

        if command_type == "close":
            await self._flush_audio_buffer(conn, force=True, reason="close_command")
            await self._forward_call_end_event(conn, reason="close_command")
            await self._send_command(
                conn,
                {
                    "version": "2",
                    "type": "closed",
                    "id": command_id or conn.open_command_id,
                    "seq": seq or conn.seq_counter,
                    "parameters": {},
                },
            )
            await conn.websocket.close(code=1000, reason="closed")
            return

        if command_type in {"disconnect", "error"}:
            await self._flush_audio_buffer(conn, force=True, reason=command_type)
            await self._forward_call_end_event(conn, reason=command_type)
            await conn.websocket.close(code=1011, reason=command_type)
            return

        if command_type == "event":
            await self._forward_event_command(conn, command)
            return

        logger.debug(
            "genesys_audiohook_command_ignored connection_id=%s type=%s",
            conn.connection_id,
            command_type or "unknown",
        )

    async def _handle_open_command(self, conn: AudioHookConnection, command: dict[str, Any]) -> None:
        parameters = command.get("parameters") if isinstance(command.get("parameters"), dict) else {}
        media = command.get("media") if isinstance(command.get("media"), dict) else {}

        media_format, sample_rate, channels, channel_labels = _extract_media_details(media)
        if not media_format:
            media_format = "PCMU"
        if sample_rate <= 0:
            sample_rate = int(app_settings.realtime_audio_default_sample_rate)
        if channels <= 0:
            channels = int(app_settings.realtime_audio_default_channels)

        conn.media_format = media_format
        conn.sample_rate = sample_rate
        conn.channels = channels
        conn.channel_labels = channel_labels or _default_channel_labels(channels)
        conn.call_id = _extract_call_id(command, parameters, conn.path)
        conn.opened = True
        conn.opened_at = datetime.utcnow().replace(tzinfo=timezone.utc)

        await self._set_status(
            last_call_id=conn.call_id,
            last_media_format=conn.media_format,
        )

        opened_payload = {
            "version": "2",
            "type": "opened",
            "id": str(command.get("id") or conn.open_command_id or f"open-{conn.connection_id}"),
            "seq": _safe_int(command.get("seq"), default=conn.seq_counter),
            "parameters": {
                "conversationId": conn.call_id,
            },
            "media": {
                "type": "audio",
                "format": conn.media_format,
                "rate": conn.sample_rate,
                "channels": conn.channel_labels,
            },
        }
        await self._send_command(conn, opened_payload)

        logger.info(
            "genesys_audiohook_opened connection_id=%s call_id=%s format=%s rate=%s channels=%s",
            conn.connection_id,
            conn.call_id or "unknown",
            conn.media_format,
            conn.sample_rate,
            conn.channels,
        )

    async def _handle_audio_payload(self, conn: AudioHookConnection, payload: bytes) -> None:
        if not conn.opened:
            logger.debug("genesys_audiohook_audio_ignored reason=not_opened connection_id=%s", conn.connection_id)
            return

        headers, raw_audio = _parse_audio_headers_and_data(payload)
        if not raw_audio:
            return

        media = headers.get("media") if isinstance(headers.get("media"), dict) else {}
        media_format, sample_rate, channels, channel_labels = _extract_media_details(media)
        if sample_rate > 0:
            conn.sample_rate = sample_rate
        if channels > 0:
            conn.channels = channels
        if channel_labels:
            conn.channel_labels = channel_labels
        if media_format:
            conn.media_format = media_format

        decoded = _decode_to_pcm_s16le(raw_audio, conn.media_format)
        if not decoded:
            logger.debug(
                "genesys_audiohook_audio_decode_unsupported connection_id=%s format=%s",
                conn.connection_id,
                conn.media_format,
            )
            return

        conn.audio_packet_count += 1
        conn.raw_audio_bytes += len(raw_audio)
        conn.audio_buffer.extend(decoded)

        await self._increment_status("audio_packets", 1, persist=False)
        await self._increment_status("audio_bytes", len(raw_audio), persist=False)
        await self._flush_audio_buffer(conn, force=False, reason="streaming")

    async def _forward_event_command(self, conn: AudioHookConnection, command: dict[str, Any]) -> None:
        if not conn.call_id:
            return
        event_type = str(command.get("eventType") or command.get("subType") or "audiohook_event").strip().lower()
        parameters = command.get("parameters") if isinstance(command.get("parameters"), dict) else {}
        text = _extract_event_text(parameters)
        payload = {
            "provider": "genesys_audiohook",
            "call_id": conn.call_id,
            "event_type": event_type or "audiohook_event",
            "speaker": "",
            "text": text,
            "status": "active",
            "timestamp": _utc_iso_now(),
            "metadata": {
                "audiohook_command": command,
                "connection_id": conn.connection_id,
            },
        }
        await self._forward_event_payload(payload)

    async def _forward_call_end_event(self, conn: AudioHookConnection, *, reason: str) -> None:
        if conn.end_event_emitted:
            return
        if not conn.call_id:
            return
        conn.end_event_emitted = True
        payload = {
            "provider": "genesys_audiohook",
            "call_id": conn.call_id,
            "event_type": "call_end",
            "speaker": "",
            "text": "",
            "status": "ended",
            "timestamp": _utc_iso_now(),
            "metadata": {
                "reason": reason,
                "connection_id": conn.connection_id,
            },
        }
        await self._forward_event_payload(payload)

    async def _forward_event_payload(self, payload: dict[str, object]) -> None:
        if self.config.dry_run:
            logger.info(
                "genesys_audiohook_event_dry_run call_id=%s event_type=%s",
                payload.get("call_id"),
                payload.get("event_type"),
            )
            return

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.target_ingest_token:
            headers["X-Cloud-Token"] = self.config.target_ingest_token

        try:
            await asyncio.to_thread(
                self._request_with_retries,
                "POST",
                self.config.target_event_ingest_url,
                headers,
                payload,
            )
            await self._increment_status("forwarded_events", 1)
        except Exception as exc:
            await self._increment_status("forward_failures", 1)
            await self._set_status(last_error=str(exc))
            logger.exception("genesys_audiohook_event_forward_failed error=%s", exc)

    async def _flush_audio_buffer(self, conn: AudioHookConnection, *, force: bool, reason: str) -> None:
        if not conn.audio_buffer:
            return

        bytes_per_second = max(1, conn.sample_rate * conn.channels * 2)
        min_bytes = max(1, int(bytes_per_second * (self.config.min_chunk_duration_ms / 1000.0)))
        max_bytes = max(min_bytes, int(bytes_per_second * (self.config.max_chunk_duration_ms / 1000.0)))

        elapsed_ms = (time.monotonic() - conn.last_flush_monotonic) * 1000.0
        if not force:
            if len(conn.audio_buffer) < min_bytes and elapsed_ms < self.config.flush_interval_ms:
                return

        while conn.audio_buffer:
            if not force and len(conn.audio_buffer) < min_bytes and elapsed_ms < self.config.flush_interval_ms:
                break
            chunk_size = min(len(conn.audio_buffer), max_bytes)
            chunk = bytes(conn.audio_buffer[:chunk_size])
            del conn.audio_buffer[:chunk_size]
            await self._forward_audio_chunk(conn, chunk, reason=reason)
            conn.last_flush_monotonic = time.monotonic()
            elapsed_ms = (time.monotonic() - conn.last_flush_monotonic) * 1000.0
            if not force and len(conn.audio_buffer) < max_bytes:
                break

    async def _forward_audio_chunk(self, conn: AudioHookConnection, chunk: bytes, *, reason: str) -> None:
        if not chunk or not conn.call_id:
            return
        payload = {
            "provider": "genesys_audiohook",
            "call_id": conn.call_id,
            "audio_encoding": "pcm_s16le",
            "sample_rate": conn.sample_rate,
            "channels": conn.channels,
            "audio_b64": base64.b64encode(chunk).decode("ascii"),
            "status": "active",
            "timestamp": _utc_iso_now(),
            "metadata": {
                "connection_id": conn.connection_id,
                "channel_labels": conn.channel_labels,
                "media_format": conn.media_format,
                "flush_reason": reason,
                "audio_packet_count": conn.audio_packet_count,
            },
        }

        if self.config.dry_run:
            logger.info(
                "genesys_audiohook_chunk_dry_run call_id=%s bytes=%s channels=%s",
                conn.call_id,
                len(chunk),
                conn.channels,
            )
            return

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.target_ingest_token:
            headers["X-Cloud-Token"] = self.config.target_ingest_token

        try:
            await asyncio.to_thread(
                self._request_with_retries,
                "POST",
                self.config.target_audio_ingest_url,
                headers,
                payload,
            )
            await self._increment_status("forwarded_chunks", 1)
            await self._set_status(last_call_id=conn.call_id)
        except Exception as exc:
            await self._increment_status("forward_failures", 1)
            await self._set_status(last_error=str(exc))
            logger.exception(
                "genesys_audiohook_chunk_forward_failed call_id=%s bytes=%s error=%s",
                conn.call_id,
                len(chunk),
                exc,
            )

    async def _send_command(self, conn: AudioHookConnection, command: dict[str, object]) -> None:
        frame = _encode_command_packet(command)
        await conn.websocket.send(frame)

    def _request_with_retries(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
    ) -> requests.Response:
        attempts = self.config.retry_max_attempts
        retryable_codes = {408, 429, 500, 502, 503, 504}
        last_exception: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    json=payload,
                    timeout=self.config.http_timeout_seconds,
                    verify=self.config.verify_ssl,
                )
            except requests.RequestException as exc:
                last_exception = exc
                if attempt >= attempts:
                    raise RuntimeError(f"Request failed after retries: {method} {url}") from exc
                delay = self._retry_delay(attempt)
                time.sleep(delay)
                continue

            if response.status_code == 200:
                return response

            if response.status_code in retryable_codes and attempt < attempts:
                delay = self._retry_delay(attempt)
                time.sleep(delay)
                continue

            raise RuntimeError(
                f"Unexpected status from ingest endpoint: {response.status_code} body={response.text[:240]}"
            )

        if last_exception:
            raise RuntimeError(f"Request failed: {method} {url}") from last_exception
        raise RuntimeError(f"Request failed without response: {method} {url}")

    def _retry_delay(self, attempt: int) -> float:
        return self.config.retry_backoff_seconds * (2 ** (attempt - 1))


def _normalize_path(path: str) -> str:
    value = str(path or "/audiohook/ws").strip()
    if not value.startswith("/"):
        value = "/" + value
    if len(value) > 1 and value.endswith("/"):
        value = value[:-1]
    return value


def _path_without_query(path: str) -> str:
    return _normalize_path(urlparse(path).path)


def _utc_iso_now() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decode_protocol_packets(data: bytes) -> list[tuple[int, bytes]]:
    packets: list[tuple[int, bytes]] = []
    offset = 0
    total = len(data)

    while offset + 4 <= total:
        packet_type = data[offset]
        payload_size = (data[offset + 1] << 16) | (data[offset + 2] << 8) | data[offset + 3]
        offset += 4
        if payload_size < 0 or payload_size > MAX_PACKET_PAYLOAD:
            break
        if offset + payload_size > total:
            break
        payload = data[offset : offset + payload_size]
        packets.append((packet_type, payload))
        offset += payload_size

    return packets


def _encode_command_packet(command: dict[str, object]) -> bytes:
    payload = json.dumps(command, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    size = len(payload)
    if size > MAX_PACKET_PAYLOAD:
        raise ValueError("Command payload too large")
    header = bytes([PACKET_TYPE_COMMAND, (size >> 16) & 0xFF, (size >> 8) & 0xFF, size & 0xFF])
    return header + payload


def _parse_audio_headers_and_data(payload: bytes) -> tuple[dict[str, object], bytes]:
    delimiter_index = payload.find(b"\r\n\r\n")
    delimiter_size = 4
    if delimiter_index < 0:
        delimiter_index = payload.find(b"\n\n")
        delimiter_size = 2
    if delimiter_index < 0:
        return {}, payload

    header_blob = payload[:delimiter_index]
    audio = payload[delimiter_index + delimiter_size :]
    headers: dict[str, object] = {}

    for raw_line in header_blob.splitlines():
        if not raw_line:
            continue
        try:
            line = raw_line.decode("iso-8859-1").strip()
        except UnicodeDecodeError:
            continue
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        try:
            headers[key] = json.loads(value)
        except json.JSONDecodeError:
            headers[key] = value

    return headers, audio


def _extract_media_details(media: object) -> tuple[str, int, int, list[str]]:
    if not isinstance(media, dict):
        return "", 0, 0, []

    media_format = str(media.get("format") or "").strip().upper()
    sample_rate = _safe_int(media.get("rate"), default=0)
    channels_raw = media.get("channels")

    channel_labels: list[str] = []
    channels = 0
    if isinstance(channels_raw, list):
        for item in channels_raw:
            if isinstance(item, str):
                label = item.strip()
            elif isinstance(item, dict):
                label = str(item.get("name") or item.get("channel") or "").strip()
            else:
                label = ""
            if label:
                channel_labels.append(label)
        channels = len(channel_labels) or len(channels_raw)
    elif isinstance(channels_raw, (int, float)):
        channels = int(channels_raw)

    return media_format, sample_rate, channels, channel_labels


def _default_channel_labels(channels: int) -> list[str]:
    if channels <= 1:
        return ["mono"]
    if channels == 2:
        return ["external", "internal"]
    return [f"ch{index + 1}" for index in range(channels)]


def _extract_call_id(
    command: dict[str, Any],
    parameters: dict[str, Any],
    path: str,
) -> str:
    candidates = [
        parameters.get("conversationId"),
        parameters.get("conversation_id"),
        parameters.get("callId"),
        parameters.get("call_id"),
        parameters.get("id"),
        command.get("conversationId"),
        command.get("id"),
    ]

    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    for key in ("conversationId", "conversation_id", "callId", "call_id", "id"):
        values = query.get(key) or []
        if values:
            candidates.append(values[0])

    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized

    return f"audiohook-{int(time.time() * 1000)}"


def _extract_event_text(parameters: dict[str, Any]) -> str:
    direct_keys = ("text", "transcript", "utteranceText", "message")
    for key in direct_keys:
        value = parameters.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    events = parameters.get("events")
    if isinstance(events, list):
        for item in events:
            if not isinstance(item, dict):
                continue
            for key in direct_keys:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            nested = item.get("parameters")
            if isinstance(nested, dict):
                for key in direct_keys:
                    value = nested.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()

    return ""


def _decode_to_pcm_s16le(raw_audio: bytes, media_format: str) -> bytes | None:
    normalized = str(media_format or "").strip().upper()
    if not normalized:
        return None

    if normalized in {"PCMU", "MULAW", "MU-LAW", "ULAW"}:
        return audioop.ulaw2lin(raw_audio, 2)

    if normalized in {"PCMA", "A-LAW", "ALAW"}:
        return audioop.alaw2lin(raw_audio, 2)

    if normalized in {"L16LE", "PCM_S16LE", "S16LE"}:
        return raw_audio if len(raw_audio) % 2 == 0 else raw_audio[:-1]

    if normalized in {"L16", "LINEAR16", "PCM_S16BE", "S16BE"}:
        clean = raw_audio if len(raw_audio) % 2 == 0 else raw_audio[:-1]
        return _byteswap_16(clean)

    return None


def _byteswap_16(payload: bytes) -> bytes:
    if not payload:
        return b""
    values = array("h")
    values.frombytes(payload)
    values.byteswap()
    return values.tobytes()
