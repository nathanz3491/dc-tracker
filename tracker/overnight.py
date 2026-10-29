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
            nothing above this line can move one. It runs in the FIRST round only
            (`--enrich-rounds`): what it reads changes when articles are ingested,
            not when a merge or a ruling lands, and on the nights of 2026-09-24 to
            09-29 it was 86% of the bill, round after round, for one new fact.
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
Go's flash allowance is $80 a month, and other work runs on the same plan, so the
tracker keeps to 85% of it: ¥482.80 over a rolling 30 days, counting the console's
and hand-run commands' daytime ledgers with the nights' (from 10-26), ¥16.09 a night,
less a seventh of whatever the last 29 days spent over their share. Unspent money is
not carried over: tried on the 10-25 ledgers, it ran four weeks above the share on
October's savings and then left two nights with nothing. Before that it was a flat
¥15 that no night reached: from 10-19 to 10-25 the loop spent ¥9.47-12.86 a night and
stopped at its fixed point with seven hours to spare, so the volumes below were
raised the same day. `--cny N` still sets a flat ceiling for one night. Web searches
are a separate bill (prepaid Serper credit, about ¥0.007 each): the ledger records
them and `--search-cap` (default 2000 a night, `TRACKER_SEARCH_CAP`) stops every
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
firing.

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
night's ¥16.09 trims; enrich runs after the duplicate, audit, obstacle,
contradiction and tranche passes in a round, so a ceiling reached early cuts
reading, never settling. An overshoot is one enrich pass (about ¥6), and the next
nights' allowance pays it back.

Later rounds are cheaper than the first: every paid phase records what it answered
or could not decide, keyed on the evidence it was shown, and does not re-offer it
until that evidence changes or a month passes (`tracker/declines.py`).

The per-item judgements — risks, audit, enrich's settle step — run at `high`
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
(`tracker.webui.statussite.parse_nights`) and `overnight.sh --status` greps it, so
the words and line shapes they look for are constants below, imported from here by
the status page, and the formats — `[YYYY-MM-DD HH:MM:SS] msg`, `--- HH:MM:SS  msg`,
`  round N: findings A -> B (+d) ...` — are byte-for-byte what the shell printed, so
every old log still parses.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final, NamedTuple, Protocol

from tracker import spend

# --- the log contract ---------------------------------------------------------------
# What `tracker.webui.statussite` and `overnight.sh --status` read. Change one of these
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

#: The twin pass: rows created in the last TWIN_DAYS days, at most TWINS of them a
#: night. A lookback rather than tonight alone, because a night whose pass was capped,
#: crashed or blind (it ran before the rows had a county until 2026-10-20) was never
#: revisited: the 10-19 night left about 16 twins standing. A pair already asked on
#: the same evidence is held back (`declines`), so the lookback re-asks only what
#: changed. The cap is what keeps a 100-row night from spending the budget here.
TWINS: Final = 60
TWIN_DAYS: Final = 3

#: The ceiling when `--cny` names none and no allowance can be read: the flat ceiling
#: every night ran under before the allowance existed.
FALLBACK_CNY: Final = "15"

#: Searches the news watch leaves for the research that runs after it.
SEARCHES_KEPT: Final = 500

