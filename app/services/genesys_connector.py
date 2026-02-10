from __future__ import annotations

import base64
import json
import logging
import os
import re
import ssl
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dateutil import parser as date_parser

from app.config import settings as app_settings

try:
    import websocket  # type: ignore
except Exception:  # pragma: no cover - surfaced at runtime in command
    websocket = None


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenesysConnectorConfig:
    login_base_url: str
    api_base_url: str
    client_id: str
    client_secret: str
    subscription_topics: list[str]
    queue_ids: list[str]
    user_ids: list[str]
    target_ingest_url: str
    target_ingest_token: str
    verify_ssl: bool
    http_timeout_seconds: int
    retry_max_attempts: int
    retry_backoff_seconds: float
    reconnect_delay_seconds: int
    topic_builder_mode: str
    topic_builder_queue_name_filters: list[str]
    topic_builder_user_name_filters: list[str]
    topic_builder_user_email_domain_filters: list[str]
    topic_builder_max_queues: int
    topic_builder_max_users: int
    topic_builder_refresh_seconds: int
    status_path: Path
    dry_run: bool = False

    @classmethod
    def from_settings(
        cls,
        *,
        dry_run: bool = False,
        target_ingest_url: str | None = None,
    ) -> GenesysConnectorConfig:
        return cls(
            login_base_url=_normalize_base_url(app_settings.genesys_login_base_url),
            api_base_url=_normalize_base_url(app_settings.genesys_api_base_url),
            client_id=app_settings.genesys_client_id.strip(),
            client_secret=app_settings.genesys_client_secret.strip(),
            subscription_topics=_split_csv(app_settings.genesys_subscription_topics),
            queue_ids=_split_csv(app_settings.genesys_queue_ids),
            user_ids=_split_csv(app_settings.genesys_user_ids),
            target_ingest_url=(target_ingest_url or app_settings.genesys_target_ingest_url).strip(),
            target_ingest_token=app_settings.genesys_target_ingest_token.strip()
            or app_settings.realtime_ingest_token.strip(),
            verify_ssl=bool(app_settings.genesys_verify_ssl),
            http_timeout_seconds=max(5, int(app_settings.genesys_http_timeout_seconds)),
            retry_max_attempts=max(1, int(app_settings.genesys_retry_max_attempts)),
            retry_backoff_seconds=max(0.2, float(app_settings.genesys_retry_backoff_seconds)),
            reconnect_delay_seconds=max(2, int(app_settings.genesys_reconnect_delay_seconds)),
            topic_builder_mode=str(app_settings.genesys_topic_builder_mode).strip().lower() or "manual",
            topic_builder_queue_name_filters=_split_csv(
                app_settings.genesys_topic_builder_queue_name_filters
            ),
            topic_builder_user_name_filters=_split_csv(
                app_settings.genesys_topic_builder_user_name_filters
            ),
            topic_builder_user_email_domain_filters=_split_csv(
                app_settings.genesys_topic_builder_user_email_domain_filters
            ),
            topic_builder_max_queues=max(0, int(app_settings.genesys_topic_builder_max_queues)),
            topic_builder_max_users=max(0, int(app_settings.genesys_topic_builder_max_users)),
            topic_builder_refresh_seconds=max(
                60, int(app_settings.genesys_topic_builder_refresh_seconds)
            ),
            status_path=Path(app_settings.genesys_connector_status_path),
            dry_run=bool(dry_run),
        )

    def with_overrides(self, **kwargs: object) -> GenesysConnectorConfig:
        return replace(self, **kwargs)


