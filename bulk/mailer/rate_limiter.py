"""Rate limiter for even distribution over time with Gaussian jitter."""
import time
import random
import threading


class RateLimiter:
    """Distributes sends evenly over a time window with natural jitter.

    Example: 50000 mails over 24h = ~0.578 mails/sec = ~1.73s between mails.
    With Gaussian jitter the actual interval varies naturally.
    """

    def __init__(self, daily_limit: int = 0, jitter_factor: float = 0.2):
        self._daily_limit = daily_limit
        self._jitter_factor = jitter_factor
        self._lock = threading.Lock()
        self._last_send = 0.0

        if daily_limit > 0:
            self._interval = 86400.0 / daily_limit
        else:
            self._interval = 0.0

    @property
    def interval(self) -> float:
        return self._interval

    def wait(self) -> float:
        if self._interval <= 0:
            return 0.0

        with self._lock:
            now = time.monotonic()
            jittered = max(0.05, random.gauss(self._interval, self._interval * self._jitter_factor))
            target = self._last_send + jittered
            wait_time = max(0.0, target - now)
            self._last_send = max(now, target)

        if wait_time > 0:
            time.sleep(wait_time)
        return wait_time