EFFORTS: Final = ("low", "high", "max")


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
    # The first enrich pass: campuses being built or permitted that lack an online
    # date or planned capacity, largest first, asked only those two fields (`enrich
    # --pipeline`). On 10-26, 111 of 258 under construction and 135 of 169 in
    # permitting had no date, so the supply chart and the delay flags could not see
    # them; the T2 pass weighs every field alike and rarely chose them.
    pipeline: int = 30
    pipeline_budget: int = 120
    enrich: int = 50
    enrich_budget: int = 200
    enrich_rounds: int = 1
    discover: int = 900
    # Have a model read the queued headlines first and set aside what cannot be campus
    # news (`ingest queue --screen`). On, for pages about a place abroad only:
    # measured on 5,272 past reads on 2026-10-26, that spares 230 useless reads and
    # loses 4 of 2,841 useful ones; every other kind cost real news
    # (`headlines.ACTED_ON`).
    screen: bool = True
    baselines: int = 300
    search_cap: int = 2000
    dates: int = 150
    reread: int = 120
    recheck: int = 120
    min_confidence: str = "0.85"
    merge: bool = True
    #: None: tonight's share of the month (`tracker.spend allowance`).
    cny: str | None = None
    tokens: int = 25_000_000
    judgement_effort: str = "high"
    extraction_effort: str | None = None
    agent_effort: str | None = None
    local_judgement: bool = False
    backup_every: int = 5
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
        "--nest",
        type=int,
        default=d.nest,
        help="campuses whose tranches the model nests, per round (default 20)",
    )
    a(
        "--pipeline",
        type=int,
        default=d.pipeline,
        help="campuses being built or permitted with no online date or capacity to enrich first (default 30); 0 to skip",
    )
    a(
        "--pipeline-budget",
        type=int,
        default=d.pipeline_budget,
        help="articles that pass may read (default 120)",
    )
    a(
        "--enrich",
        type=int,
        default=d.enrich,
        help="projects below T2 to enrich after it (default 50); 0 to skip",
    )
    a(
        "--enrich-budget",
        type=int,
        default=d.enrich_budget,
        help="articles that pass may read (default 200)",
    )
    a(
        "--enrich-rounds",
        type=int,
        default=d.enrich_rounds,
        help="enrich in the first N rounds only (default 1)",
    )
    a(
        "--discover",
        type=int,
        default=d.discover,
        help="queued articles to read, the news first (default 900); 0 to skip discovery, the news watch and the read altogether",
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
        help="web searches the whole night may send (default 2000); the news watch may use all but 500, which the research keeps",
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
        help="articles to re-read under the current prompt, first round (default 120); 0 to skip",
    )
    a(
        "--recheck",
        type=int,
        default=d.recheck,
        help="articles holding a refusal from before the gate's last revision to re-read, first round (default 120); 0 to skip",
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
        help="also stop when prompt+reply tokens pass N (default 25,000,000)",
    )
    a(
        "--judgement-effort",
        choices=EFFORTS,
        default=d.judgement_effort,
        help="reasoning for risks, audit and settle (default high)",
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
        help="send risks, audit and settle to the local model (Ollama)",
    )
    a(
        "--backup-every",
        type=int,
        default=d.backup_every,
        help="snapshot every N rounds (default 5). Always before round 1.",
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
    """The night's three counts, from one session."""
    from tracker import clean, logic
    from tracker.audit import settled_codes
    from tracker.capex import duplicate_groups, suspected_duplicates
    from tracker.models import Project

    settled: dict[int, set[str]] = {}
    todo = 0
    for finding in logic.review(session).findings:
        if finding.project_id not in settled:
            row = session.get(Project, finding.project_id)
            settled[finding.project_id] = settled_codes(row) if row else set()
        if finding.code not in settled[finding.project_id]:
            todo += 1
    pairs = [p for p in suspected_duplicates(session) if set(p.kinds) - {"name"}]
    below = sum(1 for card in clean.scan(session).cards if card.tier < 2)
    return Counts(todo, len(duplicate_groups(pairs)), below)


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

    capped_: bool = False
    first_snapshot: Path | None = None

    def __post_init__(self) -> None:
        self.cny_cap, self.cny_from = money_ceiling(self.options)
        self.env = night_env(self.options, self.cny_cap, self.ledger)

    # --- helpers ---------------------------------------------------------------------

    def tracker(self, *args: object, head: int | None = None) -> None:
        """One phase. Its exit status is ignored: see SAFETY in the module docstring."""
        try:
            self.runner([str(a) for a in args], env=self.env, head=head)
        except Exception as exc:  # could not even start it: that phase is lost
            self.log.line(f"    could not run `tracker {' '.join(map(str, args))}`: {exc}")

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
        self.morning_report()
        return 0

    def run_round(self, number: int) -> bool:
        """One round, in the order the module docstring argues. True stops the night."""
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

        placed = False
        # --- news: first, so nothing later can cost the day's news ----------------
        # The one phase that adds rows. First round only: the queue does not change
        # between rounds. Polling the feeds and walking the archives costs nothing;
        # the news watch is one search a campus (prepaid credit, under
        # `--search-cap`); each article read is one extraction (about ¥0.03), and the
        # read's identity check — one call, only when a record nearly matches a row —
        # is what keeps a campus already held under another name from becoming its
        # twin. Rounds after this one run `duplicates` over whatever it added.
        if o.do_discover and number == 1:
            log.phase(
                f"news — feeds and archives, the campus news watch, {o.discover} "
                "queued article(s) read"
            )
            self.tracker("discover", "--no-show")
            searches = (
                o.search_cap - SEARCHES_KEPT if o.search_cap > SEARCHES_KEPT else o.search_cap
            )
            self.tracker("news", "--baselines", o.baselines, "--max-queries", searches)
            if self.capped("news"):
                return True
            self.tracker(
                "ingest", "queue", "--new-first", "--limit", o.discover,
                "--screen" if o.screen else "--no-screen",
            )  # fmt: skip

            # Where the new rows are, before asking what they duplicate. The twin pass
            # pairs a row with an existing one through a shared town or county, or a
            # distance — and a row fresh from an article usually has a town and
            # nothing else: on 2026-10-19, 74 of 185 recent rows had no county and
            # none had coordinates, because `ingest geo` ran in the free phase, after
            # the pass. So a campus filed under its county met its own twin filed
            # under the town and saw nothing in common. The Census lookup is free and
            # takes seconds.
            self.tracker("ingest", "geo")
            placed = True

            # One attempt to fold per row recently added, each against its likeliest
            # twin wherever that twin is filed (`duplicates resolve --created-since`).
            # The regular pass only sees pairs within one town or one company, and on
            # 2026-10-05 a new 10 GW PORTS campus row sat unpaired beside its
            # duplicate because one row named the town and a different company, the
            # other the county.
            if self.capped("duplicates"):
                return True
            log.phase(
                f"duplicates — one attempt per row created in the last {TWIN_DAYS} "
                f"day(s), {TWINS} at most"
            )
            if o.merge:
                self.tracker(
                    "duplicates", "resolve", "--merge", "--min-confidence", o.min_confidence,
                    "--created-since", self.twin_since, "--limit", TWINS,
                )  # fmt: skip
            else:
                self.tracker(
                    "duplicates", "resolve", "--created-since", self.twin_since, "--limit", TWINS
                )

            # The night's yardstick, re-taken now. The news adds rows and findings by
            # design, so measured against the counts from before it every later round
            # looked like no progress: on 2026-10-19 round 2 lowered all three counts,
            # was still above the pre-news minimums, and the night declared a fixed
            # point with 7.4 hours and ¥5 unused. Progress is measured from here.
            self.base = self.measure()
            log.line(
                f"    after news {self.base.findings} finding(s), {self.base.groups} "
                f"duplicate group(s), {self.base.below} row(s) below T2"
            )
            self.lowest = self.base

        # --- free: no model, no cost ---------------------------------------------------
        log.phase("free — dates, geo, scope, derive, logic --auto")
        # First round only, the date check also asks publishers: one plain request per
        # page, no model. Without it only dates written in a URL's path are found, and
        # 1,836 citations sat undated night after night — the date the morning email
        # decides "is this news" by. Each page is asked once per 90 days (migration
        # 0035), newest first, so the limit reaches new articles rather than re-asking
        # a Census table that will never state one.
        if number == 1 and o.dates > 0:
            self.tracker("backfill", "dates", "--apply", "--refetch", "--yes", "--limit", o.dates)
        else:
            self.tracker("backfill", "dates", "--apply")
        # Not twice in one round: the news phase placed every row a minute ago and
        # nothing since has added one (the date check writes dates only).
        if not placed:
            self.tracker("ingest", "geo")
        self.tracker("backfill", "scope", "--apply")
        self.tracker("backfill", "derive")
        self.tracker("logic", "resolve", "--auto", "--apply")

        # --- duplicates: before anything reads a row it may fold -----------------------
        # Ahead of audit, risks and logic, because each of those pays to read one row's
        # sources: on 2026-10-19, 12 of the 14 first-round logic findings were on rows
        # created that evening, and #1732 was worked twice and #1742 for 15 tool steps
        # before the duplicate pass that ran after them merged both away.
        if self.capped("duplicates"):
            return True
        log.phase(f"duplicates — {o.pairs} pair(s)")
        if o.merge:
            self.tracker(
                "duplicates", "resolve", "--merge", "--limit", o.pairs,
                "--min-confidence", o.min_confidence,
            )  # fmt: skip
        else:
            self.tracker("duplicates", "resolve", "--limit", o.pairs)

        # --- audit: a T1 gate, and cheap -----------------------------------------------
        if self.capped("audit"):
            return True
        log.phase(f"audit — implausible figures, {o.audit} at a time")
        self.tracker("audit", "resolve", "--no-ask", "--limit", o.audit)

        if self.capped("risks"):
            return True
        log.phase(f"risks — unquoted obstacles, {o.risks} at a time")
        self.tracker("risks", "confirm", "--limit", o.risks)

        # --- the agent phases ----------------------------------------------------------
        if self.capped("logic"):
            return True
        log.phase(f"logic — a model reads the sources, {o.findings} at a time")
        self.tracker("logic", "resolve", "--limit", o.findings)

        # After logic, which may rule a claim out and re-derive a row's tranches: the
        # review then reads the tranches the night settled on.
        if self.capped("tranches"):
            return True
        log.phase(
            f"tranches — a model nests the tranches of {o.nest} campus(es) that do not add up"
        )
        self.tracker("tranches", "review", "--limit", o.nest)

        # --- enrich: last, and the largest lever ---------------------------------------
        # Every row sitting at T1 is held there by `fields_present` alone. Nothing above
        # this line can move one, because they are not wrong — they are empty. First
        # round(s) only: a second pass the same night finds the same pages, and a page
        # it has read is not read again, so it was paying to be told so. `--t2` picks
        # the rows that tier fails, fewest missing first.
        # The forward views first: campuses being built or permitted that lack the date
        # or the capacity the supply chart and the delay flags read (`--pipeline`).
        if number <= o.enrich_rounds and o.pipeline > 0:
            if self.capped("pipeline"):
                return True
            log.phase(
                f"enrich — {o.pipeline} campus(es) being built or permitted with no date "
                f"or capacity, {o.pipeline_budget} article budget"
            )
            self.tracker(
                "enrich", "--select", o.pipeline, "--pipeline", "--budget", o.pipeline_budget
            )
        if o.do_enrich and number <= o.enrich_rounds:
            if self.capped("enrich"):
                return True
            log.phase(f"enrich — {o.enrich} row(s) below T2, {o.enrich_budget} article budget")
            self.tracker(
                "enrich", "--select", o.enrich, "--t2", "--target", "0",
                "--budget", o.enrich_budget,
            )  # fmt: skip

        # Re-read, from the article cache, what an older prompt extracted. The gate has
        # been tightened release after release and each tightening applied only to rows
        # written afterwards: on 2026-10-10, 2,320 citations were last read by a
        # superseded prompt and 374 rows failed `vintage_current` for it. Cached only,
        # so nothing is fetched and a closed publisher's page we already hold is read
        # again legitimately; existing projects only, so a re-read never adds a campus.
        # Oldest vintage first. About ¥0.02 an article.
        if number == 1 and o.reread > 0:
            if self.capped("reread"):
                return True
            log.phase(f"reread — {o.reread} article(s) last read by a superseded prompt")
            self.tracker(
                "ingest", "reread", "--cached-only", "--existing-only", "--limit", o.reread
            )

        # Re-read what the gate refused before it last changed. v3.7.5 (2026-10-17)
        # began accepting short exact quotes, quotes with one slip, hyphenated figures,
        # pre-2020 years and coarse dates; 1,293 cached, readable articles held a
        # refusal it may reverse. 60 a night, about ¥1.2, so the backlog clears in
        # about 22 nights and then the step finds nothing and costs nothing.
        if number == 1 and o.recheck > 0:
            if self.capped("recheck"):
                return True
            log.phase(
                f"recheck — {o.recheck} article(s) holding a refusal from before the "
                "gate's last revision"
            )
            self.tracker(
                "ingest", "recheck", "--cached-only", "--existing-only", "--limit", o.recheck
            )

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

        if after == Counts(0, 0, 0):
            log.say(SETTLED)
            return True
        if self.stale >= o.dry_rounds:
            log.say(
                f"{o.dry_rounds} rounds with no reduction — this is the {FIXED_POINT}, stopping"
            )
            return True
        return False

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


# --- entry point ------------------------------------------------------------------------


def _local_now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def build(o: Options, stream: BinaryIO) -> Night:
    """The real night: phases as processes, counts and snapshots on the configured DB."""
    from tracker.config import get_settings

    db = get_settings().resolve_db()
    log = Log(stream, _local_now)
    named = os.environ.get("TRACKER_SPEND_LEDGER")
    # The shell names tonight's ledger; one run by hand gets its own beside the log.
    ledger = Path(named) if named else o.repo / "data" / "runs" / ledger_name(_local_now())
    return Night(
        options=o,
        log=log,
        runner=process_runner(log),
        counts=lambda: count(db),
        spent=lambda: ledger_spend(ledger),
        snapshot=lambda: snapshot_db(db, o.repo, _local_now()),
        clock=_local_now,
        ledger=ledger,
        pid=int(os.environ.get("OVERNIGHT_PID") or os.getpid()),
    )


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
    return build(options, sys.stdout.buffer).main()


if __name__ == "__main__":
    raise SystemExit(main())