class GenesysCloudConnector:
    def __init__(self, config: GenesysConnectorConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "call-analytics-genesys-connector/1.0"})

        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()
        now = _utc_iso_now()
        self._status: dict[str, object] = {
            "state": "initialized",
            "updated_at": now,
            "started_at": now,
            "pid": os.getpid(),
            "dry_run": self.config.dry_run,
            "topic_builder_mode": self.config.topic_builder_mode,
            "topics_count": 0,
            "forwarded_events": 0,
            "forward_failures": 0,
            "reconnect_count": 0,
            "last_error": "",
            "channel_id": "",
            "websocket_uri": "",
            "token_expires_at": None,
            "last_event_at": None,
            "last_payload_call_id": "",
            "last_payload_type": "",
            "topic_preview": [],
            "topic_builder": {},
        }
        self._last_topic_refresh_at: datetime | None = None
        self._cached_topic_preview: dict[str, object] | None = None
        self._persist_status(initial=True)

    def stop(self) -> None:
        self._stop_event.set()
        self._set_status(state="stopping")

    def _set_status(self, **updates: object) -> None:
        with self._status_lock:
            self._status.update(updates)
            self._status["updated_at"] = _utc_iso_now()
        self._persist_status()

    def _increment_status(self, key: str, amount: int = 1) -> None:
        with self._status_lock:
            current = int(self._status.get(key) or 0)
            self._status[key] = current + amount
            self._status["updated_at"] = _utc_iso_now()
        self._persist_status()

    def _persist_status(self, *, initial: bool = False) -> None:
        path = self.config.status_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._status_lock:
                payload = dict(self._status)
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp_path.replace(path)
        except OSError as exc:
            if initial:
                logger.warning("genesys_status_init_write_failed path=%s error=%s", path, exc)
            else:
                logger.debug("genesys_status_write_failed path=%s error=%s", path, exc)

    def run_forever(self) -> None:
        self._set_status(state="starting")
        try:
            self._validate_required_config()
        except Exception as exc:
            self._set_status(state="error", last_error=str(exc))
            raise
        if websocket is None:
            self._set_status(
                state="error",
                last_error=(
                    "websocket-client is not installed. Install dependencies with: pip install -r requirements.txt"
                ),
            )
            raise RuntimeError(
                "websocket-client is not installed. Install dependencies with: pip install -r requirements.txt"
            )

        logger.info(
            "genesys_connector_start login_base=%s api_base=%s topics=%s target=%s dry_run=%s",
            self.config.login_base_url,
            self.config.api_base_url,
            len(self.config.subscription_topics),
            self.config.target_ingest_url,
            self.config.dry_run,
        )

        while not self._stop_event.is_set():
            try:
                topics_preview = self.build_topics_preview(refresh=False)
                topics = list(topics_preview.get("topics") or [])
                if not topics:
                    raise RuntimeError(
                        "No Genesys topics configured. Set GENESYS_SUBSCRIPTION_TOPICS, "
                        "or configure builder mode with queue/user filters."
                    )

                self._set_status(
                    state="connecting",
                    topics_count=len(topics),
                    topic_preview=topics[:20],
                    topic_builder=topics_preview.get("builder") or {},
                )

                channel = self._create_notification_channel()
                channel_id = str(channel.get("id") or "")
                connect_uri = str(
                    channel.get("connectUri") or channel.get("websocketUri") or ""
                ).strip()
                if not channel_id or not connect_uri:
                    raise RuntimeError("Genesys channel response missing id/connect URI")

                self._set_status(channel_id=channel_id, websocket_uri=connect_uri, state="subscribed")
                self._subscribe_to_topics(channel_id, topics)
                self._run_websocket(connect_uri)
            except Exception as exc:
                logger.exception("genesys_connector_cycle_error error=%s", exc)
                self._set_status(state="error", last_error=str(exc))
                self._increment_status("reconnect_count", 1)
                self._sleep_with_stop(self.config.reconnect_delay_seconds)

        self._set_status(state="stopped")
        logger.info("genesys_connector_stopped")

    def _validate_required_config(self) -> None:
        if not self.config.client_id:
            raise RuntimeError("GENESYS_CLIENT_ID is required")
        if not self.config.client_secret:
            raise RuntimeError("GENESYS_CLIENT_SECRET is required")
        if not self.config.target_ingest_url and not self.config.dry_run:
            raise RuntimeError("GENESYS_TARGET_INGEST_URL is required when not in --dry-run mode")

    def build_topics_preview(self, *, refresh: bool = False) -> dict[str, object]:
        manual_topics = self._build_manual_topics()
        builder_preview = self._build_preset_topics(refresh=refresh)
        preset_topics = builder_preview.get("topics")
        if not isinstance(preset_topics, list):
            preset_topics = []

        merged_topics = sorted({*manual_topics, *[str(topic) for topic in preset_topics if topic]})
        return {
            "topics": merged_topics,
            "manual_topic_count": len(manual_topics),
            "preset_topic_count": len(preset_topics),
            "builder": builder_preview,
        }

    def _build_manual_topics(self) -> list[str]:
        topics: set[str] = set(self.config.subscription_topics)

        for queue_id in self.config.queue_ids:
            topics.add(f"v2.routing.queues.{queue_id}.conversations.calls")
        for user_id in self.config.user_ids:
            topics.add(f"v2.users.{user_id}.conversations.calls")

        return sorted(topic.strip() for topic in topics if topic.strip())

    def _build_preset_topics(self, *, refresh: bool = False) -> dict[str, object]:
        mode = (self.config.topic_builder_mode or "manual").strip().lower()
        if mode in {"manual", "off", "none", ""}:
            return {"mode": mode, "topics": [], "queues": [], "users": []}

        if not refresh and self._cached_topic_preview and not self._should_refresh_builder_topics():
            return self._cached_topic_preview

        include_queues = mode in {"queues", "queue", "queues_users", "users_queues", "all", "org"}
        include_users = mode in {"users", "user", "queues_users", "users_queues", "all", "org"}
        if not include_queues and not include_users:
            include_queues = True
            include_users = True

        selected_queues: list[dict[str, str]] = []
        selected_users: list[dict[str, str]] = []
        topics: set[str] = set()

        if include_queues:
            selected_queues = self._discover_queues()
            for queue in selected_queues:
                queue_id = queue.get("id")
                if queue_id:
                    topics.add(f"v2.routing.queues.{queue_id}.conversations.calls")

        if include_users:
            selected_users = self._discover_users()
            for user in selected_users:
                user_id = user.get("id")
                if user_id:
                    topics.add(f"v2.users.{user_id}.conversations.calls")

        preview = {
            "mode": mode,
            "generated_at": _utc_iso_now(),
            "topics": sorted(topics),
            "queues": selected_queues,
            "users": selected_users,
        }
        self._last_topic_refresh_at = datetime.utcnow()
        self._cached_topic_preview = preview

        logger.info(
            "genesys_topic_builder mode=%s queues=%s users=%s topics=%s",
            mode,
            len(selected_queues),
            len(selected_users),
            len(preview["topics"]),
        )
        return preview

    def _should_refresh_builder_topics(self) -> bool:
        if self._last_topic_refresh_at is None:
            return True
        elapsed = (datetime.utcnow() - self._last_topic_refresh_at).total_seconds()
        return elapsed >= self.config.topic_builder_refresh_seconds

    def _discover_queues(self) -> list[dict[str, str]]:
        filters = [term.lower() for term in self.config.topic_builder_queue_name_filters if term]
        max_items = self.config.topic_builder_max_queues
        if max_items == 0:
            return []

        discovered: list[dict[str, str]] = []
        page_number = 1
        page_size = 100
        while True:
            url = f"{self.config.api_base_url}/api/v2/routing/queues"
            response = self._request(
                "GET",
                url,
                params={"pageSize": page_size, "pageNumber": page_number},
                expected_status=(200,),
            )
            payload = response.json()
            entities = payload.get("entities")
            if not isinstance(entities, list) or not entities:
                break

            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                queue_id = str(entity.get("id") or "").strip()
                name = str(entity.get("name") or "").strip()
                if not queue_id or not name:
                    continue
                if filters and not any(term in name.lower() for term in filters):
                    continue
                discovered.append({"id": queue_id, "name": name})
                if max_items > 0 and len(discovered) >= max_items:
                    return discovered

            page_count = _parse_int(payload.get("pageCount"))
            if page_count and page_number >= page_count:
                break
            if len(entities) < page_size:
                break
            page_number += 1
            if page_number > 50:
                break

        return discovered

    def _discover_users(self) -> list[dict[str, str]]:
        name_filters = [term.lower() for term in self.config.topic_builder_user_name_filters if term]
        domain_filters = [
            term.lower().lstrip("@")
            for term in self.config.topic_builder_user_email_domain_filters
            if term
        ]
        max_items = self.config.topic_builder_max_users
        if max_items == 0:
            return []

        discovered: list[dict[str, str]] = []
        page_number = 1
        page_size = 100
        while True:
            url = f"{self.config.api_base_url}/api/v2/users"
            response = self._request(
                "GET",
                url,
                params={"pageSize": page_size, "pageNumber": page_number, "state": "active"},
                expected_status=(200,),
            )
            payload = response.json()
            entities = payload.get("entities")
            if not isinstance(entities, list) or not entities:
                break

            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                user_id = str(entity.get("id") or "").strip()
                name = str(entity.get("name") or "").strip()
                email = str(entity.get("email") or "").strip().lower()
                if not user_id:
                    continue
                if name_filters and not any(term in name.lower() for term in name_filters):
                    continue
                if domain_filters and not any(email.endswith(f"@{domain}") for domain in domain_filters):
                    continue
                discovered.append({"id": user_id, "name": name, "email": email})
                if max_items > 0 and len(discovered) >= max_items:
                    return discovered

            page_count = _parse_int(payload.get("pageCount"))
            if page_count and page_number >= page_count:
                break
            if len(entities) < page_size:
                break
            page_number += 1
            if page_number > 50:
                break

        return discovered

    def _build_topics(self) -> list[str]:
        preview = self.build_topics_preview(refresh=False)
        topics = preview.get("topics")
        if isinstance(topics, list):
            return [str(topic) for topic in topics if str(topic).strip()]
        return []

    def _run_websocket(self, connect_uri: str) -> None:
        assert websocket is not None

        close_info: dict[str, object] = {"code": None, "reason": ""}

        def on_open(ws_app: object) -> None:
            _ = ws_app
            self._set_status(state="running", last_error="")
            logger.info("genesys_ws_connected uri=%s", connect_uri)

        def on_message(ws_app: object, message: str) -> None:
            _ = ws_app
            self._handle_notification_message(message)

        def on_error(ws_app: object, error: object) -> None:
            _ = ws_app
            self._set_status(last_error=str(error or "websocket_error"))
            logger.warning("genesys_ws_error error=%s", error)

        def on_close(ws_app: object, status_code: object, message: object) -> None:
            _ = ws_app
            close_info["code"] = status_code
            close_info["reason"] = message
            self._set_status(state="reconnecting")
            logger.warning(
                "genesys_ws_closed code=%s reason=%s",
                status_code,
                message,
            )

        sslopt: dict[str, object] = {}
        sslopt["cert_reqs"] = ssl.CERT_REQUIRED if self.config.verify_ssl else ssl.CERT_NONE

        ws_app = websocket.WebSocketApp(
            connect_uri,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        ws_app.run_forever(ping_interval=20, ping_timeout=10, sslopt=sslopt)

        if self._stop_event.is_set():
            return

        logger.info(
            "genesys_ws_reconnect_scheduled delay_seconds=%s code=%s reason=%s",
            self.config.reconnect_delay_seconds,
            close_info.get("code"),
            close_info.get("reason"),
        )
        self._increment_status("reconnect_count", 1)
        self._sleep_with_stop(self.config.reconnect_delay_seconds)

    def _handle_notification_message(self, message: str) -> None:
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            logger.debug("genesys_message_ignored reason=invalid_json")
            return

        notifications = _flatten_notifications(parsed)
        total_payloads = 0
        for notification in notifications:
            for payload in self._map_notification_to_payloads(notification):
                try:
                    self._forward_payload(payload)
                    total_payloads += 1
                    self._set_status(
                        last_event_at=_utc_iso_now(),
                        last_payload_call_id=str(payload.get("call_id") or ""),
                        last_payload_type=str(payload.get("event_type") or ""),
                    )
                except Exception as exc:
                    logger.exception(
                        "genesys_payload_forward_failed call_id=%s event_type=%s error=%s",
                        payload.get("call_id"),
                        payload.get("event_type"),
                        exc,
                    )
                    self._increment_status("forward_failures", 1)

        if total_payloads:
            self._increment_status("forwarded_events", total_payloads)
            logger.debug("genesys_message_forwarded payloads=%s", total_payloads)

    def _map_notification_to_payloads(
        self,
        notification: dict[str, Any],
    ) -> list[dict[str, object]]:
        topic = str(notification.get("topicName") or notification.get("topic") or "").strip()
        if not topic:
            return []
        if topic.endswith("channel.metadata"):
            return []

        event_body = notification.get("eventBody")
        if not isinstance(event_body, dict):
            event_body = {}

        call_id = self._extract_call_id(topic, event_body)
        if not call_id:
            return []

        event_type = self._extract_event_type(topic, event_body)
        status = self._extract_status(event_type, event_body)
        sentiment = self._extract_sentiment(event_body)
        confidence = self._extract_confidence(event_body)
        occurred_at = self._extract_occurred_at(notification, event_body)
        speaker = self._extract_speaker(event_body)

        text_records = self._extract_text_records(event_body)
        if not text_records:
            text_records = [{"text": "", "speaker": speaker, "source": "topic_only"}]

        payloads: list[dict[str, object]] = []
        for record in text_records[:6]:
            text = str(record.get("text") or "").strip()
            record_speaker = str(record.get("speaker") or speaker or "").strip().lower()
            metadata = {
                "genesys_topic": topic,
                "genesys_source": str(record.get("source") or "event"),
                "genesys_event_keys": sorted(event_body.keys())[:40],
            }
            metadata.update(_extract_monitoring_metrics(event_body))

            payloads.append(
                {
                    "provider": "genesys_cloud",
                    "call_id": call_id,
                    "event_type": event_type,
                    "speaker": record_speaker,
                    "text": text,
                    "sentiment": sentiment,
                    "confidence": confidence,
                    "status": status,
                    "timestamp": occurred_at,
                    "agent_id": self._extract_agent_id(event_body),
                    "customer_id": self._extract_customer_id(event_body),
                    "metadata": metadata,
                }
            )

        return payloads

    def _extract_call_id(self, topic: str, event_body: dict[str, Any]) -> str:
        candidates = [
            event_body.get("conversationId"),
            event_body.get("conversation_id"),
            event_body.get("id"),
        ]

        conversation = event_body.get("conversation")
        if isinstance(conversation, dict):
            candidates.append(conversation.get("id"))
            candidates.append(conversation.get("conversationId"))

        for value in candidates:
            normalized = str(value or "").strip()
            if normalized:
                return normalized

        match = re.search(r"conversations\.([a-f0-9-]{16,})", topic, flags=re.IGNORECASE)
        if match:
            return match.group(1)

        return ""

    def _extract_event_type(self, topic: str, event_body: dict[str, Any]) -> str:
        explicit = str(event_body.get("eventType") or event_body.get("type") or "").strip().lower()
        if explicit:
            return explicit
        parts = [part for part in topic.split(".") if part]
        if parts:
            return parts[-1].lower()
        return "transcript"

    def _extract_status(self, event_type: str, event_body: dict[str, Any]) -> str:
        raw = str(
            event_body.get("status")
            or event_body.get("state")
            or event_body.get("conversationState")
            or ""
        ).strip().lower()
        if raw:
            if any(token in raw for token in ("disconnect", "terminated", "ended", "complete", "closed")):
                return "ended"
            return "active"
        if any(token in event_type for token in ("disconnect", "terminate", "end", "complete")):
            return "ended"
        return "active"

    def _extract_occurred_at(
        self,
        notification: dict[str, Any],
        event_body: dict[str, Any],
    ) -> str:
        for key in ("eventTime", "timestamp", "eventDate", "createdDate", "startTime"):
            value = event_body.get(key)
            parsed = _parse_datetime(value)
            if parsed is not None:
                return parsed.isoformat()

        metadata = notification.get("metadata")
        if isinstance(metadata, dict):
            parsed = _parse_datetime(metadata.get("messageTime"))
            if parsed is not None:
                return parsed.isoformat()

        return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

    def _extract_speaker(self, event_body: dict[str, Any]) -> str:
        for key in ("speaker", "speakerType", "participantPurpose", "purpose", "role"):
            value = str(event_body.get(key) or "").strip().lower()
            if value:
                return _normalize_speaker(value)

        participants = event_body.get("participants")
        if isinstance(participants, list):
            for participant in participants:
                if not isinstance(participant, dict):
                    continue
                purpose = str(participant.get("purpose") or participant.get("participantPurpose") or "").strip()
                state = str(participant.get("state") or "").lower()
                if not purpose:
                    continue
                if state in {"connected", "alerting"}:
                    return _normalize_speaker(purpose)
        return ""

    def _extract_agent_id(self, event_body: dict[str, Any]) -> str:
        for key in ("agentId", "agent_id", "userId"):
            value = str(event_body.get(key) or "").strip()
            if value:
                return value

        participants = event_body.get("participants")
        if isinstance(participants, list):
            for participant in participants:
                if not isinstance(participant, dict):
                    continue
                purpose = str(participant.get("purpose") or "").lower()
                if purpose not in {"agent", "user"}:
                    continue
                value = str(participant.get("userId") or participant.get("id") or "").strip()
                if value:
                    return value
        return ""

    def _extract_customer_id(self, event_body: dict[str, Any]) -> str:
        for key in ("customerId", "externalContactId", "customer_id"):
            value = str(event_body.get(key) or "").strip()
            if value:
                return value

        participants = event_body.get("participants")
        if isinstance(participants, list):
            for participant in participants:
                if not isinstance(participant, dict):
                    continue
                purpose = str(participant.get("purpose") or "").lower()
                if purpose not in {"customer", "external"}:
                    continue
                value = str(participant.get("id") or participant.get("externalContactId") or "").strip()
                if value:
                    return value
        return ""

    def _extract_text_records(self, event_body: dict[str, Any]) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []

        transcripts = event_body.get("transcripts")
        if isinstance(transcripts, list):
            for entry in transcripts:
                if not isinstance(entry, dict):
                    continue
                text = str(
                    entry.get("text")
                    or entry.get("transcript")
                    or entry.get("utteranceText")
                    or ""
                ).strip()
                if not text:
                    continue
                speaker = str(
                    entry.get("speaker")
                    or entry.get("participantPurpose")
                    or entry.get("role")
                    or ""
                ).strip()
                records.append(
                    {
                        "text": text,
                        "speaker": _normalize_speaker(speaker),
                        "source": "transcripts",
                    }
                )

        utterances = event_body.get("utterances")
        if isinstance(utterances, list):
            for entry in utterances:
                if not isinstance(entry, dict):
                    continue
                text = str(entry.get("text") or entry.get("utteranceText") or "").strip()
                if not text:
                    continue
                speaker = str(entry.get("speaker") or entry.get("role") or "").strip()
                records.append(
                    {
                        "text": text,
                        "speaker": _normalize_speaker(speaker),
                        "source": "utterances",
                    }
                )

        for key in ("text", "transcript", "utteranceText", "message"):
            value = event_body.get(key)
            if isinstance(value, str) and value.strip():
                records.append({"text": value.strip(), "speaker": "", "source": key})
            elif isinstance(value, dict):
                nested_text = str(value.get("text") or value.get("body") or "").strip()
                if nested_text:
                    records.append({"text": nested_text, "speaker": "", "source": key})

        deduped: list[dict[str, object]] = []
        seen: set[str] = set()
        for record in records:
            text = str(record.get("text") or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    def _extract_sentiment(self, event_body: dict[str, Any]) -> float | None:
        direct_candidates = [
            event_body.get("sentiment"),
            event_body.get("sentimentScore"),
            event_body.get("overallSentiment"),
            event_body.get("sentiment_score"),
        ]
        for candidate in direct_candidates:
            parsed = _parse_sentiment(candidate)
            if parsed is not None:
                return parsed

        sentiment = event_body.get("sentiment")
        if isinstance(sentiment, dict):
            for key in ("score", "overall", "value"):
                parsed = _parse_sentiment(sentiment.get(key))
                if parsed is not None:
                    return parsed

        return None

    def _extract_confidence(self, event_body: dict[str, Any]) -> float | None:
        direct_candidates = [
            event_body.get("confidence"),
            event_body.get("confidenceScore"),
            event_body.get("sentimentConfidence"),
        ]
        sentiment = event_body.get("sentiment")
        if isinstance(sentiment, dict):
            direct_candidates.extend(
                [sentiment.get("confidence"), sentiment.get("confidenceScore")]
            )

        for candidate in direct_candidates:
            parsed = _parse_float(candidate)
            if parsed is None:
                continue
            return max(0.0, min(1.0, parsed))
        return None

    def _forward_payload(self, payload: dict[str, object]) -> None:
        if self.config.dry_run:
            logger.info(
                "genesys_payload_dry_run call_id=%s event_type=%s speaker=%s text_len=%s",
                payload.get("call_id"),
                payload.get("event_type"),
                payload.get("speaker"),
                len(str(payload.get("text") or "")),
            )
            return

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.target_ingest_token:
            headers["X-Cloud-Token"] = self.config.target_ingest_token

        response = self._request(
            "POST",
            self.config.target_ingest_url,
            include_auth=False,
            headers=headers,
            json_body=payload,
            expected_status=(200,),
        )
        logger.debug(
            "genesys_payload_forwarded status=%s call_id=%s event_type=%s",
            response.status_code,
            payload.get("call_id"),
            payload.get("event_type"),
        )

    def _create_notification_channel(self) -> dict[str, Any]:
        url = f"{self.config.api_base_url}/api/v2/notifications/channels"
        response = self._request("POST", url, json_body={}, expected_status=(200, 201))
        payload = response.json()
        self._set_status(channel_id=str(payload.get("id") or ""))
        logger.info(
            "genesys_channel_created channel_id=%s expires=%s",
            payload.get("id"),
            payload.get("expires"),
        )
        return payload

    def _subscribe_to_topics(self, channel_id: str, topics: list[str]) -> None:
        url = f"{self.config.api_base_url}/api/v2/notifications/channels/{channel_id}/subscriptions"
        body = [{"id": topic} for topic in topics]
        response = self._request("POST", url, json_body=body, expected_status=(200,))
        self._set_status(state="subscribed", topics_count=len(topics))
        logger.info(
            "genesys_channel_subscribed channel_id=%s topics=%s response_status=%s",
            channel_id,
            len(topics),
            response.status_code,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        include_auth: bool = True,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | None = None,
        data: object | None = None,
        json_body: object | None = None,
        expected_status: tuple[int, ...] = (200,),
    ) -> requests.Response:
        attempts = self.config.retry_max_attempts
        retryable_codes = {408, 429, 500, 502, 503, 504}
        last_exception: Exception | None = None

        for attempt in range(1, attempts + 1):
            req_headers: dict[str, str] = {}
            if include_auth:
                req_headers.update(self._auth_headers())
            if headers:
                req_headers.update(headers)

            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    headers=req_headers,
                    params=params,
                    data=data,
                    json=json_body,
                    timeout=self.config.http_timeout_seconds,
                    verify=self.config.verify_ssl,
                )
            except requests.RequestException as exc:
                last_exception = exc
                if attempt >= attempts:
                    raise RuntimeError(f"Request failed after retries: {method} {url}") from exc
                delay = self._retry_delay(attempt)
                logger.warning(
                    "genesys_http_retry reason=network method=%s url=%s attempt=%s/%s delay=%.2f",
                    method.upper(),
                    url,
                    attempt,
                    attempts,
                    delay,
                )
                self._sleep_with_stop(delay)
                continue

            if response.status_code in expected_status:
                return response

            if response.status_code == 401 and include_auth:
                self._invalidate_token()

            should_retry = response.status_code in retryable_codes and attempt < attempts
            if should_retry:
                delay = self._retry_delay(attempt)
                logger.warning(
                    "genesys_http_retry reason=status method=%s url=%s status=%s attempt=%s/%s delay=%.2f",
                    method.upper(),
                    url,
                    response.status_code,
                    attempt,
                    attempts,
                    delay,
                )
                self._sleep_with_stop(delay)
                continue

            snippet = _response_snippet(response.text)
            raise RuntimeError(
                f"Request failed: {method.upper()} {url} status={response.status_code} body={snippet}"
            )

        if last_exception is not None:
            raise RuntimeError(f"Request failed: {method} {url}") from last_exception
        raise RuntimeError(f"Request failed: {method} {url}")

    def _auth_headers(self) -> dict[str, str]:
        token = self._get_access_token()
        return {"Authorization": f"Bearer {token}"}

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = None

    def _get_access_token(self) -> str:
        if self._token and self._token_expires_at:
            if datetime.utcnow() < (self._token_expires_at - timedelta(seconds=30)):
                return self._token

        url = f"{self.config.login_base_url}/oauth/token"
        credentials = f"{self.config.client_id}:{self.config.client_secret}".encode("utf-8")
        encoded = base64.b64encode(credentials).decode("utf-8")
        headers = {
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        response = self._request(
            "POST",
            url,
            include_auth=False,
            headers=headers,
            data={"grant_type": "client_credentials"},
            expected_status=(200,),
        )
        payload = response.json()
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("Genesys OAuth response missing access_token")

        expires_in = int(payload.get("expires_in") or 3600)
        self._token = token
        self._token_expires_at = datetime.utcnow() + timedelta(seconds=max(60, expires_in))
        self._set_status(token_expires_at=self._token_expires_at.replace(tzinfo=timezone.utc).isoformat())
        logger.info("genesys_oauth_token_refreshed expires_in=%s", expires_in)
        return token

    def _retry_delay(self, attempt: int) -> float:
        return self.config.retry_backoff_seconds * max(1, attempt - 1)

    def _sleep_with_stop(self, delay_seconds: float) -> None:
        if delay_seconds <= 0:
            return
        end_time = time.time() + delay_seconds
        while time.time() < end_time:
            if self._stop_event.is_set():
                return
            time.sleep(0.2)


def _normalize_base_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _flatten_notifications(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        notifications = payload.get("notifications")
        if isinstance(notifications, list):
            return [item for item in notifications if isinstance(item, dict)]
        return [payload]
    return []


def _normalize_speaker(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""
    if normalized in {"agent", "user", "acd"}:
        return "agent"
    if normalized in {"customer", "external", "client"}:
        return "customer"
    return normalized


def _parse_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_sentiment(value: object) -> float | None:
    parsed = _parse_float(value)
    if parsed is not None:
        return max(-1.0, min(1.0, parsed))

    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"negative", "neg"}:
        return -0.7
    if normalized in {"neutral"}:
        return 0.0
    if normalized in {"positive", "pos"}:
        return 0.7
    return None


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = date_parser.parse(text)
        except (ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _extract_monitoring_metrics(event_body: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}

    silence = (
        event_body.get("deadAirSeconds")
        or event_body.get("silenceSeconds")
        or event_body.get("dead_air_seconds")
    )
    if silence is not None:
        parsed_silence = _parse_float(silence)
        if parsed_silence is not None:
            metrics["metrics"] = {"dead_air_seconds": max(0.0, parsed_silence)}
    return metrics


def _response_snippet(text: str, max_len: int = 240) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


def _utc_iso_now() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
