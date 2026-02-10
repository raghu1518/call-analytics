from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError

from app.services.genesys_audiohook_listener import (
    GenesysAudioHookListener,
    GenesysAudioHookListenerConfig,
)


class Command(BaseCommand):
    help = (
        "Run Genesys AudioHook listener (listen-only websocket server that receives call media "
        "and forwards chunks to /api/realtime/audio/chunk)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Accept AudioHook connections and decode packets but do not forward to ingest APIs.",
        )
        parser.add_argument(
            "--host",
            type=str,
            default="",
            help="Override listener host for this run.",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=0,
            help="Override listener port for this run.",
        )
        parser.add_argument(
            "--path",
            type=str,
            default="",
            help="Override websocket path for this run (example: /audiohook/ws).",
        )
        parser.add_argument(
            "--log-level",
            type=str,
            default="INFO",
            help="Listener logger level (DEBUG, INFO, WARNING, ERROR).",
        )

    def handle(self, *args, **options) -> None:
        _ = args
        dry_run = bool(options.get("dry_run"))
        host = str(options.get("host") or "").strip() or None
        port = int(options.get("port") or 0) or None
        path = str(options.get("path") or "").strip() or None
        log_level = str(options.get("log_level") or "INFO").upper().strip()

        listener_logger = logging.getLogger("app.services.genesys_audiohook_listener")
        listener_logger.setLevel(getattr(logging, log_level, logging.INFO))

        config = GenesysAudioHookListenerConfig.from_settings(
            dry_run=dry_run,
            host=host,
            port=port,
            path=path,
        )
        listener = GenesysAudioHookListener(config)

        self.stdout.write(
            self.style.SUCCESS(
                "Starting Genesys AudioHook listener "
                f"(dry_run={config.dry_run}, host={config.host}, port={config.port}, path={config.path})"
            )
        )
        try:
            listener.run_forever()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Genesys AudioHook listener stopped by user."))
        except Exception as exc:
            raise CommandError(f"Genesys AudioHook listener failed: {exc}") from exc
