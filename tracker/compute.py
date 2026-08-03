"""Capacity expressed as accelerators: how many H200s a site is worth.

Megawatts is what gets reported and is not what anybody is actually asking. The
question behind these rows is how much training capacity a campus represents,
and "1 GW" only answers it after arithmetic the reader has to do with an
assumption they have to supply. This supplies one assumption, writes it down, and
applies it the same way everywhere.

**It is a unit conversion, not a claim.** The result is tiered `derived`, like
`county` and `lat`/`lon`: deterministic, reproducible, and carrying no more
authority than the megawatt figure it came from. If the capacity is 待确认 then so
is this, and if there is no capacity there is no number — a site nobody has sized
gets nothing rather than a zero, because a zero would be counted.

**A source that states a chip count beats it.** An article saying "100,000 GPUs"
has answered the question directly, and no conversion improves on that. Those
values come through the ordinary evidence gate with their own quote and are
tiered `reported`.
"""

from __future__ import annotations

from typing import Final

#: Facility power drawn per H200, in kilowatts, at the meter.
#:
#: Built from three published numbers rather than picked:
#:
#: * the H200 SXM board is **700 W** (NVIDIA's datasheet figure);
#: * a DGX H200 with eight of them draws **8.5 kW** for the whole node — CPUs,
#:   memory, NICs, fans and power-supply losses included — which is **1.06 kW per
#:   GPU** of IT load, and is the number that matters because a data center powers
#:   nodes rather than bare boards;
#: * a liquid-cooled AI hall is underwritten at **PUE 1.15 to 1.25** for 2026, so
#:   cooling and distribution add about a fifth again.
#:
#: 1.06 * 1.2 = about 1.3, giving roughly **770 H200s per megawatt**.
#:
#: Every part of that will age — boards get denser, PUE improves, and a site built
#: in 2028 will not be full of H200s at all. That is why this is a setting and why
#: the stored value is recomputed rather than remembered: the column is "capacity
#: restated in a unit people think in", not a measurement, and it should move when
#: the assumption does.
DEFAULT_KW_PER_H200: Final = 1.3

#: Below this, the answer is "a handful" and a precise-looking count is a lie
#: about precision. A 0.5 MW edge site is not a meaningful GPU cluster.
MIN_MW: Final = 0.1


def kw_per_h200(settings=None) -> float:
    from tracker.config import get_settings

    value = (settings or get_settings()).kw_per_h200
    return value if value and value > 0 else DEFAULT_KW_PER_H200


def h200_equivalent(mw: float | None, *, settings=None) -> int | None:
    """Accelerators a given IT capacity supports, or None if it cannot be said.

    Rounded to two significant figures, then to a whole number. The input is a
    megawatt figure someone rounded before publishing it, so reporting 36,923
    would dress a rounded input as a measurement — the honest output is 37,000.
    """
    if mw is None:
        return None
    try:
        megawatts = float(mw)
    except (TypeError, ValueError):
        return None
    if megawatts < MIN_MW:
        return None

    exact = megawatts * 1000.0 / kw_per_h200(settings)
    return _two_significant_figures(exact)


def _two_significant_figures(value: float) -> int:
    if value <= 0:
        return 0
    from math import floor, log10

    magnitude = floor(log10(value))
    step = 10 ** max(0, magnitude - 1)
    return int(round(value / step) * step)


def describe(count: int | None) -> str:
    """Short human form for a count, for tables and prose."""
    if count is None:
        return "—"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M".replace(".0M", "M")
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


__all__ = [
    "DEFAULT_KW_PER_H200",
    "MIN_MW",
    "describe",
    "h200_equivalent",
    "kw_per_h200",
]
