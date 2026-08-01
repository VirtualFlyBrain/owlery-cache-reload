#!/usr/bin/env python3
"""Background-friendly admission control for the cache reload sweep.

The cache loader is a housekeeping job: it must never compete with live user
traffic for VFBquery capacity. This module provides two cooperating pieces.

``AdaptiveLimiter``
    A global, dynamically resizable cap on how many requests the whole sweep
    may have in flight at once -- across every query type, not per pool.

``StatusGovernor``
    A background thread that polls the VFBquery ``/status`` endpoint and moves
    that cap up and down. It uses additive-increase / multiplicative-decrease:
    creep upwards only while the service looks completely idle, and drop hard
    (or all the way to zero) the moment anything is queueing. The effect is a
    slow trickle that gets out of the way as soon as real users show up.
"""

import json
import random
import threading
import time
import urllib.request


class AdaptiveLimiter:
    """A semaphore whose size can be changed while threads are waiting on it."""

    def __init__(self, initial, hard_max):
        self._cv = threading.Condition()
        self._hard_max = max(1, int(hard_max))
        self._limit = max(0, min(self._hard_max, int(initial)))
        self._in_flight = 0
        self._closed = False

    @property
    def hard_max(self):
        return self._hard_max

    @property
    def limit(self):
        with self._cv:
            return self._limit

    @property
    def in_flight(self):
        with self._cv:
            return self._in_flight

    def set_limit(self, value):
        """Resize the cap. Returns the value actually applied."""
        with self._cv:
            value = max(0, min(self._hard_max, int(value)))
            if value != self._limit:
                self._limit = value
                self._cv.notify_all()
            return self._limit

    def close(self):
        """Wake every waiter so the sweep can shut down."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def acquire(self):
        """Block until a slot is free. Returns False if the limiter was closed."""
        with self._cv:
            while not self._closed and self._in_flight >= self._limit:
                self._cv.wait(timeout=1.0)
            if self._closed:
                return False
            self._in_flight += 1
            return True

    def release(self):
        with self._cv:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._cv.notify()


class StatusGovernor:
    """Drives an AdaptiveLimiter from the VFBquery /status endpoint.

    The signal we care about is ``waiting``: if anything is queued at the
    service, someone is being made to wait, and a background cache warmer has
    no business adding to that. ``active`` versus ``max_concurrent`` gives the
    same warning slightly earlier.
    """

    def __init__(self, limiter, status_url, poll_interval=10.0,
                 idle_fraction=0.25, pause_seconds=60.0, verbose=True):
        self.limiter = limiter
        self.status_url = status_url
        self.poll_interval = poll_interval
        self.idle_fraction = idle_fraction
        self.pause_seconds = pause_seconds
        self.verbose = verbose
        self._stop = threading.Event()
        self._thread = None
        self._paused_until = 0.0
        self._consecutive_errors = 0

    def start(self):
        if not self.status_url:
            if self.verbose:
                print("[governor] disabled (no --status-url); "
                      f"running at a fixed cap of {self.limiter.limit}")
            return
        self._thread = threading.Thread(
            target=self._loop, name="status-governor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval + 5.0)

    def _fetch(self):
        req = urllib.request.Request(self.status_url,
                                     headers={"User-Agent": "owlery-cache-reload/governor"})
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def _log(self, message):
        if self.verbose:
            print(f"[governor] {message}", flush=True)

    def _loop(self):
        while not self._stop.wait(self.poll_interval):
            try:
                status = self._fetch()
                self._consecutive_errors = 0
            except Exception as exc:
                # A status blip must not stall the sweep, but repeated failures
                # mean we are flying blind -- fall back to the most cautious cap.
                self._consecutive_errors += 1
                if self._consecutive_errors == 3:
                    self._log(f"cannot read {self.status_url} ({type(exc).__name__}); "
                              "holding at 1 until it recovers")
                    self.limiter.set_limit(1)
                continue
            self._apply(status)

    def _apply(self, status):
        active = int(status.get("active", 0) or 0)
        waiting = int(status.get("waiting", 0) or 0)
        max_concurrent = int(status.get("max_concurrent", 0) or 0)
        current = self.limiter.limit
        now = time.monotonic()

        saturated = max_concurrent and active >= max_concurrent
        idle_ceiling = int(max_concurrent * self.idle_fraction) if max_concurrent else 0

        if saturated or waiting > 0:
            if saturated and waiting > 0:
                # Fully backed up: stand down completely for a cooldown.
                self._paused_until = now + self.pause_seconds
                if current != 0:
                    self.limiter.set_limit(0)
                    self._log(f"service saturated (active={active}/{max_concurrent}, "
                              f"waiting={waiting}) -- pausing for "
                              f"{int(self.pause_seconds)}s")
            else:
                reduced = max(1, current // 2)
                if reduced != current:
                    self.limiter.set_limit(reduced)
                    self._log(f"backing off to {reduced} (active={active}/"
                              f"{max_concurrent}, waiting={waiting})")
            return

        if now < self._paused_until:
            return

        if current == 0:
            self.limiter.set_limit(1)
            self._log(f"service clear (active={active}/{max_concurrent}, "
                      f"waiting={waiting}) -- resuming at 1")
            return

        # Only creep upwards while the service looks genuinely idle, so the
        # sweep never claims headroom that live traffic is about to need.
        if active <= idle_ceiling and current < self.limiter.hard_max:
            self.limiter.set_limit(current + 1)
            self._log(f"service idle (active={active}/{max_concurrent}) -- "
                      f"raising to {current + 1}")


def backoff_delay(attempt, base, cap):
    """Exponential backoff with full jitter, so retries do not sync up."""
    window = min(cap, base * (2 ** attempt))
    return window * (0.5 + random.random() * 0.5)
