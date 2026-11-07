"""The nightly quality loop: every phase that can improve a row, in rounds, until none does.

`scripts/overnight.sh` starts it from launchd at 18:01 and stays alive around it for
the whole night — see THE SHELL AROUND IT at the end of this docstring for what the
script keeps and why. Everything that decides what the night does is here::

    python -m tracker.overnight run [options]    # the night (overnight.sh runs this)
    python -m tracker.overnight check [options]  # parse the options and exit

Every other script does one pass over one thing. This runs every phase that can
improve a row, in dependency order, in rounds, until they stop improving it.

WHY ROUNDS. Neither backlog is a queue that drains. Ruling a claim out re-derives
the row and a re-derived row can raise a finding the old value hid — 52
resolutions once took the total from 530 to 529. Merging changes the survivor's
claim set and can match a third row that did not match before — answering 13 pairs
took the group count from 47 to 48. So the target is a fixed point, and the stop
condition is consecutive rounds with no reduction.

"Reduction" means a count falling BELOW THE LOWEST IT HAS BEEN THIS NIGHT, not below
last round's. The counts wobble on their own — a merge splits one group into two,
a re-derived row drops below T2 and climbs back — and measured against the previous
round every wobble back down read as progress: 2026-09-29 ran seven rounds with
duplicate groups going 12, 14, 12, 11 and rows below T2 525, 527, 524, 528, and
stopped with nothing moved. Against the running minimum it stops after three.

THE PHASE ORDER IS THE ARGUMENT. The news first, then cheapest and most-blocking:

  news      first round only, and FIRST since 2026-10-18: it ran last, so a host
            reset at 19:32 on 10-16 meant no discovery that day, and feeds that
            hold less than a day (Bisnow: five hours) lost those stories for good.
            `discover` polls the feeds and walks every archive — an operator's own
            data-center newsroom read without the keyword test — and remembers
            every entry it saw (`seen_url`); `news` searches the news for every
            campus that is due (`tracker.ingest.watch`: daily while a campus is
            decided or built, a year back on its first look, up to `--baselines`
            first looks a night); the queue read then takes `--discover` queued
            articles, the news first, then what the watch and the newsrooms found,
            then campuses the database has not heard of, through its identity
            check. Then the Census lookup places the new rows (county,
            town centre), and one duplicate attempt is made per row created in
            the last `TWIN_DAYS` days, `TWINS` at most; the night's progress
            counts are re-taken after that, since the news adds work by design.
            600 a night since 2026-10-19: the watch's first look and the first
            walk of the builders' archives left about 6,600 articles queued, a
            read costs about ¥0.0075 (10-18: 156 for ¥1.17) and 40 took 48
            seconds. At 150 the backlog would have taken two months to clear; at
            600 it is about ¥4.5 a night and about two weeks.
  free      dates, geo, scope, derive, logic --auto. No model, no cost. `scope`
            re-gates stored labels against their own quotes; `derive` re-applies
            every derived value, and both are pure functions of what is stored.
            In the first round the dates step also asks publishers (`--dates`,
            one plain request a page, each page once per 90 days, newest first).
  duplicates the agent: reads both rows, folds or parks. The only step that
            deletes anything, so it goes behind a snapshot — and ahead of every
            phase that pays to read one row, since a row it folds is a row they
            would have read for nothing (10-19: 12 of 14 logic findings were on
            rows created that evening, two of them merged away minutes later).
  audit     THE biggest tier lever and the one this loop used to miss entirely:
            `audit_clear` is a T1 gate failing 68 rows and the SOLE blocker on 54.
            One call per finding on the fixed-menu path, so it is also cheap.
  risks     reads the article behind each unquoted obstacle. Also T3, also cheap.
  logic     the agent: reads sources, rules wrong claims out of the merge.
  tranches  since 2026-10-21: one call per campus whose tranches still count the
            same megawatts twice (`tracker tranches review`), worst first, `--nest`
            a round. The model rules which tranche is part of which, the same as
            which, superseded or another campus's; a campus left open is held back
            until its tranches change. 128 campuses were open on the 10-21 copy,
            22 settled by the free rules alone; at 20 a round the rest take a few
            nights at about ¥0.02 a campus.
  enrich    the agent, last and separately budgeted, because it is the most
            expensive rung (~77,000 tokens a row) AND the largest tier lever
            there is: every one of the 283 rows sitting at T1 is held there by
            `fields_present` alone. Those rows are not wrong, they are empty, and
            nothing above this line can move one. It runs in the first TWO rounds
            only (`--enrich-rounds`), the second on rows the first did not ask
            about (`enrich --fresh`): what it reads changes when articles are
            ingested, not when a merge or a ruling lands, and on the nights of
            2026-09-24 to 09-29, asking the same rows again, it was 86% of the
            bill, round after round, for one new fact.
            It picks rows with `--t2` — the ones below T2 for missing fields,
            fewest missing first — where `--target 0` alone had sorted the
            FULLEST rows first and spent every round on rows already past the bar.
  reread    first round only: re-reads, from the cache, up to `--reread` articles
            that a superseded extraction prompt last read, oldest vintage first, so
            a tightened gate reaches rows written before it. Existing rows only.
  recheck   first round only: the same cached re-read, of up to `--recheck` articles
            holding a value, milestone or party the gate refused before its last
            revision (`crawl.GATE_REVISED_AT`). A gate change does not move the
            prompt's stamp, so `reread` never sees it. Most refusals first; each
            article is taken once. Existing rows only.
  (news)    see the top: what used to be the `discover` phase here, last, now
            runs before everything else. Everything published within the email's
            two-month window is read first, newest first, whatever campus it
            names, because what is read tonight is mailed tomorrow: a 09-21
            lawsuit against a tracked campus once waited behind the backlog until
            10-03 and was mailed as news.

WHAT IT COSTS, AND THE CEILING. The ceiling is money, priced from the spend ledger by
`tracker.spend` at OpenCode Go's rates for the hour each call was made. By default it
is tonight's share of a month (`tracker.spend allowance`, from 2026-10-25). OpenCode
Go's flash allowance is $100 a month (from 10-31; $80 before), and other work runs on
the same plan, so the tracker keeps to 85% of it: ¥603.50 over a rolling 30 days,
counting the console's and hand-run commands' daytime ledgers with the nights' (from
10-26), ¥20.12 a night,
less a seventh of whatever the last 29 days spent over their share. Unspent money is
not carried over: tried on the 10-25 ledgers, it ran four weeks above the share on
October's savings and then left two nights with nothing. Before that it was a flat
¥15 that no night reached: from 10-19 to 10-25 the loop spent ¥9.47-12.86 a night and
stopped at its fixed point with seven hours to spare, so the volumes below were
raised the same day. `--cny N` still sets a flat ceiling for one night. Web searches
are a separate bill (prepaid Serper credit, about ¥0.007 each): the ledger records
them and `--search-cap` (default 1700 a night, `TRACKER_SEARCH_CAP`) stops every
command at it; the news watch, which runs first, may use all but 500 of them, so the
research that runs later still has searches to send. The money ceiling does not
count them, and the closing line and the spend report price them beside the model
spend.

The ceiling is checked twice. Between phases, here, before every paid phase — so a
night ends below `--cny` plus one phase. And inside each paid command: the night's
ceiling is exported to every child (`TRACKER_SPEND_CEILING_CNY`,
`TRACKER_SPEND_CEILING_TOKENS`), so a command that reaches it mid-pass stops there
and says "spend ceiling reached", rather than finishing a pass the night can no
longer afford.

A ceiling in tokens (`--tokens`, still honoured) mostly counted cached prompt,
which is billed at a fiftieth of the rate; about 93% of the money is the reply,
most of it reasoning. 25,000,000 tokens was anything from ¥20 to ¥31, and on the
six nights to 2026-09-29 the loop cost ¥9-34 without the token ceiling ever
firing. Since 2026-11-01 its default follows the yuan ceiling (`token_ceiling`): 20%
above what that buys at the cheapest measured rate, ¥0.61 a million. The fixed 25
million had become the stop that bound once the yuan ceiling rose with Go's month:
that night ended at ¥16.06 of ¥43.18.

THE VOLUME, raised on 2026-10-05. The cost fixes of 09-30 left nights at ¥1.76-2.75
against a ¥12 ceiling, so the loop was doing a fifth of what it was allowed to:
15 rows enriched on a 60-article budget, 10 queued articles read for new campuses,
while 419 rows sat below T2 with something still worth searching for and 1,659
readable articles waited in the queue. Now 40 rows on 160 articles (the enrich pass
at ~¥0.12 a row, about ¥5), 40 queued articles (~¥0.01 each), 18 feeds, and 60
findings and 40 pairs a round. About ¥7 a night, so the ceiling still has room; the
enrich pass is the largest single phase, which bounds the overshoot at about
`--cny` + ¥5.

Raised again on 2026-10-25, to spend the month's share: 80 rows on 320 articles
(69% of rows still lacked a basic field; split on 10-26 into 30 campuses being built
or permitted with no date or capacity, then 50 below T2), 900 queued articles, and
120 each to re-read and re-check. About ¥16-17 a night at the 10-25 rates, which the
¥16.09 ceiling trimmed and the ¥20.12 one of 10-31 has room for; enrich runs after
the duplicate, audit, obstacle, contradiction and tranche passes in a round, so a
ceiling reached early cuts reading, never settling. An overshoot is one enrich pass (about ¥6), and the next
nights' allowance pays it back.

Raised by half on 2026-10-31, when Go's own usage showed the nights stopping at their
fixed point under the ceiling (¥12-16 a night against ¥16.09, 31% of Go's month used
with nine nights left): 45 + 75 rows on 180 + 300 articles, 1,350 queued articles, and
180 each to re-read and re-check. The ceiling is now read from Go's windows
(`tracker.spend.go_allowance`), so a quiet stretch of Go's month is spent before it
resets.

Later rounds are cheaper than the first: every paid phase records what it answered
or could not decide, keyed on the evidence it was shown, and does not re-offer it
until that evidence changes or a month passes (`tracker/declines.py`).

The per-item judgements — risks, audit, tranches, enrich's settle step — run at `high`
reasoning effort (`--judgement-effort`). They ran at `low` for one night,
2026-09-30, and two of the audit's three model decisions that night were wrong in
ways that move published totals: it replaced a campus's $600M with a statewide
"$20 billion+ in Ohio", and kept a land price as a campus's build investment while
saying in its own reason that it was the land price. The step cost ¥0.18 that
night; `high` costs a few tenths of a yuan more, which is not the place to save.
`--local-judgement` sends them to the local model (TRACKER_JUDGEMENT_PROVIDER=ollama).
`--agent-effort` sets the agent loops' effort (logic, duplicates, enrich's agent) for
the night, as `TRACKER_DEEPSEEK_AGENT_EFFORT`; unset, `.env` decides.

The morning report ends with every value the night changed (`tracker changes`
against the snapshot taken before round 1), each with the sentence now behind it,
so a person can read what the models did in a couple of minutes.

NOTHING IS LOST WITHOUT A RULING. Before the report, `tracker losses --restore`
compares every value with that same snapshot. A value emptied while its citation
still claims it was set aside by a ruling and is left alone; one whose citation
simply stopped claiming it — a re-read that did not repeat the field — gets its claim
back with its sentence when the stored article still contains it, and the row is
re-derived. Over 10-18 to 10-28 that path emptied 81 values, 42 with a sentence on
file stating them; `upsert.omitted_quietly` now closes it, and this is the net that
does not depend on knowing which path did the damage (`tracker.losses`).

THE CEILING READS A LEDGER, NOT THE LOG. Every paid call appends a line to the file
`TRACKER_SPEND_LEDGER` names (`tracker.llm.record_spend`), so a phase is counted
whether or not its command prints a token summary. The tally used to grep the log
for `~N tokens`, which three commands print and five paid phases did not — and
under `set -o pipefail` a grep that matched nothing *failed*, so from 2026-09-03 the
very first tally of every night, taken before any phase had printed anything,
ended the script straight after its snapshot. Twenty-one nights ran no phase at
all and logged no error. The tally is computed here now, in-process
(:func:`ledger_spend`), and a ledger that cannot be read or priced is a night that
has spent nothing — it costs the ceiling check, never the night. The morning report
breaks the night's spend down by command, from the same file.

WHAT IT CANNOT DO. Roughly 250 findings are about tranche identity —
`block_label_ambiguous` and relatives — and the only repair an agent has is
superseding a claim about a field. It will read the sources and decline them, once
each. The real fix is a `blocks fold` command that does not exist. A stubborn
residue of block findings is not this loop failing.

WHO CHOOSES THE STEPS, since 2026-11-05. Each phase above is a step
(`tracker.night.steps`), and every other night (`--mode auto`, by date) a model — the
loop director — chooses which steps run, how big, in what order and when the night
stops, re-planning after every three steps. On the nights between, the order above
runs exactly as before and the director only predicts each step's cost and yield. The
model chooses; code decides whether the choice is allowed (`tracker.night.referee`:
ranges, drift, floors, the order rules, effort), runs it as the same commands, and
measures it. The news, the twin pass and the free phase are placed by the night, never
chosen. A director that fails, spends its ¥5, or presides over a round that loses two
answers in the answer key (`tracker.night.guard`) hands the rest of the night to the
fixed schedule. Every step of every night leaves a record of its cost and result
(`tracker.night.record`, `data/runs/steps.jsonl`). `docs/loop-director.md` has the
rules and why each exists.

SAFETY, since nobody is watching:

* a `VACUUM INTO` snapshot before the first merge and every `--backup-every` rounds
  (`tracker.db.take_snapshot`, verified; a snapshot that fails stops the rounds,
  because the next phase may delete a row);
* one run at a time — the shell's lock;
* every phase is its own `tracker` process, and its exit status is ignored: a
  provider failure at 3am, a crash or a kill loses that phase, not the night, and
  every command commits as it goes;
* the night runs on one version of the code: the shell waits out a deploy already
  under way, and the deployer holds any commit while the lock is live. Still, do not
  push to main while it runs: the held commit goes live unwatched when the night
  ends.

THE SHELL AROUND IT. `scripts/overnight.sh` keeps the parts that have to be the
process the deployer can see. The deployer (outside this repo) decides a night is
running by reading `data/runs/overnight.lock/pid` and checking that process's command
line names `overnight.sh` — so the lock is taken by the shell, held by the shell, and
released by the shell's EXIT trap, and the shell runs this module as a child rather
than `exec`-ing into it. It also keeps `--status`, the wait for a running deployer,
the PATH and venv, the per-night spend ledger (`TRACKER_SPEND_LEDGER`, named
`overnight-YYYYMMDD-HHMMSS.spend`), the read of tonight's allowance, and the `tee`
into `data/runs/overnight.log`.

THE LOG IS A CONTRACT. The status page reads `data/runs/overnight.log`
(`tracker.webui.statussite.parse_nights`), `tracker loop` reads it with the opening
block's `ceilings`, `at start`, `spend` and `steps` lines and a directed night's
`referee:` and `accuracy` lines and its hand-over and stop (`tracker.loopstatus`), and
`overnight.sh --status` greps it, so the words and line shapes they look for are
constants below, imported from here by both readers, and the formats — `[YYYY-MM-DD HH:MM:SS] msg`, `--- HH:MM:SS  msg`,
`  round N: findings A -> B (+d) ...` — are byte-for-byte what the shell printed, so
every old log still parses.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import os
import re
import signal
import subprocess
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Final, NamedTuple, Protocol

from tracker import spend
from tracker.night import record as record_mod
from tracker.night import referee
from tracker.night import steps as steps_mod
from tracker.night.director import CHECK_IN_EVERY
from tracker.night.steps import EFFORTS, TWIN_DAYS, Step

# --- the log contract ---------------------------------------------------------------
# What `tracker.webui.statussite`, `tracker.loopstatus` and `overnight.sh --status` read. Change one of these
# and old logs stop parsing: add a new spelling beside the old instead.

#: Opens a night: `[stamp] overnight starting  (pid N)`.
STARTING: Final = "overnight starting"
#: Closes a night: `[stamp] overnight complete — Nm, <money>`.
COMPLETE: Final = "overnight complete"
#: The money ceiling stopped the night (also what a paid command prints mid-pass).
SPEND_CEILING: Final = "spend ceiling reached"
#: The token ceiling's own wording, from logs before the ceiling was money.
TOKEN_CEILING: Final = "token ceiling reached"
#: `--hours` stopped the night.
WALL_CLOCK: Final = "wall-clock ceiling"
#: `--dry-rounds` rounds in a row moved nothing.
FIXED_POINT: Final = "fixed point"
#: Every count reached zero.
SETTLED: Final = "everything settled"
#: Each round's progress line starts with this; `--status` greps for it.
ROUND_LINE: Final = "  round "
#: The director ended a directed night (`[stamp] the director stopped the night: why`).
DIRECTOR_STOPPED: Final = "the director stopped the night"
#: The director failed and the fixed schedule took over (`[stamp] the director failed (why) — …`).
DIRECTOR_FAILED: Final = "the director failed"
#: The opening block's line saying who chooses tonight's steps.
STEPS_LINE: Final = "    steps     "
#: A change the referee made to a director's plan.
REFEREE_LINE: Final = "    referee: "
#: The accuracy check after a directed round, and in the morning report.
ACCURACY_LINE: Final = "    accuracy  "
#: Accuracy items shown with the step that moved each, in the log and to the director.
ATTRIBUTED_SHOWN: Final = 8

#: A `say` line: `[YYYY-MM-DD HH:MM:SS] message`.
STAMPED: Final = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$")
#: A `phase` line: `--- HH:MM:SS  message`.
PHASE_LINE: Final = re.compile(r"^--- (\d\d:\d\d:\d\d)  ")
#: The night's length in the closing line.
MINUTES: Final = re.compile(re.escape(COMPLETE) + r" — (\d+)m")

#: The per-night spend ledger, named by the shell when the night starts:
#: `overnight-YYYYMMDD-HHMMSS.spend`, in the log's directory.
LEDGER_PREFIX: Final = "overnight-"
LEDGER_STAMP: Final = "%Y%m%d-%H%M%S"
LEDGER_SUFFIX: Final = ".spend"


def ledger_name(at: dt.datetime) -> str:
    """The file name the night starting at `at` (local time) keeps its spend in."""
    return f"{LEDGER_PREFIX}{at:{LEDGER_STAMP}}{LEDGER_SUFFIX}"


# --- the night's fixed settings ---------------------------------------------------------

# The twin pass: rows created in the last TWIN_DAYS days, at most TWINS of them a
# night. A lookback rather than tonight alone, because a night whose pass was capped,
# crashed or blind (it ran before the rows had a county until 2026-10-20) was never
# revisited: the 10-19 night left about 16 twins standing. A pair already asked on
# the same evidence is held back (`declines`), so the lookback re-asks only what
# changed. The cap is what keeps a 100-row night from spending the budget here.
# SEARCHES_KEPT: what the fixed schedule's news watch leaves for the research after it.
# All four live with the steps they shape (`tracker.night.steps`).

#: The ceiling when `--cny` names none and no allowance can be read: the flat ceiling
#: every night ran under before the allowance existed.
FALLBACK_CNY: Final = "15"

#: How a night is chosen: by the date (`auto`: directed on even days, fixed on odd),
#: or always one way. Decided 2026-11-05: alternate nights, so a bad directed night is
#: followed by a normal one.
MODES: Final = ("auto", "fixed", "director")


# --- options ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    hours: int = 10
    rounds: int = 20
    dry_rounds: int = 2
    findings: int = 60
    pairs: int = 40
    audit: int = 60
    risks: int = 40
    nest: int = 20
    # Values the suspicion score ranks most likely wrong, re-read by the logic agent
    # in round 1 (`logic resolve --suspects`): the top of the ranking and a random
    # fifth. Each answer lands in the verdict log the score is retrained from.
    suspects: int = 25
    # The first enrich pass: campuses being built or permitted that lack an online
    # date or planned capacity, largest first, asked only those two fields (`enrich
    # --pipeline`). On 10-26, 111 of 258 under construction and 135 of 169 in
    # permitting had no date, so the supply chart and the delay flags could not see
    # them; the T2 pass weighs every field alike and rarely chose them.
    pipeline: int = 45
    pipeline_budget: int = 180
    enrich: int = 75
    enrich_budget: int = 300
    # Round 2 takes the rows round 1 did not ask about (`enrich --fresh`). Grouping the
    # news watch's searches by company (v6.5.1) freed about 500 searches a night, and
    # on 11-03 enrich still left ¥29.63 and seven hours unused: 681 rows below T2
    # had fields worth asking about and round 1 reached 75.
    enrich_rounds: int = 2
    discover: int = 1350
    # Have a model read the queued headlines first and set aside what cannot be campus
    # news (`ingest queue --screen`). On, for pages about a place abroad only:
    # measured on 5,272 past reads on 2026-10-26, that spares 230 useless reads and
    # loses 4 of 2,841 useful ones; every other kind cost real news
    # (`headlines.ACTED_ON`).
    screen: bool = True
    baselines: int = 300
    # 1,700 from 11-04, 2,000 before: every night spent all 2,000 and the credit
    # was running out; quiet campuses are looked at weekly from the same day, which
    # frees about the difference.
    search_cap: int = 1700
    dates: int = 150
    reread: int = 180
    recheck: int = 180
    min_confidence: str = "0.85"
    merge: bool = True
    #: None: tonight's share of the month (`tracker.spend allowance`).
    cny: str | None = None
    #: None: derived from tonight's money ceiling (`token_ceiling`), so the yuan stop
    #: is the one that binds and the tokens are only a backstop.
    tokens: int | None = None
    judgement_effort: str = "high"
    extraction_effort: str | None = None
    agent_effort: str | None = None
    local_judgement: bool = False
    backup_every: int = 5
    #: Who chooses the steps: `auto` alternates by date (`MODES`).
    mode: str = "auto"
    #: Run a recorded directed night's steps again, without a model.
    replay: Path | None = None
    #: Set by the shell, not by a person: the checkout, and what `tracker.spend
    #: allowance` printed (possibly nothing).
    repo: Path = field(default_factory=Path.cwd)
    allowance: str | None = None

    @property
    def do_discover(self) -> bool:
        return self.discover > 0

    @property
    def do_enrich(self) -> bool:
        return self.enrich > 0


def _number(text: str) -> str:
    """A decimal kept as typed — the log prints it back as given — once it parses."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {text!r}")
    return text


