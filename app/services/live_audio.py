from __future__ import annotations

import io
import json
import logging
import re
import threading
import wave
from datetime import datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


class LiveAudioBufferService:
    """Stores rolling PCM chunks per call and exposes WAV render output."""

    def __init__(
        self,
        base_dir: Path,
        window_seconds: int = 240,
        max_chunk_bytes: int = 2_000_000,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.window_seconds = max(30, int(window_seconds))
        self.max_chunk_bytes = max(8_192, int(max_chunk_bytes))
        self._lock = threading.Lock()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def append_pcm_chunk(
        self,
        *,
        call_id: str,
        pcm_bytes: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int = 2,
        chunk_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        if not pcm_bytes:
            raise ValueError("Empty audio chunk")
        if len(pcm_bytes) > self.max_chunk_bytes:
            raise ValueError("Audio chunk exceeds max size")
        if sample_rate <= 0:
            raise ValueError("Invalid sample_rate")
        if channels <= 0:
            raise ValueError("Invalid channels")
        if sample_width <= 0:
            raise ValueError("Invalid sample_width")

        safe_call_id = self._safe_call_id(call_id)
        timestamp = occurred_at or datetime.utcnow()
        with self._lock:
            call_dir = self.base_dir / safe_call_id
            call_dir.mkdir(parents=True, exist_ok=True)
            state_path = call_dir / "state.json"
            state = self._load_state(state_path, call_id)

            if self._audio_format_changed(state, sample_rate, channels, sample_width):
                self._reset_call_dir(call_dir)
                state = self._new_state(call_id, sample_rate, channels, sample_width)

            seq = int(state.get("next_seq") or 1)
            persisted_chunk_id = (chunk_id or f"{int(timestamp.timestamp() * 1000)}_{seq}").strip()
            chunk_name = f"{seq:09d}_{persisted_chunk_id}.pcm"
            chunk_path = call_dir / chunk_name
            chunk_path.write_bytes(pcm_bytes)

            bytes_per_sample = channels * sample_width
            sample_count = max(1, len(pcm_bytes) // bytes_per_sample)
            chunk_meta = {
                "id": persisted_chunk_id,
                "file": chunk_name,
                "samples": sample_count,
                "bytes": len(pcm_bytes),
                "occurred_at": timestamp.isoformat() + "Z",
            }
            chunks = list(state.get("chunks") or [])
            chunks.append(chunk_meta)

            max_samples = self.window_seconds * sample_rate
            total_samples = int(state.get("total_samples") or 0) + sample_count
            while chunks and total_samples > max_samples and len(chunks) > 1:
                dropped = chunks.pop(0)
                total_samples -= int(dropped.get("samples") or 0)
                dropped_file = call_dir / str(dropped.get("file") or "")
                try:
                    if dropped_file.exists():
                        dropped_file.unlink()
                except OSError:
                    logger.debug("live_audio_chunk_cleanup_failed path=%s", dropped_file)

            state["chunks"] = chunks
            state["total_samples"] = max(0, total_samples)
            state["next_seq"] = seq + 1
            state["sample_rate"] = sample_rate
            state["channels"] = channels
            state["sample_width"] = sample_width
            state["updated_at"] = _utcnow_iso()
            state["last_chunk_id"] = persisted_chunk_id
            state_path.write_text(json.dumps(state), encoding="utf-8")

            return self._state_summary(call_id, state)

    def get_state(self, call_id: str) -> dict[str, Any]:
        safe_call_id = self._safe_call_id(call_id)
        with self._lock:
            state_path = self.base_dir / safe_call_id / "state.json"
            if not state_path.exists():
                return self._state_summary(call_id, None)
            state = self._load_state(state_path, call_id)
            return self._state_summary(call_id, state)

    def get_wav_bytes(self, call_id: str, max_seconds: int | None = None) -> bytes | None:
        safe_call_id = self._safe_call_id(call_id)
        with self._lock:
            call_dir = self.base_dir / safe_call_id
            state_path = call_dir / "state.json"
            if not state_path.exists():
                return None
            state = self._load_state(state_path, call_id)
            chunks = list(state.get("chunks") or [])
            if not chunks:
                return None

            sample_rate = int(state.get("sample_rate") or 0)
            channels = int(state.get("channels") or 0)
            sample_width = int(state.get("sample_width") or 0)
            if sample_rate <= 0 or channels <= 0 or sample_width <= 0:
                return None

            pcm_parts: list[bytes] = []
            for chunk in chunks:
                file_name = str(chunk.get("file") or "")
                if not file_name:
                    continue
                path = call_dir / file_name
                if not path.exists():
                    continue
                try:
                    pcm_parts.append(path.read_bytes())
                except OSError:
                    logger.debug("live_audio_chunk_read_failed path=%s", path)

            if not pcm_parts:
                return None

            pcm_payload = b"".join(pcm_parts)
            bytes_per_second = sample_rate * channels * sample_width
            if max_seconds and max_seconds > 0 and bytes_per_second > 0:
                max_bytes = bytes_per_second * int(max_seconds)
                if len(pcm_payload) > max_bytes:
                    pcm_payload = pcm_payload[-max_bytes:]

            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav_file:
                wav_file.setnchannels(channels)
                wav_file.setsampwidth(sample_width)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(pcm_payload)
            return buffer.getvalue()

    def _audio_format_changed(
        self,
        state: dict[str, Any],
        sample_rate: int,
        channels: int,
        sample_width: int,
    ) -> bool:
        if not state.get("chunks"):
            return False
        return (
            int(state.get("sample_rate") or 0) != sample_rate
            or int(state.get("channels") or 0) != channels
            or int(state.get("sample_width") or 0) != sample_width
        )

    def _reset_call_dir(self, call_dir: Path) -> None:
        for path in call_dir.glob("*.pcm"):
            try:
                path.unlink()
            except OSError:
                logger.debug("live_audio_chunk_delete_failed path=%s", path)
        state_path = call_dir / "state.json"
        try:
            if state_path.exists():
                state_path.unlink()
        except OSError:
            logger.debug("live_audio_state_delete_failed path=%s", state_path)

    def _load_state(self, state_path: Path, call_id: str) -> dict[str, Any]:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        if not isinstance(state, dict):
            state = {}

        sample_rate = int(state.get("sample_rate") or 0)
        channels = int(state.get("channels") or 0)
        sample_width = int(state.get("sample_width") or 0)
        if sample_rate <= 0 or channels <= 0 or sample_width <= 0:
            state = self._new_state(call_id, 16000, 1, 2)
        else:
            state.setdefault("call_id", call_id)
            state.setdefault("window_seconds", self.window_seconds)
            state.setdefault("chunks", [])
            state.setdefault("total_samples", 0)
            state.setdefault("next_seq", 1)
            state.setdefault("updated_at", _utcnow_iso())
            state.setdefault("last_chunk_id", "")
        return state

    def _new_state(
        self,
        call_id: str,
        sample_rate: int,
        channels: int,
        sample_width: int,
    ) -> dict[str, Any]:
        return {
            "call_id": call_id,
            "window_seconds": self.window_seconds,
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
            "chunks": [],
            "total_samples": 0,
            "next_seq": 1,
            "updated_at": _utcnow_iso(),
            "last_chunk_id": "",
        }

    def _state_summary(self, call_id: str, state: dict[str, Any] | None) -> dict[str, Any]:
        if not state:
            return {
                "call_id": call_id,
                "available": False,
                "duration_seconds": 0.0,
                "sample_rate": None,
                "channels": None,
                "sample_width": None,
                "chunk_count": 0,
                "updated_at": None,
                "last_chunk_id": "",
                "window_seconds": self.window_seconds,
            }

        sample_rate = int(state.get("sample_rate") or 0)
        total_samples = int(state.get("total_samples") or 0)
        duration_seconds = round(total_samples / sample_rate, 3) if sample_rate > 0 else 0.0
        chunks = list(state.get("chunks") or [])
        return {
            "call_id": str(state.get("call_id") or call_id),
            "available": bool(chunks),
            "duration_seconds": duration_seconds,
            "sample_rate": sample_rate if sample_rate > 0 else None,
            "channels": int(state.get("channels") or 0) or None,
            "sample_width": int(state.get("sample_width") or 0) or None,
            "chunk_count": len(chunks),
            "updated_at": str(state.get("updated_at") or ""),
            "last_chunk_id": str(state.get("last_chunk_id") or ""),
            "window_seconds": int(state.get("window_seconds") or self.window_seconds),
        }

    def _safe_call_id(self, call_id: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(call_id or "").strip())
        cleaned = cleaned.strip("._")
        return cleaned[:96] or "call"
