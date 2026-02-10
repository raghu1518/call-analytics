from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError

from app.services.genesys_connector import GenesysCloudConnector, GenesysConnectorConfig


class Command(BaseCommand):
    help = "Run Genesys Cloud realtime connector (OAuth + subscriptions + websocket forwarding)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Connect and parse Genesys events but do not forward to /api/realtime/events.",
        )
        parser.add_argument(
            "--target-ingest-url",
            type=str,
            default="",
            help="Override target ingest URL for this run.",
        )
        parser.add_argument(
            "--log-level",
            type=str,
            default="INFO",
            help="Connector logger level (DEBUG, INFO, WARNING, ERROR).",
        )

    def handle(self, *args, **options) -> None:
        _ = args
        dry_run = bool(options.get("dry_run"))
        target_ingest_url = str(options.get("target_ingest_url") or "").strip() or None
        log_level = str(options.get("log_level") or "INFO").upper().strip()

        connector_logger = logging.getLogger("app.services.genesys_connector")
        connector_logger.setLevel(getattr(logging, log_level, logging.INFO))

        config = GenesysConnectorConfig.from_settings(
            dry_run=dry_run,
            target_ingest_url=target_ingest_url,
        )
        connector = GenesysCloudConnector(config)

        self.stdout.write(
            self.style.SUCCESS(
                "Starting Genesys connector "
                f"(dry_run={config.dry_run}, target={config.target_ingest_url})"
            )
        )
        try:
            connector.run_forever()
        except KeyboardInterrupt:
            connector.stop()
            self.stdout.write(self.style.WARNING("Genesys connector stopped by user."))
        except Exception as exc:
            raise CommandError(f"Genesys connector failed: {exc}") from exc