def parser() -> argparse.ArgumentParser:
    d = Options()
    p = argparse.ArgumentParser(
        prog="overnight.sh",
        description=(
            "Runs every quality phase in rounds until they stop improving anything. "
            "Meant to be started in tmux and left. `overnight.sh --status` prints where "
            "a running one has got to."
        ),
        epilog=(
            "In tmux:\n\n"
            "  tmux new -s tracker\n"
            "  caffeinate -i ~/dev/tracker/repo/scripts/overnight.sh --hours 10\n"
            "  # ctrl-b d to detach, `tmux attach -t tracker` to return\n\n"
            "Everything is appended to data/runs/overnight.log; the morning report is the "
            "last thing in it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    def a(*names: str, **kwargs) -> None:
        if kwargs.get("type") in (int, _number):
            kwargs.setdefault("metavar", "N")
        elif "choices" in kwargs:
            kwargs.setdefault("metavar", "E")
        p.add_argument(*names, **kwargs)

    a(
        "--hours",
        type=int,
        default=d.hours,
        help="wall-clock ceiling (default 10). Checked between rounds.",
    )
    a("--rounds", type=int, default=d.rounds, help="max rounds (default 20)")
    a(
        "--dry-rounds",
        type=int,
        default=d.dry_rounds,
        help="stop after N rounds with no reduction (default 2)",
    )
    a("--findings", type=int, default=d.findings, help="logic findings per round (default 60)")
    a("--pairs", type=int, default=d.pairs, help="duplicate pairs per round (default 40)")
    a("--audit", type=int, default=d.audit, help="audit findings per round (default 60)")
    a("--risks", type=int, default=d.risks, help="obstacles per round (default 40)")
    a(
        "--suspects",
        type=int,
        default=d.suspects,
        help="values the suspicion score ranks most likely wrong, re-read in round 1 "
        "(default 25; 0 turns it off)",
    )
    a(
        "--nest",
        type=int,
        default=d.nest,
        help="campuses whose tranches the model nests, per round (default 20)",
    )
    a(
        "--pipeline",
        type=int,
        default=d.pipeline,
        help="campuses being built or permitted with no online date or capacity to enrich first (default 45); 0 to skip",
    )
    a(
        "--pipeline-budget",
        type=int,
        default=d.pipeline_budget,
        help="articles that pass may read (default 180)",
    )
    a(
        "--enrich",
        type=int,
        default=d.enrich,
        help="projects below T2 to enrich after it (default 75); 0 to skip",
    )
    a(
        "--enrich-budget",
        type=int,
        default=d.enrich_budget,
        help="articles that pass may read (default 300)",
    )
    a(
        "--enrich-rounds",
        type=int,
        default=d.enrich_rounds,
        help="enrich in the first N rounds only, each after the first on rows not yet "
        "asked about tonight (default 2)",
    )
    a(
        "--discover",
        type=int,
        default=d.discover,
        help="queued articles to read, the news first (default 1350); 0 to skip discovery, the news watch and the read altogether",
    )
    screen = p.add_mutually_exclusive_group()
    screen.add_argument(
        "--screen",
        dest="screen",
        action="store_true",
        default=d.screen,
        help="have a model read their headlines first and set aside what is about a place abroad (default on)",
    )
    screen.add_argument(
        "--no-screen", dest="screen", action="store_false", help="read the queue unscreened"
    )
    a(
        "--baselines",
        type=int,
        default=d.baselines,
        help="campuses the news watch gives their first, year-long look each night (default 300); 0 for follow-ups only",
    )
    a(
        "--search-cap",
        type=int,
        default=d.search_cap,
        help="web searches the whole night may send (default 1700); the news watch may use all but 500, which the research keeps",
    )
    a(
        "--dates",
        type=int,
        default=d.dates,
        help="pages to ask for a publish date, first round (default 150); 0 to skip",
    )
    a(
        "--reread",
        type=int,
        default=d.reread,
        help="articles to re-read under the current prompt, first round (default 180); 0 to skip",
    )
    a(
        "--recheck",
        type=int,
        default=d.recheck,
        help="articles holding a refusal from before the gate's last revision to re-read, first round (default 180); 0 to skip",
    )
    a(
        "--min-confidence",
        type=_number,
        default=d.min_confidence,
        help="floor a duplicate fold needs (default 0.85)",
    )
    a(
        "--no-merge",
        dest="merge",
        action="store_false",
        default=d.merge,
        help="never fold duplicates; park and rule only. Deletes nothing.",
    )
    a(
        "--cny",
        type=_number,
        default=None,
        help="stop when the night's model spend, priced, reaches N yuan (default: tonight's share of the tracker's month, `tracker.spend allowance`)",
    )
    a(
        "--tokens",
        type=int,
        default=d.tokens,
        help="also stop when prompt+reply tokens pass N (default: 20%% above what the "
        "money ceiling buys at the cheapest measured rate)",
    )
    a(
        "--judgement-effort",
        choices=EFFORTS,
        default=d.judgement_effort,
        help="reasoning for risks, audit, tranches and settle (default high)",
    )
    a(
        "--extraction-effort",
        choices=EFFORTS,
        default=None,
        help="reasoning for reading articles (default: whatever .env says)",
    )
    a(
        "--agent-effort",
        choices=EFFORTS,
        default=None,
        help="reasoning for the agent loops: logic, duplicates, enrich (default: whatever .env says)",
    )
    a(
        "--local-judgement",
        action="store_true",
        help="send risks, audit, tranches and settle to the local model (Ollama); "
        "the agent loops stay on the API",
    )
    a(
        "--backup-every",
        type=int,
        default=d.backup_every,
        help="snapshot every N rounds (default 5). Always before round 1.",
    )
    a(
        "--mode",
        choices=MODES,
        default=d.mode,
        metavar="M",
        help="who chooses the steps: director (a model, inside the same limits), fixed "
        "(the schedule in this file), or auto (default): director on even days, fixed "
        "on odd, with the director predicting in the background on fixed nights",
    )
    a(
        "--replay",
        type=Path,
        default=None,
        help="run the steps a directed night recorded (data/runs/director-*.jsonl) "
        "again, without a model",
    )
    a("--repo", type=Path, default=None, help=argparse.SUPPRESS)
    a("--allowance", default=None, help=argparse.SUPPRESS)
    return p


def parse_options(argv: Sequence[str]) -> Options:
    ns = vars(parser().parse_args(list(argv)))
    if ns["repo"] is None:
        ns["repo"] = Path.cwd()
    if ns["backup_every"] < 1:
        parser().error("--backup-every must be at least 1")
    return Options(**ns)


#: Yuan per million prompt+reply tokens, the cheapest night measured: 10-26 to 11-01
#: ran ¥0.611 to ¥0.666 a million (average ¥0.643), the mix of models and cache hits
#: moving it. The cheapest, so a night that turns out cheap still meets the yuan
#: ceiling before the token one.
CNY_PER_MILLION_TOKENS: Final = 0.61
#: The token ceiling sits this far above what the yuan ceiling buys: a backstop for a
#: ledger that cannot be priced, never the stop that ends an ordinary night. A fixed
#: 25 million ended 2026-11-01 at ¥16.06 of a ¥43.18 ceiling, 37% of the money.
TOKEN_HEADROOM: Final = 1.2


def token_ceiling(cny_cap: str) -> int:
    """Tokens the night may use: 20% above what `cny_cap` yuan buys at the cheapest rate."""
    return int(float(cny_cap) / CNY_PER_MILLION_TOKENS * 1_000_000 * TOKEN_HEADROOM)


def money_ceiling(o: Options) -> tuple[str, str]:
    """Tonight's money ceiling in yuan, as the log prints it, and where it came from.

    `--cny` if given; else tonight's share of the tracker's month on OpenCode Go, less
    a seventh of whatever the last 29 days overspent (`tracker.spend allowance`, read
    by the shell over every ledger beside tonight's). A ceiling that cannot be read
    falls back to the old ¥15.
    """
    if o.cny is not None:
        return o.cny, "--cny"
    text = (o.allowance or "").strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return text, "the month's allowance"
    return FALLBACK_CNY, "the fallback, as no allowance could be read"


def night_env(o: Options, cny_cap: str, ledger: Path) -> dict[str, str]:
    """What every `tracker` child gets on top of the shell's environment.

    The environment wins over `.env`, which is the point: these are the night's
    settings, not the machine's.
    """
    env = {
        "TRACKER_SPEND_LEDGER": str(ledger),
        "TRACKER_SPEND_CEILING_CNY": cny_cap,
        "TRACKER_SPEND_CEILING_TOKENS": str(o.tokens),
        "TRACKER_SEARCH_CAP": str(o.search_cap),
        "TRACKER_DEEPSEEK_JUDGEMENT_EFFORT": o.judgement_effort,
    }
    if o.extraction_effort:
        env["TRACKER_DEEPSEEK_EXTRACTION_EFFORT"] = o.extraction_effort
    if o.agent_effort:
        env["TRACKER_DEEPSEEK_AGENT_EFFORT"] = o.agent_effort
    if o.local_judgement:
        env["TRACKER_JUDGEMENT_PROVIDER"] = "ollama"
    return env


# --- what the loop measures -------------------------------------------------------------


class Counts(NamedTuple):
    """Three numbers: logic findings not yet answered, duplicate GROUPS, rows below T2.

    Groups rather than pairs because nine rows for one campus are 36 pairs and one
    group, and a loop counting pairs would read a single merge as huge progress.
    Rows-below-T2 is what the enrich phase moves and neither other number can see.
    """

    findings: int
    groups: int
    below: int


def progress_counts(session) -> Counts:
    """The night's three counts, from one session (`quality.night_counts`, which
    loads the rows once for all three)."""
    from tracker.quality import night_counts

    return Counts(*night_counts(session))


def count(db_path: Path) -> Counts:
    """The counts from a fresh, read-only session on `db_path`.

    Fresh each time: every phase between two counts is another process writing the
    file, and a session held open across them would read a stale snapshot.
    """
    from tracker.db import open_db, session_scope

    engine = open_db(db_path)
    try:
        with session_scope(engine, commit=False) as session:
            return progress_counts(session)
    finally:
        engine.dispose()


class Spent(NamedTuple):
    cny: float
    tokens: int


def ledger_spend(ledger: Path | None) -> Spent:
    """What the night's paid calls cost (yuan) and how many tokens they moved.

    Priced by `tracker.spend` at the rate for the hour each call was made. A missing
    or empty ledger is a night that has spent nothing, and a ledger that cannot be
    priced costs the night its ceiling check — never the night itself.
    """
    try:
        calls = spend.read(ledger)
        return Spent(spend.total(calls), sum(call.tokens for call in calls))
    except Exception:  # a pricing bug must not end the night
        return Spent(0.0, 0)


# --- running a phase --------------------------------------------------------------------


class Runner(Protocol):
    """Runs `tracker <args>` with `env` added; returns its exit status.

    `head`: keep only the first N lines of its output (stdout and stderr together),
    for the morning report's read-only summaries.
    """

    def __call__(
        self, args: Sequence[str], *, env: Mapping[str, str], head: int | None = None
    ) -> int: ...


class Log:
    """The night's output, UTF-8 whatever the console's encoding, flushed every write.

    Flushed because each phase is a child writing to the same stream: anything this
    process had buffered would come out after the phase it introduces.
    """

    def __init__(self, stream: BinaryIO, clock: Callable[[], dt.datetime]):
        self.stream = stream
        self.clock = clock

    def write(self, text: str) -> None:
        self.raw(text.encode("utf-8"))

    def raw(self, data: bytes) -> None:
        self.stream.write(data)
        self.stream.flush()

    def line(self, text: str = "") -> None:
        self.write(text + "\n")

    def say(self, message: str) -> None:
        self.write(f"\n[{self.clock():%Y-%m-%d %H:%M:%S}] {message}\n")

    def phase(self, message: str) -> None:
        self.write(f"\n--- {self.clock():%H:%M:%S}  {message}\n")


def process_runner(log: Log) -> Runner:
    """Each phase as `python -m tracker …`: its own process, stdin from /dev/null.

    Its own process for isolation — a phase that crashes, exits non-zero or is killed
    costs that phase, never the night. Its output goes straight to the stream this
    process writes to, as it is produced, so the log shows a long phase working.
    """

    def run(args: Sequence[str], *, env: Mapping[str, str], head: int | None = None) -> int:
        argv = [sys.executable, "-m", "tracker", *map(str, args)]
        merged = {**os.environ, **env}
        log.stream.flush()
        if head is None:
            return subprocess.run(
                argv, stdin=subprocess.DEVNULL, env=merged, check=False
            ).returncode
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=merged,
            check=False,
        )
        for line in done.stdout.splitlines(keepends=True)[:head]:
            log.raw(line if line.endswith(b"\n") else line + b"\n")
        return done.returncode

    return run


class SnapshotFailed(RuntimeError):
    pass


def snapshot_db(db: Path, repo: Path, now: dt.datetime) -> Path:
    """A verified `VACUUM INTO` copy of `db`, dated, beside the checkout.

    In `../backups` when the host has one (it sits beside the checkout, so a bad
    commit cannot reach it), else `data/backups`. VACUUM INTO, never cp: WAL mode
    means a copy of the main file alone opens cleanly and is silently stale.
    """
    from tracker.db import take_snapshot

    beside = repo.parent / "backups"
    directory = beside if beside.is_dir() else repo / "data" / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"tracker.backup-overnight-{now:%Y%m%d-%H%M%S}.db"
    try:
        problems = take_snapshot(db, dest)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise SnapshotFailed(str(exc)) from exc
    if problems:
        dest.unlink(missing_ok=True)
        raise SnapshotFailed("; ".join(problems))
    return dest


class CountFailed(RuntimeError):
    pass


# --- the night --------------------------------------------------------------------------


@dataclass
class Night:
    """One night's loop. Everything it touches outside itself is passed in, so the
    tests drive it with a stub runner, fake counts and a fake clock."""

    options: Options
    log: Log
    runner: Runner
    counts: Callable[[], Counts]
    spent: Callable[[], Spent]
    snapshot: Callable[[], Path]
    clock: Callable[[], dt.datetime]
    ledger: Path
    pid: int
    #: Writes the night's check findings to the training log; returns how many.
    findings: Callable[[], int] = lambda: 0
    #: The counts a step is measured by (`record.probe`), or None when unmeasured.
    probe: Callable[[], record_mod.Metrics | None] | None = None
    #: Where each step's record is appended (`data/runs/steps.jsonl`), or None.
    record_path: Path | None = None
    #: The director choosing tonight's steps, or None for the fixed schedule.
    director: Any = None
    #: On a fixed night, the director predicting in the background, or None.
    shadow: Any = None
    #: What the director may ask for (`referee.Rules`); defaults from the options.
    rules: referee.Rules | None = None
    #: The accuracy check against the first snapshot (`guard.check`), or None.
    guard: Callable[[Path], Any] | None = None
    #: The director's plan file (`director.PlanFile`), or None.
    plan_file: Any = None
    #: What the director has spent tonight, in yuan.
    director_spent: Callable[[], float] = lambda: 0.0
    #: Runs once the morning report is written (a directed night clears the operator's note).
    on_finish: Callable[[], Any] = lambda: None
    night_id: str = ""
    version: str = ""

    capped_: bool = False
    first_snapshot: Path | None = None

    def __post_init__(self) -> None:
        self.cny_cap, self.cny_from = money_ceiling(self.options)
        if self.options.tokens is None:
            self.options = dataclasses.replace(self.options, tokens=token_ceiling(self.cny_cap))
        self.env = night_env(self.options, self.cny_cap, self.ledger)
        if self.rules is None:
            self.rules = referee.Rules(
                self.options,
                efforts={
                    steps_mod.AGENT_EFFORT: self.options.agent_effort or "high",
                    steps_mod.JUDGEMENT_EFFORT: self.options.judgement_effort,
                    steps_mod.EXTRACTION_EFFORT: self.options.extraction_effort or "high",
                },
                allowance=referee.allowance(self.options, {}),
            )
        self.tonight = referee.Tonight()
        self.records: list[record_mod.StepRecord] = []
        self.metrics: record_mod.Metrics | None = None
        self.fell_back = ""
        self.stop_why = ""
        self.guard_lines: list[str] = []
        #: The latest check's items, each with the step that moved it.
        self.guard_items: list[str] = []
        self.placed = False
        #: Records already told to the director.
        self.reported = 0
        self.shadow_used = self.shadow is not None
        self.shadow_stopped = ""

    @property
    def mode(self) -> str:
        if self.director is None:
            return "fixed"
        return "director" if not self.fell_back else "director, fell back"

    # --- helpers ---------------------------------------------------------------------

    def tracker(
        self, *args: object, head: int | None = None, env: Mapping[str, str] | None = None
    ) -> int:
        """One command; its exit status. A failure is that command's alone: see SAFETY."""
        try:
            code = self.runner([str(a) for a in args], env={**self.env, **(env or {})}, head=head)
            return int(code or 0)
        except Exception as exc:  # could not even start it: that phase is lost
            self.log.line(f"    could not run `tracker {' '.join(map(str, args))}`: {exc}")
            return -1

    def measure(self) -> Counts:
        try:
            return Counts(*self.counts())
        except Exception as exc:
            raise CountFailed(f"{type(exc).__name__}: {exc}") from exc

    def backup(self) -> Path:
        dest = self.snapshot()
        self.log.line(f"    snapshot: {dest}")
        return dest

    def over_ceiling(self) -> tuple[bool, Spent]:
        """Either ceiling reached: yuan (as the log prints it, to the fen), or tokens."""
        s = self.spent()
        return (s.tokens >= self.options.tokens or round(s.cny, 2) >= float(self.cny_cap)), s

    def capped(self, before: str) -> bool:
        """Before each paid phase rather than once a round: a round is several phases,
        so a check at the top of the round let one round overshoot the ceiling by a
        round. True ends the rounds; the settle after them still runs once."""
        over, s = self.over_ceiling()
        if over:
            self.log.say(
                f"{SPEND_CEILING} (¥{s.cny:.2f} of ¥{self.cny_cap}, "
                f"~{s.tokens} of {self.options.tokens} tokens) before {before} — stopping"
            )
            self.capped_ = True
        return over

    def settle(self, label: str = "settle — re-derive and score") -> None:
        """Re-derive every row and score the night. Free; the end of every round, and
        once more after a round the ceiling cut short, since that round skipped its
        own and the phases that did run changed rows."""
        self.log.phase(label)
        self.tracker("backfill", "derive")
        self.tracker("clean", "--snapshot", "--since", "1")

    # --- the night -------------------------------------------------------------------

    def main(self) -> int:
        o = self.options
        self.started = self.clock()
        # The moment rows count as new tonight, in the database's own clock (naive UTC).
        started_utc = self.started.astimezone(dt.UTC)
        self.started_utc = started_utc.strftime("%Y-%m-%d %H:%M:%S")
        # The start of the twin pass's lookback, in the same clock.
        self.twin_since = (started_utc - dt.timedelta(days=TWIN_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        self.deadline = self.started + dt.timedelta(hours=o.hours)

        log = self.log
        log.say(f"{STARTING}  (pid {self.pid})")
        log.line(f"    repo      {o.repo}")
        log.line(
            f"    ceilings  {o.hours}h, {o.rounds} rounds, ¥{self.cny_cap}, "
            f"{o.tokens} tokens, {o.search_cap} searches"
        )
        log.line(f"    money     ¥{self.cny_cap} tonight, from {self.cny_from}")
        log.line(
            f"    per round {o.findings} findings, {o.audit} audit, {o.risks} risks, {o.pairs} pairs"
        )
        log.line(
            f"    enrich    {o.pipeline} campus(es) with no date or capacity on "
            f"{o.pipeline_budget} articles, then {o.enrich} row(s) below T2 on "
            f"{o.enrich_budget}, first {o.enrich_rounds} round(s)"
        )
        log.line(
            f"    merge={int(o.merge)}  enrich={int(o.do_enrich)}  screen={int(o.screen)}  "
            f"min-confidence {o.min_confidence}"
        )
        where = ", on the local model" if o.local_judgement else ""
        log.line(f"    judgement {o.judgement_effort} effort{where}")
        if o.agent_effort:
            log.line(f"    agent     {o.agent_effort} effort")
        log.line(f"    spend     {self.ledger}")
        if self.director is not None:
            log.line(
                f"{STEPS_LINE}chosen by the director ({self.director.mode}), inside the same ceilings"
            )
        elif self.shadow is not None:
            log.line(f"{STEPS_LINE}the fixed schedule; the director predicts in the background")

        try:
            self.base = self.measure()
        except CountFailed as exc:
            log.say(f"could not take the night's counts ({exc}) — stopping before round 1")
            return 1
        log.line(
            f"    at start  {self.base.findings} finding(s), {self.base.groups} duplicate "
            f"group(s), {self.base.below} row(s) below T2"
        )
        # The lowest each count has been tonight. Progress is a count falling below
        # its minimum, not below last round's value — see WHY ROUNDS above.
        self.lowest = self.base
        self.stale = 0

        log.say("snapshot before anything is deleted")
        try:
            self.first_snapshot = self.backup()
        except SnapshotFailed as exc:
            log.say(f"the snapshot failed ({exc}) — stopping before anything is deleted")
            return 1
        # The free checks' findings, each with the value it flagged, before the night
        # can change either: what later happens to that value is the finding's label.
        log.line(f"    findings recorded for training: {self.findings()}")
        # Rank tonight's values by the suspicion score, free, before anything changes
        # them; round 1's suspects phase re-reads the top of it.
        if o.suspects > 0:
            log.say("suspicion score")
            self.tracker("suspect", "score")
        self.metrics = self.take_metrics()

        try:
            for number in range(1, o.rounds + 1):
                if self.run_round(number):
                    break
        except SnapshotFailed as exc:
            log.say(f"the snapshot failed ({exc}) — stopping before anything else is deleted")
        except CountFailed as exc:
            log.say(f"could not take the night's counts ({exc}) — stopping")
        except Exception:
            # A bug in this loop must cost the rounds left, not the settle and the
            # morning report.
            log.say("the loop failed — stopping")
            log.write(traceback.format_exc())

        if self.capped_:
            self.settle("settle — re-derive and score, after the ceiling")
        self.restore_losses()
        self.morning_report()
        # Tidying up after the night must not fail it.
        with contextlib.suppress(Exception):
            self.on_finish()
        return 0

    def restore_losses(self) -> None:
        """Put back what the night dropped with nothing deciding it (`tracker.losses`).

        Once, after the rounds and before the report, so the report's counts include
        what came back. Against the snapshot from before round 1: a value emptied
        since then whose citation simply stopped claiming it — while the stored
        article still contains its sentence — gets its claim back and its row
        re-derived. A value a ruling cleared is a decision and is left alone. Free.
        """
        if self.first_snapshot is None or not self.first_snapshot.is_file():
            return
        self.log.phase("losses — values dropped with no ruling behind them, put back")
        self.tracker("losses", "--against", self.first_snapshot, "--restore")

    def run_round(self, number: int) -> bool:
        """One round: the fixed schedule's steps or the director's, then the settle.

        True stops the night.
        """
        o = self.options
        log = self.log
        now = self.clock()
        if now >= self.deadline:
            log.say(f"{WALL_CLOCK} of {o.hours}h reached — stopping between rounds")
            return True
        s = self.spent()
        left = int((self.deadline - now).total_seconds()) // 60
        log.say(f"round {number} of {o.rounds}  (¥{s.cny:.2f}, ~{s.tokens} tokens, {left}m left)")
        # Every `--backup-every` rounds after the first: rounds 6, 11, 16 by default.
        if number > 1 and (number - 1) % o.backup_every == 0:
            self.backup()

        self.placed = False
        directed = self.director is not None and not self.fell_back
        if self.directed_round(number) if directed else self.fixed_round(number):
            return True

        # --- reconcile and measure -----------------------------------------------------
        self.settle()
        after = self.measure()
        before = self.base
        log.write(
            f"\n  round {number}: findings {before.findings} -> {after.findings} "
            f"({after.findings - before.findings:+d})  groups {before.groups} -> "
            f"{after.groups} ({after.groups - before.groups:+d})  below-T2 {before.below} "
            f"-> {after.below} ({after.below - before.below:+d})\n"
        )
        if self.progressed(after):
            self.stale = 0
        else:
            self.stale += 1
            low = self.lowest
            log.line(
                f"  nothing below the night's lowest ({low.findings} findings, {low.groups} "
                f"groups, {low.below} below T2): {self.stale} of {o.dry_rounds} stale rounds"
            )
        self.base = after
        self.metrics = self.take_metrics()

        if self.director is not None and not self.fell_back:
            self.check_accuracy()
        if self.stop_why:
            log.say(f"{DIRECTOR_STOPPED}: {self.stop_why}")
            return True
        if after == Counts(0, 0, 0):
            log.say(SETTLED)
            return True
        if self.stale >= o.dry_rounds:
            log.say(
                f"{o.dry_rounds} rounds with no reduction — this is the {FIXED_POINT}, stopping"
            )
            return True
        return False

    # --- steps --------------------------------------------------------------------------

    def run_step(self, step: Step, number: int) -> bool:
        """One step: its ceiling check, its phase line, its commands, its record.

        True stops the night. The commands, their order and the phase line are the
        ones the fixed loop always ran (`tracker.night.steps`); what is new is that
        every step leaves a record of what it cost and what it changed.
        """
        gate = steps_mod.gate(step)
        if gate is not None and self.capped(gate):
            return True
        context = steps_mod.Context(self.options, number, self.twin_since, placed=self.placed)
        self.log.phase(steps_mod.label(step, context))
        started, mark, before = self.clock(), record_mod.ledger_size(self.ledger), self.metrics
        extra = steps_mod.env(step)
        exits: list[int] = []
        stopped = ""
        for command in steps_mod.commands(step, context):
            if command.gate is not None and self.capped(command.gate):
                stopped = command.gate
                break
            exits.append(self.tracker(*command.args, env=extra))
        if step.kind == "news":
            self.placed = True
        self.tonight.add(step)
        if not stopped:
            self.metrics = self.take_metrics()
        self.keep(step, number, context, started, mark, exits, before, stopped)
        if stopped:
            return True
        if step.kind == "twins":
            # The night's yardstick, re-taken now. The news adds rows and findings by
            # design, so measured against the counts from before it every later round
            # looked like no progress: on 2026-10-19 round 2 lowered all three counts,
            # was still above the pre-news minimums, and the night declared a fixed
            # point with 7.4 hours and ¥5 unused. Progress is measured from here.
            self.base = self.measure()
            self.log.line(
                f"    after news {self.base.findings} finding(s), {self.base.groups} "
                f"duplicate group(s), {self.base.below} row(s) below T2"
            )
            self.lowest = self.base
        return False

    def take_metrics(self) -> record_mod.Metrics | None:
        """The counts a step is measured by. A count that fails costs the record, not the night."""
        if self.probe is None:
            return None
        try:
            return self.probe()
        except Exception:
            return None

    def keep(
        self,
        step: Step,
        number: int,
        context: steps_mod.Context,
        started: dt.datetime,
        mark: int,
        exits: list[int],
        before: record_mod.Metrics | None,
        stopped: str,
    ) -> None:
        cost = record_mod.cost_of(record_mod.ledger_slice(self.ledger, mark))
        after = None if stopped else self.metrics
        made = record_mod.StepRecord(
            night=self.night_id or f"{self.started:%Y%m%d-%H%M%S}",
            mode="fixed" if self.director is None else "director",
            round=number,
            index=len(self.records) + 1,
            kind=step.kind,
            origin=step.origin,
            size=step.size,
            started=started.isoformat(timespec="seconds"),
            minutes=round((self.clock() - started).total_seconds() / 60, 2),
            commands=[list(c.args) for c in steps_mod.commands(step, context)],
            exits=exits,
            budget=step.budget,
            fresh=step.fresh,
            rows=list(step.rows),
            fields=list(step.fields),
            effort=step.effort,
            note=step.note,
            cny=cost.cny,
            tokens=cost.tokens,
            searches=cost.searches,
            fetches=cost.fetches,
            before=before._asdict() if before else None,
            after=after._asdict() if after else None,
            result=record_mod.result(steps_mod.KINDS[step.kind].result, before, after),
            expect=dict(step.expect),
            why=step.why,
            version=self.version,
            stopped=stopped,
        )
        self.records.append(made)
        if self.record_path is not None:
            record_mod.append(self.record_path, made)
        if self.plan_file is not None:
            self.plan_file.write("step", round=number, step=step.as_json(), record=made.as_json())

    # --- the fixed schedule ---------------------------------------------------------------

    def fixed_round(self, number: int) -> bool:
        plan = steps_mod.fixed_round(self.options, number)
        if self.director is not None:
            plan = [s for s in plan if self.fixed_allowed(s, fallback=True)]
        return self.run_fixed(plan, number)

    def run_fixed(self, plan: list[Step], number: int) -> bool:
        """The fixed steps in order; on a fixed night the director predicts beside them."""
        for i, step in enumerate(plan):
            if self.shadow is not None and i % CHECK_IN_EVERY == 0:
                self.shadow_check_in(number, plan[i:], at_start=i == 0)
            if self.run_step(step, number):
                return True
        return False

    def fixed_allowed(self, step: Step, *, fallback: bool = False) -> bool:
        """After a director ran part of the night: no once-a-night step twice, no third
        enrich pass, and a second one only on rows not yet asked today."""
        kind = steps_mod.KINDS[step.kind]
        if fallback:
            step.origin = "fallback"
        if kind.once and self.tonight.totals.get(step.kind, 0) > 0:
            return False
        if step.kind in steps_mod.SELECTING:
            passes = self.tonight.passes.get(step.kind, 0)
            if passes >= referee.SELECTING_PASSES:
                return False
            if passes:
                step.fresh = True
        return True

    def finish_fixed(self, number: int) -> bool:
        """The rest of a round the director left: the fixed round's steps not yet run."""
        ran = {r.kind for r in self.records if r.round == number}
        plan = [
            s
            for s in steps_mod.fixed_round(self.options, number)
            if s.kind not in ran and self.fixed_allowed(s, fallback=True)
        ]
        return self.run_fixed(plan, number)

    def fall_back(self, why: str) -> None:
        self.fell_back = why
        self.log.say(f"{DIRECTOR_FAILED} ({why}) — the rest of the night runs the fixed schedule")
        if self.plan_file is not None:
            self.plan_file.write("fallback", why=why)

    # --- the director ----------------------------------------------------------------------

    def directed_round(self, number: int) -> bool:
        """The night's frame (news, twins, free), then the director's steps, re-planned
        after every three. A director that fails hands the rest to the fixed schedule."""
        from tracker.night.director import DirectorFailed, expect_of

        o = self.options
        try:
            plan = self.director.check_in(
                self.check_in_text(number, at_start=True), round_number=number, at_start=True
            )
        except DirectorFailed as exc:
            self.fall_back(str(exc))
            return self.fixed_round(number)
        notes: list[str] = []
        frame: list[Step] = []
        if o.do_discover and number == 1:
            fixed = steps_mod.fixed_round(o, 1)
            news = referee.news_step(plan.news, o, fixed[0], notes)
            news.expect = expect_of((plan.news or {}).get("expect"))
            frame += [news, dataclasses.replace(fixed[1], origin="frame")]
        frame.append(Step("free", origin="frame"))
        if plan.stop:
            self.stop_why = plan.why or "no reason given"
        verdict = referee.review(
            [] if plan.stop else plan.steps, self.rules, self.tonight, first_round=number == 1
        )
        self.answer_director(notes + verdict.notes, verdict.steps, frame=frame)
        for step in frame:
            if self.run_step(step, number):
                return True
        return self.run_directed(verdict.steps, number, plan.end_round)

    def run_directed(self, pending: list[Step], number: int, end_round: bool) -> bool:
        from tracker.night.director import DirectorFailed

        ran = asked_at = 0
        while True:
            more = not pending and not end_round and not self.stop_why
            due = pending and ran - asked_at >= CHECK_IN_EVERY and not self.stop_why
            if more or due:
                asked_at = ran
                try:
                    plan = self.director.check_in(
                        self.check_in_text(number, at_start=False, upcoming=pending),
                        round_number=number,
                        at_start=False,
                    )
                except DirectorFailed as exc:
                    self.fall_back(str(exc))
                    return self.finish_fixed(number)
                if plan.stop:
                    self.stop_why = plan.why or "no reason given"
                    pending = [s for s in pending if s.origin == "floor"]
                    self.answer_director(["stopping after this round settles"], pending)
                    end_round = True
                else:
                    verdict = referee.review(
                        plan.steps, self.rules, self.tonight, first_round=number == 1
                    )
                    self.answer_director(verdict.notes, verdict.steps)
                    pending, end_round = verdict.steps, plan.end_round or not verdict.steps
            if not pending:
                return False
            step = pending.pop(0)
            if self.run_step(step, number):
                return True
            ran += 1

    def answer_director(
        self, notes: list[str], steps: list[Step], *, frame: list[Step] = ()
    ) -> None:
        """The referee's verdict, to the director and the log."""
        for note in notes:
            self.log.line(f"{REFEREE_LINE}{note}")
        planned = ", ".join(f"{s.kind} {s.size}" for s in [*frame, *steps]) or "nothing"
        text = (
            "will run: "
            + planned
            + (
                "\nchanges to your plan:\n" + "\n".join(f"- {n}" for n in notes)
                if notes
                else "\nno changes"
            )
        )
        self.director.answer(text)

    def shadow_check_in(self, number: int, upcoming: list[Step], *, at_start: bool) -> None:
        """On a fixed night: what the director would run instead, and its prediction for
        each of the next fixed steps. Nothing it says runs; a failure only stops it."""
        try:
            plan = self.shadow.check_in(
                self.check_in_text(number, at_start=at_start, upcoming=upcoming, shadow=True),
                round_number=number,
                at_start=at_start,
            )
        except Exception as exc:
            self.log.line(f"    the director's background predictions stopped: {exc}")
            if self.plan_file is not None:
                self.plan_file.write("fallback", why=str(exc), shadow=True)
            self.shadow = None
            self.shadow_stopped = str(exc)
            return
        from tracker.night.director import expect_of

        for prediction in plan.predictions:
            try:
                index = int(prediction.get("index", -1))
            except (TypeError, ValueError):
                continue
            if 0 <= index < min(len(upcoming), CHECK_IN_EVERY):
                upcoming[index].expect = expect_of(prediction)
        if self.plan_file is not None:
            self.plan_file.write(
                "shadow", round=number, steps=[s.as_json() for s in plan.steps], why=plan.why
            )
        self.shadow.answer("recorded; on a fixed night nothing you plan runs")

    def check_in_text(
        self, number: int, *, at_start: bool, upcoming: Sequence[Step] = (), shadow: bool = False
    ) -> str:
        """What the director is told at a check-in: what ran, what it cost, what is left."""
        o = self.options
        since = [r for r in self.records if r.index > self.reported]
        self.reported = len(self.records)
        where = "start of the round" if at_start else f"after {len(since)} more step(s)"
        lines = []
        if shadow:
            lines.append("SHADOW NIGHT: the fixed schedule runs tonight and you do not control it.")
        lines.append(f"CHECK-IN: round {number} of at most {o.rounds}, {where}.")
        lines.append(self.limits_text())
        if since:
            lines.append("RAN SINCE YOUR LAST CHECK-IN:")
            lines += [f"- {record_line(r)}" for r in since]
        if self.metrics is not None:
            m = self.metrics
            lines.append(
                f"COUNTS NOW: {m.below} rows below T2, {m.findings} findings, {m.groups} "
                f"duplicate groups, {m.values} stored values, {m.rows} rows"
            )
        if self.guard_lines:
            lines.append("ACCURACY (against tonight's first snapshot): " + self.guard_lines[-1])
            lines += [f"- {line}" for line in self.guard_items[:ATTRIBUTED_SHOWN]]
        lines.append(
            "USED TONIGHT: "
            + (", ".join(f"{k} {v}" for k, v in sorted(self.tonight.totals.items())) or "nothing")
            + f"; enrich passes {dict(self.tonight.passes) or 0}; named-row steps "
            f"{self.tonight.rows_steps} of {referee.ROWS_STEPS}"
        )
        if shadow:
            lines.append("NEXT FIXED STEPS:")
            lines += [
                f"[{i}] {s.kind} size {s.size}" + (f", budget {s.budget}" if s.budget else "")
                for i, s in enumerate(upcoming[:CHECK_IN_EVERY])
            ]
            lines.append(
                "Call submit_plan with the steps you would run instead, and in `predictions` "
                "your expectation (cny, searches, minutes, result) for each listed step by index."
            )
            return "\n".join(lines)
        if at_start and number == 1 and o.do_discover:
            fixed = steps_mod.fixed_round(o, 1)[0]
            lines.append(
                "THIS ROUND STARTS WITH the news (fixed: "
                f"{fixed.searches} searches, {fixed.size} queued articles, {fixed.baselines} "
                "first looks; set yours in `news`), the twin pass, then the free phase. "
                "Then your steps."
            )
        elif at_start:
            lines.append("THIS ROUND STARTS WITH the free phase. Then your steps.")
        if upcoming:
            lines.append(
                "STILL PLANNED (your answer replaces these): "
                + ", ".join(f"{s.kind} {s.size}" for s in upcoming)
            )
        lines.append("Plan the rest of this round with submit_plan, or stop_night.")
        return "\n".join(lines)

    def limits_text(self) -> str:
        o = self.options
        s = self.spent()
        try:
            searched = spend.searches(spend.read(self.ledger))
        except Exception:
            searched = 0
        left = max(0, int((self.deadline - self.clock()).total_seconds()) // 60)
        return (
            f"LIMITS: ¥{s.cny:.2f} of ¥{self.cny_cap} spent; {searched} of {o.search_cap} "
            f"searches; {left} minutes left; your own spend ¥{self.director_spent():.2f}; "
            f"steps {self.tonight.steps} of {referee.MAX_STEPS}"
        )

    def history_text(self) -> str:
        """The night so far as a table: for a folded conversation and `night_so_far`."""
        rows = [f"r{r.round} {record_line(r)}" for r in self.records] or ["nothing has run yet"]
        return "\n".join([*rows, self.limits_text()])

    def check_accuracy(self) -> None:
        """The accuracy check after a directed round; a net loss hands the night back."""
        if self.guard is None or self.first_snapshot is None:
            return
        try:
            found = self.guard(self.first_snapshot)
        except Exception as exc:
            self.log.line(f"{ACCURACY_LINE}could not be checked: {exc}")
            return
        self.guard_lines.append(found.summary())
        self.log.line(f"{ACCURACY_LINE}{found.summary()}")
        self.guard_items = [self.attribute(item) for item in getattr(found, "items", ())]
        for line in self.guard_items[:ATTRIBUTED_SHOWN]:
            self.log.line(f"      {line}")
        if found.worse:
            self.fall_back(f"the answer key lost {found.net_loss} answer(s) net")

    def attribute(self, item: Any) -> str:
        """One accuracy item and the step that moved it, from when its citation was read.

        On 2026-11-06 the director was told only that one answer had stopped being right,
        blamed the rulings, and ran six logic findings all night; the news read had moved
        it. A value whose citation was not read tonight was moved by a ruling, a merge or
        a re-derive, not by a reading.
        """
        read = getattr(item, "read_at", None)
        if read is None:
            if item.kind == "unbacked":
                return item.text
            return (
                f"{item.text} — no citation read tonight: a ruling, a merge or a re-derive moved it"
            )
        for r in reversed(self.records):
            try:
                start = dt.datetime.fromisoformat(r.started)
            except ValueError:
                continue
            start = (start.astimezone(dt.UTC) if start.tzinfo else start).replace(tzinfo=None)
            end = start + dt.timedelta(minutes=r.minutes + 1)
            if start - dt.timedelta(minutes=1) <= read <= end:
                kind = steps_mod.KINDS.get(r.kind)
                if kind is not None and not kind.choosable:
                    who = "placed by the night; no director chooses it"
                else:
                    who = {
                        "director": "a step you chose",
                        "floor": "a floor the referee added",
                        "fallback": "the fixed schedule",
                        "fixed": "the fixed schedule",
                    }.get(r.origin, r.origin)
                return f"{item.text} — read by the {r.kind} step, round {r.round} ({who})"
        return f"{item.text} — read at {read:%H:%M} UTC, outside any step of tonight's"

    def progressed(self, now: Counts) -> bool:
        """Whether any count fell below the lowest it has been tonight; lowers those
        minimums. Against the minimum rather than last round's value, so a count that
        wobbles up and back down is not progress; see WHY ROUNDS above."""
        low = self.lowest
        moved = now.findings < low.findings or now.groups < low.groups or now.below < low.below
        self.lowest = Counts(
            min(now.findings, low.findings), min(now.groups, low.groups), min(now.below, low.below)
        )
        return moved

    # --- the morning report -------------------------------------------------------------

    def morning_report(self) -> None:
        log = self.log
        log.say("what is left")
        self.tracker("clean")
        log.line()
        self.tracker("logic", "check", head=12)
        # How the figures relate (勾稽关系), and the change since last night: one line
        # per relationship, `!` on a held one that breaks (a bug). Recorded, so
        # tomorrow's report can say what tonight moved.
        log.say("how the figures relate")
        self.tracker("invariants", "--record")
        log.line()
        self.tracker("duplicates", head=2)
        # The opposite question: one row holding several campuses' citations, so its
        # figures are a mixture. Read-only — the count and the worst row; a person
        # splits them.
        self.tracker("duplicates", "mixed", head=2)
        # Refit both suspicion models on everything the night added (verdicts, findings,
        # the snapshots' history). Free; each keeps its old weights unless the refit
        # ranks the answer key at least as well.
        log.say("retrain the suspicion score")
        if self.first_snapshot is not None:
            self.tracker("suspect", "train", "--snapshots", self.first_snapshot.parent)
        else:
            self.tracker("suspect", "train")

        elapsed = int((self.clock() - self.started).total_seconds()) // 60
        log.say(f"{COMPLETE} — {elapsed}m, {self.spend_summary()}")
        # The night's spend by phase — command, and the stage within it where one was
        # recorded — dearest first, with how much of the prompt the provider served
        # from cache. A phase whose hit rate falls is one whose prompt prefix is being
        # disturbed.
        for line in self.spend_report():
            log.line(line)

        # How the news came in: per source, what was seen, kept, read, failed to load,
        # set aside unread and found; how late it was; and whatever is wrong with the
        # intake itself — no discovery tonight, a feed gone quiet, fresh news left
        # waiting, search credit running low.
        log.say("how the news came in")
        self.tracker("news", "report", "--since", self.started_utc)

        # Every value the night changed, each with the sentence now behind it. The
        # quality counts above cannot see a wrong value that has a real quote; a reader
        # can, in the minutes this list takes to read. `tracker logic rule-out` takes
        # back a bad one.
        log.say("what the night changed")
        if self.first_snapshot is not None and self.first_snapshot.is_file():
            self.tracker("changes", "--against", self.first_snapshot)
        else:
            log.line("    no snapshot from before round 1 to compare against")
        log.line()
        log.line("    Anything still listed needs either a person or a command that does not")
        log.line("    exist yet. The block findings are the second kind — see WHAT IT CANNOT DO")
        log.line("    in tracker/overnight.py.")
        if self.records and (
            self.director is not None or self.shadow_used or self.record_path is not None
        ):
            log.say("how the night was chosen")
            for line in self.choice_report():
                log.line(line)

    def choice_report(self) -> list[str]:
        """Who chose the steps, what each cost and did, against what was predicted."""
        out = []
        if self.director is None:
            out.append(
                "    the fixed schedule"
                + ("; the director predicted in the background" if self.shadow_used else "")
            )
        else:
            out.append(f"    chosen by the director ({self.director.mode})")
            if self.fell_back:
                out.append(f"    it failed and the fixed schedule took over: {self.fell_back}")
            if self.stop_why:
                out.append(f"    it stopped the night: {self.stop_why}")
        if self.shadow_stopped:
            out.append(f"    the background predictions stopped: {self.shadow_stopped}")
        out.append(f"    the director's own spend: ¥{self.director_spent():.2f}")
        out.append(
            "    round  step        size  origin    ¥        searches  minutes  result (expected)"
        )
        for r in self.records:
            expected = r.expect.get("result")
            out.append(
                f"    {r.round:>5}  {r.kind:<10} {r.size:>5}  {r.origin:<8} {r.cny:>7.2f}  "
                f"{r.searches:>8}  {r.minutes:>7.1f}  "
                f"{'?' if r.result is None else r.result}"
                + (f" ({expected:g})" if expected is not None else "")
                + (" FAILED" if r.failed else "")
            )
        scored = [r for r in self.records if r.result is not None and "result" in r.expect]
        if scored:
            miss = sum(abs(r.result - r.expect["result"]) for r in scored) / len(scored)
            out.append(f"    predictions: {len(scored)} step(s), off by {miss:.1f} on average")
        costed = [r for r in self.records if "cny" in r.expect]
        if costed:
            miss = sum(abs(r.cny - r.expect["cny"]) for r in costed) / len(costed)
            out.append(
                f"    cost predictions: {len(costed)} step(s), off by ¥{miss:.2f} on average"
            )
        if self.guard is not None and self.first_snapshot is not None:
            try:
                found = self.guard(self.first_snapshot)
                out.append(f"{ACCURACY_LINE}{found.summary()}")
                lines = [self.attribute(i) for i in getattr(found, "items", ())] or found.detail
                out += [f"      {line}" for line in lines[:12]]
            except Exception as exc:
                out.append(f"{ACCURACY_LINE}could not be checked: {exc}")
        return out

    def spend_summary(self) -> str:
        """The closing line's money: the model bill the ceiling counts, then what the
        web searches cost beside it — prepaid credit, outside the ceiling by design,
        and on 2026-10-19 780 of them, about ¥5.54 against ¥9.89 on models, which this
        line used to leave out. Falls back to the bare figures rather than failing the
        report."""
        try:
            return spend.summary(spend.read(self.ledger))
        except Exception:
            s = self.spent()
            return f"¥{s.cny:.2f}, ~{s.tokens} tokens"

    def spend_report(self) -> list[str]:
        try:
            return spend.report(spend.read(self.ledger))
        except Exception:
            return ["    (no report)"]


def record_line(r: record_mod.StepRecord) -> str:
    """One step as the director reads it: what it was, what it cost, what it did."""
    kind = steps_mod.KINDS[r.kind]
    expected = r.expect.get("result")
    line = (
        f"{r.kind} {r.size} {kind.unit} ({r.origin}): ¥{r.cny:.2f}, {r.searches} searches, "
        f"{r.minutes:.1f} min, result {'?' if r.result is None else r.result} {kind.result}"
    )
    if expected is not None:
        line += f" (you expected {expected:g})"
    if r.failed:
        line += (
            f"; FAILED (exit {', '.join(map(str, r.exits))}) — a crash, not a finding of nothing"
        )
    if r.stopped:
        line += f"; the {r.stopped} ceiling stopped the night inside it"
    return line


# --- entry point ------------------------------------------------------------------------


def _local_now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def choose_mode(o: Options, today: dt.date) -> str:
    """`fixed` or `director` tonight. `auto` alternates by the date's day count, so the
    two take turns across month ends too (the 31st and the 1st are both odd days)."""
    if o.replay is not None:
        return "director"
    if o.mode != "auto":
        return o.mode
    return "director" if today.toordinal() % 2 == 0 else "fixed"


def director_spend(ledger: Path) -> float:
    """What the director's own calls have cost tonight (ledger command `director`)."""
    from tracker.night.director import LEDGER_COMMAND

    try:
        return spend.total([c for c in spend.read(ledger) if c.command == LEDGER_COMMAND])
    except Exception:
        return 0.0


def build(o: Options, stream: BinaryIO) -> Night:
    """The real night: phases as processes, counts and snapshots on the configured DB."""
    from tracker import __version__
    from tracker.config import get_settings
    from tracker.night import guard as guard_mod

    settings = get_settings()
    db = settings.resolve_db()
    log = Log(stream, _local_now)
    named = os.environ.get("TRACKER_SPEND_LEDGER")
    # The shell names tonight's ledger; one run by hand gets its own beside the log.
    ledger = Path(named) if named else o.repo / "data" / "runs" / ledger_name(_local_now())
    started = _local_now()
    night = Night(
        options=o,
        log=log,
        runner=process_runner(log),
        counts=lambda: count(db),
        spent=lambda: ledger_spend(ledger),
        snapshot=lambda: snapshot_db(db, o.repo, _local_now()),
        clock=_local_now,
        ledger=ledger,
        pid=int(os.environ.get("OVERNIGHT_PID") or os.getpid()),
        findings=lambda: night_findings(db, _local_now()),
        probe=lambda: record_mod.probe(db),
        record_path=ledger.parent / record_mod.FILENAME,
        # A citation read after the first snapshot was written is tonight's.
        guard=lambda first: guard_mod.check(
            db,
            first,
            guard_mod.week_old(first.parent, _local_now()),
            since=dt.datetime.fromtimestamp(first.stat().st_mtime, dt.UTC).replace(tzinfo=None),
        ),
        director_spent=lambda: director_spend(ledger),
        night_id=f"{started:%Y%m%d-%H%M%S}",
        version=__version__,
    )
    try:
        attach_director(night, settings, db, ledger.parent, started)
    except Exception as exc:  # a director that cannot be set up costs nothing but itself
        night.director = night.shadow = None
        night.shadow_used = False
        log.say(
            f"the director could not be set up ({type(exc).__name__}: {exc}) — the fixed schedule runs"
        )
    return night


def attach_director(
    night: Night, settings: Any, db: Path, runs: Path, started: dt.datetime
) -> None:
    """Tonight's director: choosing on a directed night, predicting on a fixed one."""
    from sqlalchemy import select

    from tracker import attempts
    from tracker.db import open_db, session_scope
    from tracker.gapfill import FILLABLE_FIELDS
    from tracker.models import Project, utcnow
    from tracker.night import briefing
    from tracker.night import director as director_mod

    o = night.options
    mode = choose_mode(o, started.date())
    today = utcnow().date()
    records = record_mod.read(night.record_path) if night.record_path else []

    def existing(ids: Sequence[int]) -> set[int]:
        engine = open_db(db)
        try:
            with session_scope(engine, commit=False) as session:
                return set(session.scalars(select(Project.id).where(Project.id.in_(list(ids)))))
        finally:
            engine.dispose()

    def asked_today(ids: Sequence[int]) -> set[int]:
        engine = open_db(db)
        try:
            with session_scope(engine, commit=False) as session:
                return {
                    p.id
                    for p in session.scalars(select(Project).where(Project.id.in_(list(ids))))
                    if attempts.asked_since(p, today)
                }
        finally:
            engine.dispose()

    night.rules = referee.Rules(
        o,
        efforts={
            steps_mod.AGENT_EFFORT: o.agent_effort or settings.deepseek_agent_effort,
            steps_mod.JUDGEMENT_EFFORT: o.judgement_effort,
            steps_mod.EXTRACTION_EFFORT: o.extraction_effort or settings.deepseek_extraction_effort,
        },
        allowance=referee.allowance(
            o, record_mod.per_night(record_mod.recent(records), mode="fixed")
        ),
        existing=existing,
        asked_today=asked_today,
        fillable=FILLABLE_FIELDS,
    )
    night.plan_file = director_mod.PlanFile(
        director_mod.plan_path(runs, night.night_id), _local_now
    )
    if o.replay is not None:
        night.director = director_mod.ReplayDirector(o.replay)
        return
    if mode == "fixed" and o.mode == "fixed":
        return  # asked for a plain fixed night: no background predictions either
    if not settings.has_api_key():
        return

    from tracker.llm import build_extractor

    state = briefing.read_state(db, today)
    conditions = known_conditions(settings)
    note = director_mod.read_note(runs)
    limits = (
        f"money ceiling ¥{night.cny_cap} tonight ({night.cny_from}); search cap {o.search_cap}; "
        f"{o.hours} hours from the start; at most {o.rounds} rounds; your own budget "
        f"¥{director_mod.BUDGET_CNY:g}; started {started:%Y-%m-%d %H:%M}"
    )
    text = briefing.briefing(
        state=state,
        prices=record_mod.prices(records),
        limits=limits,
        conditions=conditions,
        note=note,
        mode="a directed night: you choose the steps"
        if mode == "director"
        else "a fixed night: you predict in the background",
        last_nights=briefing.last_nights(records),
    )
    lookups = briefing.Lookups(
        db, records=lambda: [*records, *night.records], tonight=night.history_text, today=today
    ).tools()
    director = director_mod.ModelDirector(
        extractor=build_extractor(settings, model=settings.deepseek_reasoning_model, effort="high"),
        system=director_mod.system_prompt(o),
        briefing=text,
        lookups=lookups,
        plan_file=night.plan_file,
        spent=night.director_spent,
        clock=_local_now,
        history=night.history_text,
        mode="director" if mode == "director" else "shadow",
    )
    if mode == "director":
        night.director = director
        night.on_finish = lambda: director_mod.clear_note(runs)
    else:
        night.shadow = director
        night.shadow_used = True


def known_conditions(settings: Any) -> list[str]:
    """What the director should know about tonight's world, each a plain sentence."""
    out = []
    try:
        from tracker.llm import deepseek_balance

        found = deepseek_balance(settings)
        if found is not None and found[1] <= 0:
            out.append(
                f"DeepSeek's balance is {found[1]:.2f}: pages our own clients cannot load and "
                "that escalate to its fetch tool are refused tonight"
            )
    except Exception:
        pass
    try:
        from tracker.ingest.desk import serper_credit

        credit = serper_credit(settings)
        if credit is not None:
            out.append(f"Serper search credit left: {credit:,}")
    except Exception:
        pass
    return out


def night_findings(db: Path, now: dt.datetime) -> int:
    """Record the free checks' findings for training (`tracker.labels`). Never raises."""
    from tracker import labels
    from tracker.db import open_db, session_scope

    try:
        engine = open_db(db)
        try:
            with session_scope(engine, commit=False) as session:
                return labels.record_findings(session, night=now.date().isoformat())
        finally:
            engine.dispose()
    except Exception:  # a training log must not cost the night
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in {"run", "check"}:
        print(
            "usage: python -m tracker.overnight {run|check} [options]  (--help for options)",
            file=sys.stderr,
        )
        return 2
    options = parse_options(args[1:])  # exits 2 on a bad option, 0 after --help
    if args[0] == "check":
        return 0
    night = build(options, sys.stdout.buffer)
    # The shell passes on a signal it is sent (see `run_night` in overnight.sh). As an
    # exception it unwinds through the running phase's `subprocess.run`, which kills
    # that phase, and past the loop's own error handling: a stopped night is cut off,
    # and the log must not say it completed.
    signal.signal(signal.SIGTERM, _stopped)
    try:
        return night.main()
    except Stopped as stop:
        night.log.say(f"stopped by signal {stop.signum} — the rest of the night is cut off")
        return 128 + stop.signum


class Stopped(BaseException):
    """A signal asked the night to stop. Not an `Exception`: nothing may swallow it."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def _stopped(signum: int, _frame) -> None:
    raise Stopped(signum)


if __name__ == "__main__":
    raise SystemExit(main())
