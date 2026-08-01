#!/usr/bin/env python3
"""Offline checks for the pacing logic. No network, no production traffic.

Run with:  python3 test_throttle.py
"""

import threading
import time
import unittest

from throttle import AdaptiveLimiter, StatusGovernor, backoff_delay


def status(active=0, waiting=0, max_concurrent=40):
    return {"active": active, "waiting": waiting, "max_concurrent": max_concurrent}


class TestAdaptiveLimiter(unittest.TestCase):
    def test_caps_in_flight(self):
        limiter = AdaptiveLimiter(initial=2, hard_max=8)
        peak = [0]
        lock = threading.Lock()
        errors = []

        def worker():
            try:
                self.assertTrue(limiter.acquire())
                with lock:
                    peak[0] = max(peak[0], limiter.in_flight)
                time.sleep(0.05)
                limiter.release()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertLessEqual(peak[0], 2, "limiter let more than its cap through")
        self.assertEqual(limiter.in_flight, 0)

    def test_hard_max_is_respected(self):
        limiter = AdaptiveLimiter(initial=1, hard_max=3)
        self.assertEqual(limiter.set_limit(99), 3)
        self.assertEqual(limiter.set_limit(-5), 0)

    def test_zero_limit_blocks_then_releases(self):
        limiter = AdaptiveLimiter(initial=0, hard_max=4)
        got = []

        def worker():
            got.append(limiter.acquire())

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.2)
        self.assertEqual(got, [], "acquire returned while the limit was 0")
        limiter.set_limit(1)
        t.join(timeout=3)
        self.assertEqual(got, [True])

    def test_close_wakes_waiters(self):
        limiter = AdaptiveLimiter(initial=0, hard_max=4)
        got = []
        t = threading.Thread(target=lambda: got.append(limiter.acquire()))
        t.start()
        time.sleep(0.1)
        limiter.close()
        t.join(timeout=3)
        self.assertEqual(got, [False], "close() must unblock waiters")


class TestStatusGovernor(unittest.TestCase):
    def governor(self, limiter, **kw):
        kw.setdefault("verbose", False)
        return StatusGovernor(limiter, status_url="http://example.invalid/status", **kw)

    def test_backs_off_when_anything_is_queued(self):
        limiter = AdaptiveLimiter(initial=8, hard_max=8)
        gov = self.governor(limiter)
        gov._apply(status(active=10, waiting=3))
        self.assertEqual(limiter.limit, 4, "a non-empty queue must halve the cap")

    def test_pauses_when_saturated(self):
        limiter = AdaptiveLimiter(initial=4, hard_max=8)
        gov = self.governor(limiter)
        gov._apply(status(active=40, waiting=97, max_concurrent=40))
        self.assertEqual(limiter.limit, 0, "saturation must stop the sweep dead")

    def test_stays_paused_for_the_cooldown(self):
        limiter = AdaptiveLimiter(initial=4, hard_max=8)
        gov = self.governor(limiter, pause_seconds=30.0)
        gov._apply(status(active=40, waiting=97))
        gov._apply(status(active=0, waiting=0))
        self.assertEqual(limiter.limit, 0, "cooldown must outlast one clear reading")

    def test_resumes_at_one_after_cooldown(self):
        limiter = AdaptiveLimiter(initial=4, hard_max=8)
        gov = self.governor(limiter, pause_seconds=0.0)
        gov._apply(status(active=40, waiting=97))
        gov._apply(status(active=0, waiting=0))
        self.assertEqual(limiter.limit, 1, "should come back cautiously, not at full tilt")

    def test_creeps_up_only_while_idle(self):
        limiter = AdaptiveLimiter(initial=1, hard_max=8)
        gov = self.governor(limiter, idle_fraction=0.25)
        gov._apply(status(active=2, waiting=0, max_concurrent=40))
        self.assertEqual(limiter.limit, 2, "an idle service should allow a step up")
        gov._apply(status(active=30, waiting=0, max_concurrent=40))
        self.assertEqual(limiter.limit, 2, "busy-but-unqueued should hold, not climb")

    def test_never_exceeds_the_ceiling(self):
        limiter = AdaptiveLimiter(initial=1, hard_max=3)
        gov = self.governor(limiter)
        for _ in range(20):
            gov._apply(status(active=0, waiting=0))
        self.assertEqual(limiter.limit, 3)

    def test_a_single_live_request_prevents_a_climb(self):
        # The whole point: one queued user must be enough to stop us expanding.
        limiter = AdaptiveLimiter(initial=4, hard_max=8)
        gov = self.governor(limiter)
        gov._apply(status(active=1, waiting=1, max_concurrent=40))
        self.assertLess(limiter.limit, 4)


class TestBackoff(unittest.TestCase):
    def test_grows_and_is_capped(self):
        base, cap = 15.0, 300.0
        for attempt in range(10):
            d = backoff_delay(attempt, base, cap)
            self.assertGreater(d, 0)
            self.assertLessEqual(d, cap)
        early = [backoff_delay(0, base, cap) for _ in range(50)]
        late = [backoff_delay(4, base, cap) for _ in range(50)]
        self.assertLess(max(early), min(late), "later attempts must wait longer")

    def test_is_jittered(self):
        values = {round(backoff_delay(2, 15.0, 300.0), 6) for _ in range(50)}
        self.assertGreater(len(values), 1, "identical delays would re-synchronise retries")


class TestRunQueryRetry(unittest.TestCase):
    """run_query must retry busy statuses and give up on real errors."""

    def setUp(self):
        import main
        self.main = main
        self.calls = []

        class FakeResponse:
            def __init__(self, code):
                self.status_code = code
                self.headers = {}

        class FakeSession:
            def __init__(self, outer, codes):
                self.outer, self.codes = outer, codes

            def get(self, url, timeout=None, headers=None):
                self.outer.calls.append(url)
                code = self.codes[min(len(self.outer.calls) - 1, len(self.codes) - 1)]
                return FakeResponse(code)

        self.FakeSession = FakeSession
        self._orig_session = main._get_session
        self._orig_sleep = main.time.sleep
        main.time.sleep = lambda _s: None

    def tearDown(self):
        self.main._get_session = self._orig_session
        self.main.time.sleep = self._orig_sleep

    def _run(self, codes, retries=3):
        session = self.FakeSession(self, codes)
        self.main._get_session = lambda: session
        return self.main.run_query("Q", "http://x/{id}", "FBbt_1", timeout=1,
                                   retries=retries, backoff_base=0.0, backoff_cap=0.0)

    def test_503_is_retried_until_it_succeeds(self):
        result = self._run([503, 503, 200])
        self.assertIn("✓", result)
        self.assertEqual(len(self.calls), 3, "503 must be retried, not discarded")

    def test_400_is_not_retried(self):
        result = self._run([400])
        self.assertIn("status 400", result)
        self.assertEqual(len(self.calls), 1, "a bad query must not be hammered")

    def test_gives_up_after_the_retry_budget(self):
        result = self._run([503], retries=2)
        self.assertIn("gave up", result)
        self.assertEqual(len(self.calls), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
