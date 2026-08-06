"""Rolling-window governor for the undocumented option-quote quota.

Measured behaviour (scripts/probe_option_quota.py, verified three ways):

    500 option-symbol-quotes per rolling minute, then error 301607
    "Too many option securities request within one minute"

  * Counted by TOTAL symbols summed across requests. Repeats are NOT free:
    25 calls x 20 symbols and 10 calls x 50 symbols both died at exactly 500.
  * calc_indexes(n option symbols) costs n.
  * subscribe(n option symbols) costs n, but only ONCE. Pushes thereafter are free.
  * realtime_depth / realtime_quote cache reads cost NOTHING (1000 reads, then a
    400-symbol calc_indexes still succeeded inside the same window).
  * option_chain_expiry_date_list / option_chain_info_by_date cost nothing
    (2002 symbols collected, then a full calc_indexes still succeeded).

None of this appears in the official rate-limit table, which documents only the
500-concurrent-subscription ceiling and 10 calls/second. The quota is real and it
is the binding constraint on refresh design, so it gets its own gate.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0
MEASURED_LIMIT = 500

# Spend against a lower ceiling than measured: the server's window boundary is not
# observable, so a full 500 would sit exactly on the cliff edge.
DEFAULT_LIMIT = 450

# Penalty applied when the server rejects us anyway; long enough to clear a window.
BACKOFF_SECONDS = 65.0


def is_option_quota_error(exc: BaseException) -> bool:
    text = str(exc)
    return "301607" in text and "option" in text.lower()


class OptionQuotaGovernor:
    """Sliding-window gate. Blocks until `cost` symbols fit in the trailing minute."""

    def __init__(self, limit: int = DEFAULT_LIMIT, window: float = WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window
        self._events: deque[tuple[float, int]] = deque()
        self._spent = 0
        self._blocked_until = 0.0
        self._waited = 0.0
        self._cv = threading.Condition()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0][0] <= cutoff:
            _, cost = self._events.popleft()
            self._spent -= cost

    def spent(self) -> int:
        with self._cv:
            self._prune(time.monotonic())
            return self._spent

    def available(self) -> int:
        with self._cv:
            self._prune(time.monotonic())
            return max(0, self.limit - self._spent)

    def acquire(self, cost: int, timeout: float | None = None) -> bool:
        """Block until `cost` fits. Returns False on timeout.

        A cost above the limit can never fit, so it is clamped and logged rather
        than deadlocking the caller.
        """
        if cost <= 0:
            return True
        if cost > self.limit:
            log.warning("option quota: cost %d exceeds limit %d; clamping", cost, self.limit)
            cost = self.limit

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while True:
                now = time.monotonic()
                self._prune(now)

                wait_for = 0.0
                if now < self._blocked_until:
                    wait_for = self._blocked_until - now
                elif self._spent + cost > self.limit:
                    # Wait for the oldest event to age out of the window.
                    wait_for = (self._events[0][0] + self.window) - now if self._events else 0.1
                else:
                    self._events.append((now, cost))
                    self._spent += cost
                    return True

                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        return False
                    wait_for = min(wait_for, remaining)
                before = time.monotonic()
                self._cv.wait(max(0.01, min(wait_for, 5.0)))
                self._waited += time.monotonic() - before

    def penalize(self) -> None:
        """Called after a 301607. Assume the window is full and stand down."""
        with self._cv:
            now = time.monotonic()
            self._blocked_until = max(self._blocked_until, now + BACKOFF_SECONDS)
            self._prune(now)
            # Treat the window as exhausted so nothing else slips through.
            self._events.append((now, max(0, self.limit - self._spent)))
            self._spent = self.limit
            self._cv.notify_all()
        log.warning("option quota tripped (301607); backing off %.0fs", BACKOFF_SECONDS)

    def blocked_for(self) -> float:
        with self._cv:
            return max(0.0, self._blocked_until - time.monotonic())

    def waited(self) -> float:
        """Cumulative seconds spent blocked on the window, since process start.

        Account-wide, like the quota itself: sampling it either side of one billed
        operation can attribute a concurrent caller's wait to that operation. Both
        waits are real, which is what the page claims.
        """
        with self._cv:
            return self._waited


_governor: OptionQuotaGovernor | None = None
_lock = threading.Lock()


def get_governor() -> OptionQuotaGovernor:
    global _governor
    with _lock:
        if _governor is None:
            _governor = OptionQuotaGovernor()
        return _governor
