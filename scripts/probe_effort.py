#!/usr/bin/env python
"""Check that DeepSeek actually honours the reasoning-effort setting.

The effort dial is the largest cost lever the tool has: the reply is about 93% of
what a night costs, and most of the reply is reasoning. It used to be sent inside the
`thinking` object, where DeepSeek's guide does not put it, and nothing ever checked
that the API read it there. `tracker.llm.DeepSeekExtractor._reasoning` now sends it
where the guide does — and this is the measurement that says whether it works.

    python scripts/probe_effort.py                   # DeepSeek, then the reserve
    python scripts/probe_effort.py --route deepseek --repeat 3

Asks one small reasoning question at `low` and at `high` effort, `--repeat` times
each, and prints the reasoning tokens every reply spent — once against DeepSeek
directly and once against the OpenCode Go reserve, because the reserve forwards the
request to its own deployment and may not honour the dial the same way. If `low` is
not clearly cheaper than `high`, the dial is not reaching that provider and `low`
saves nothing there.

**It spends money**, a little: four to six calls with a short prompt, well under
¥0.05 at off-peak rates. It needs `TRACKER_DEEPSEEK_API_KEY`, so it runs on the
machine that holds one, and it is refused at peak hours like any other paid command
unless `TRACKER_PEAK_GUARD=off` is set. It writes nothing to the database.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracker.config import get_settings
from tracker.llm import DeepSeekExtractor, arm_peak_guard, split_thinking

QUESTION = (
    "A campus is announced at 1,200 MW of IT load in three equal phases. Phase one is "
    "energised; phase two is half built. How many megawatts are operating, and how many "
    "are under construction? Answer with two numbers."
)


def probe(settings, *, repeat: int, label: str) -> bool | None:
    """Low against high on one provider. True if low is clearly cheaper, None if skipped."""
    spent: dict[str, list[int]] = {}
    for effort in ("low", "high"):
        extractor = DeepSeekExtractor(settings, effort=effort)
        for _ in range(max(1, repeat)):
            reply = extractor.complete(system="Answer briefly.", user=QUESTION, max_tokens=8000)
            answer, thinking = split_thinking(reply.text)
            spent.setdefault(effort, []).append(reply.completion_tokens or 0)
            print(
                f"{label:9} {reply.model:22} {effort:5}  {reply.completion_tokens or 0:6d} reply "
                f"tokens  {len(thinking):6d} chars of reasoning  answer: {answer.strip()[:40]!r}"
            )
    low, high = statistics.median(spent["low"]), statistics.median(spent["high"])
    verdict = bool(high) and low < 0.75 * high
    print(
        f"{label}: median reply tokens low {low:.0f}, high {high:.0f} — "
        + ("the dial is honoured: `low` is cheaper" if verdict else "`low` is NOT clearly cheaper")
    )
    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repeat", type=int, default=2, help="calls per effort (default 2)")
    parser.add_argument(
        "--route",
        choices=("deepseek", "reserve", "both"),
        default="both",
        help="DeepSeek direct, the OpenCode Go reserve, or each in turn (default both)",
    )
    args = parser.parse_args(argv)

    arm_peak_guard()
    settings = get_settings()
    results = {}
    # DeepSeek first: a reserve run sets the process-wide switch, and after it every
    # call would go to the reserve whatever this asked for.
    if args.route in ("deepseek", "both"):
        results["deepseek"] = probe(
            settings.model_copy(update={"reserve_when": "empty"}),
            repeat=args.repeat,
            label="deepseek",
        )
    if args.route in ("reserve", "both"):
        if settings.has_reserve_key():
            results["reserve"] = probe(
                settings.model_copy(update={"reserve_when": "always"}),
                repeat=args.repeat,
                label="reserve",
            )
        else:
            print("reserve: skipped — TRACKER_OPENCODE_GO_API_KEY is not set")
    if not all(results.values()):
        print(
            "\nA dial that is not clearly cheaper is either not honoured or the question is "
            "too easy to show it — try --repeat 5 before concluding."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
