"""
Minimal in-process pub/sub event bus.

This is intentionally NOT a message queue (no Redis/RabbitMQ, no
persistence, no cross-process delivery) — the app runs as a single Flask
process, so a plain dict of callbacks is the honest implementation of
"loose coupling" at this scale. If the app ever splits into separate
services, this is the seam to replace with a real broker; nothing above
this module needs to change since it only talks to `publish`/`subscribe`.

Delivery is synchronous: publish() calls every matching subscriber
in-thread, in registration order. Exceptions in one subscriber are
caught and logged so a broken listener can't take down the publisher
(the orchestrator calling publish() mid-pipeline must not crash because
a notification subscriber threw).
"""
from collections import defaultdict


class EventBus:
    def __init__(self):
        self._subscribers = defaultdict(list)

    def subscribe(self, event_name, callback):
        """callback(event_name: str, payload: dict) -> None"""
        self._subscribers[event_name].append(callback)
        return self

    def subscribe_all(self, callback):
        """Subscribe to every event, regardless of name — useful for a
        single audit-log or debug listener."""
        self._subscribers['*'].append(callback)
        return self

    def publish(self, event_name, payload=None):
        payload = payload or {}
        for callback in self._subscribers.get(event_name, []):
            self._safe_call(callback, event_name, payload)
        for callback in self._subscribers.get('*', []):
            self._safe_call(callback, event_name, payload)

    def _safe_call(self, callback, event_name, payload):
        try:
            callback(event_name, payload)
        except Exception as e:
            print(f"[event_bus] subscriber for '{event_name}' raised: {e}")
