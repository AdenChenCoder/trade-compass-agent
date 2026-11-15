"""Deterministic indicator meaning shared by calculation and publication."""
from __future__ import annotations


def moving_average_alignment(ma_values):
    ordered = sorted(ma_values.items(), key=lambda item: int(item[0][2:]))
    if len(ordered) < 2:
        return "insufficient_data"
    values = [value for _, value in ordered]
    if all(a >= b for a, b in zip(values, values[1:])):
        return "bullish_alignment"
    if all(a <= b for a, b in zip(values, values[1:])):
        return "bearish_alignment"
    return "mixed"
