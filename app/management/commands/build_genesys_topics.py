from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from app.services.genesys_connector import GenesysCloudConnector, GenesysConnectorConfig


class Command(BaseCommand):
    help = "Build Genesys subscription topic presets from org queues/users."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--mode",
            type=str,
            default="",
            help="Builder mode: queues_users (default), queues, users, manual/off.",
        )
        parser.add_argument(
            "--queue-filter",
            action="append",
            default=[],
            help="Queue name contains filter (repeatable).",
        )
        parser.add_argument(
            "--user-filter",
            action="append",
            default=[],
            help="User display name contains filter (repeatable).",
        )
        parser.add_argument(
            "--email-domain",
            action="append",
            default=[],
            help="User email domain filter (repeatable), e.g. company.com",
        )
        parser.add_argument(
            "--max-queues",
            type=int,
            default=None,
            help="Maximum queues to include.",
        )
        parser.add_argument(
            "--max-users",
            type=int,
            default=None,
            help="Maximum users to include.",
        )
        parser.add_argument(
            "--output-file",
            type=str,
            default="",
            help="Optional file path to write full JSON preview.",
        )
        parser.add_argument(
            "--as-env",
            action="store_true",
            help="Print only GENESYS_SUBSCRIPTION_TOPICS=... value.",
        )

    def handle(self, *args, **options) -> None:
        _ = args
        mode = str(options.get("mode") or "").strip().lower()
        queue_filters = [str(item).strip() for item in options.get("queue_filter") or [] if str(item).strip()]
        user_filters = [str(item).strip() for item in options.get("user_filter") or [] if str(item).strip()]
        email_domains = [str(item).strip().lstrip("@") for item in options.get("email_domain") or [] if str(item).strip()]
        max_queues = options.get("max_queues")
        max_users = options.get("max_users")
        output_file = str(options.get("output_file") or "").strip()
        as_env = bool(options.get("as_env"))

        base_config = GenesysConnectorConfig.from_settings(dry_run=True)
        override_data: dict[str, object] = {}
        if mode:
            override_data["topic_builder_mode"] = mode
        if queue_filters:
            override_data["topic_builder_queue_name_filters"] = queue_filters
        if user_filters:
            override_data["topic_builder_user_name_filters"] = user_filters
        if email_domains:
            override_data["topic_builder_user_email_domain_filters"] = email_domains
        if max_queues is not None:
            override_data["topic_builder_max_queues"] = max(0, int(max_queues))
        if max_users is not None:
            override_data["topic_builder_max_users"] = max(0, int(max_users))

        config = base_config.with_overrides(**override_data) if override_data else base_config
        connector = GenesysCloudConnector(config)
        try:
            preview = connector.build_topics_preview(refresh=True)
        except Exception as exc:
            raise CommandError(f"Unable to build topic preset: {exc}") from exc

        topics = preview.get("topics")
        if not isinstance(topics, list):
            topics = []

        if as_env:
            env_value = ",".join(str(topic) for topic in topics)
            self.stdout.write(f"GENESYS_SUBSCRIPTION_TOPICS={env_value}")
        else:
            response_payload = {
                "mode": config.topic_builder_mode,
                "manual_topic_count": preview.get("manual_topic_count"),
                "preset_topic_count": preview.get("preset_topic_count"),
                "total_topics": len(topics),
                "builder": preview.get("builder"),
                "topics": topics,
            }
            self.stdout.write(json.dumps(response_payload, indent=2))

        if output_file:
            target = Path(output_file)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(preview, indent=2), encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote topic preview to {target}"))
