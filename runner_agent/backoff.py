"""Wartezeiten zwischen Verbindungsversuchen (M14-08)."""

from __future__ import annotations

import random
from collections.abc import Callable


class Backoff:
    """Exponentielles Backoff mit Streuung.

    Nach ``reset()`` (stabile Verbindung) beginnt es wieder bei ``base``. Close-Codes
    außerhalb von ``protocol.RECONNECT`` warten gleich die längste Zeit.
    ``rand`` liefert Werte in [0, 1) und ist im Test ersetzbar.
    """

    def __init__(
        self,
        base: float = 1.0,
        factor: float = 2.0,
        maximum: float = 60.0,
        jitter: float = 0.2,
        rand: Callable[[], float] = random.random,
    ):
        self.base = base
        self.factor = factor
        self.maximum = maximum
        self.jitter = jitter
        self.rand = rand
        self.attempt = 0

    def reset(self) -> None:
        self.attempt = 0

    def next(self, *, longest: bool = False) -> float:
        if longest:
            delay = self.maximum
        else:
            delay = min(self.base * self.factor**self.attempt, self.maximum)
        self.attempt += 1
        spread = delay * self.jitter
        return max(0.0, delay - spread + 2 * spread * self.rand())
