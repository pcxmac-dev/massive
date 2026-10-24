"""Span arithmetic for the chart engine."""
from __future__ import annotations


def uncovered(window: tuple[int, int], spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The parts of [lo, hi) that none of `spans` ([start, end) each) covers, oldest first."""
    lo, hi = window
    out, at = [], lo
    for a, b in sorted(spans):
        if b <= at:
            continue
        if a >= hi:
            break
        if a > at:
            out.append((at, a))
        at = max(at, b)
    if at < hi:
        out.append((at, hi))
    return out
