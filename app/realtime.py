from __future__ import annotations

import json
import queue
import threading
from typing import Any


class RealtimeEventBus:
    """In-process pub/sub bus for SSE clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._subscribers: dict[int, queue.Queue[str]] = {}

    def subscribe(self) -> tuple[int, queue.Queue[str]]:
        with self._lock:
            subscriber_id = self._next_id
            self._next_id += 1
            self._subscribers[subscriber_id] = queue.Queue(maxsize=200)
            return subscriber_id, self._subscribers[subscriber_id]

    def unsubscribe(self, subscriber_id: int) -> None:
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def publish(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, default=str)
        with self._lock:
            subscribers = list(self._subscribers.values())

        stale: list[queue.Queue[str]] = []
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(encoded)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(encoded)
                except Exception:
                    stale.append(subscriber)

        if stale:
            with self._lock:
                stale_ids = {
                    key
                    for key, value in self._subscribers.items()
                    if value in stale
                }
                for key in stale_ids:
                    self._subscribers.pop(key, None)


event_bus = RealtimeEventBus()
