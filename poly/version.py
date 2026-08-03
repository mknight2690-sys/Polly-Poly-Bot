"""Single source of truth for POLY Alert Deck build label.

Bump BUILD (and usually PATCH) on every user-visible iteration so the
dashboard top-right tag and chat stay in sync.
"""
from __future__ import annotations

# Human + UI label — bump this whenever we ship a deck change.
BUILD = "v0.37.5"
# Monotonic integer for quick compare / cache bust hints.
BUILD_NUM = 43

__all__ = ["BUILD", "BUILD_NUM", "as_dict"]


def as_dict() -> dict:
    return {"build": BUILD, "build_num": BUILD_NUM}
