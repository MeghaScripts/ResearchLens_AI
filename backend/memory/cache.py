"""
Tiny in-process TTL cache. Not Redis — a plain dict with expiry
timestamps, guarded by a lock. Appropriate for a single Flask process;
if this app ever runs multiple worker processes behind gunicorn with
more than one worker, this cache stops being shared across them and a
real cache (Redis) becomes worth the operational cost. Until then, this
is the right-sized version.

Used for things like caching a topic's fetched-papers result for a few
minutes so re-running an agent during iterative UI use (e.g. asking the
Proposal agent, then the Experiment agent, on the same gap) doesn't
re-hit arXiv/Semantic Scholar/OpenAlex needlessly.
"""
import time
import threading


class TTLCache:
    def __init__(self, default_ttl_seconds=300):
        self.default_ttl = default_ttl_seconds
        self._store = {}
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return default
            value, expires_at = entry
            if time.time() > expires_at:
                del self._store[key]
                return default
            return value

    def set(self, key, value, ttl_seconds=None):
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        with self._lock:
            self._store[key] = (value, time.time() + ttl)

    def clear(self):
        with self._lock:
            self._store.clear()
