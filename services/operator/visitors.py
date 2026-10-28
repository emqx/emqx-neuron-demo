"""Bounded admission limits shared by HTTP and device control."""
import math
import threading
import time


class Refused(Exception):
    def __init__(self, message, status=409, retry_after=1):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class Limiter:
    """Bound the limiter itself; fail closed when its identity budget is full."""
    def __init__(self, capacity=5000, clock=time.time):
        self.entries = {}
        self.capacity = capacity
        self.clock = clock
        self.lock = threading.Lock()

    def take(self, key, limit, seconds):
        now = self.clock()
        with self.lock:
            for old in [k for k, (_, end) in self.entries.items() if end <= now]:
                del self.entries[old]
            count, end = self.entries.get(key, (0, now + seconds))
            if count >= limit or (key not in self.entries and len(self.entries) >= self.capacity):
                raise Refused("Too many requests. Please wait before trying again.", 429, max(1, math.ceil(end - now)))
            self.entries[key] = (count + 1, end)
