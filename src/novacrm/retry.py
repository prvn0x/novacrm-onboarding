"""Short in-step retries for transient failures (design D6): 1 s, 2 s, 4 s with jitter.

After these, the caller parks the deal and the next poll resumes it; nothing here waits longer.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from typing import TypeVar

from .ports import TransientError

T = TypeVar("T")


def with_retries(
    fn: Callable[[], T],
    delays: Sequence[float],
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn`; on TransientError wait and try again, at most len(delays) more times."""
    for delay in delays:
        try:
            return fn()
        except TransientError:
            sleep(delay * random.uniform(0.8, 1.2))
    return fn()
