"""Rules that apply to a place, not to one campus: read them, keep them, match them.

Every fact in this database belongs to a campus an article named, and the reader
drops an article that names none. A rule that applies to a whole state or town names
none — so in the week after the 2026-11-03 elections 72 articles about the vote were
filed "no project", and the Texas governor-elect's permit moratorium reached 21 of
205 Texas projects: only the ones a stock story happened to name. A **condition** is
that rule, read once, quoted, and matched by code to every project in its place and
stage.

**Where the articles come from.** Every page the news step reads is saved to the
article cache, including the ones it found no campus in. :func:`pages` walks the
pages read since a date, keeps those a free word test says may state a rule
(:func:`worth_reading`), and :func:`read` puts each to a model once per prompt
(:data:`STAMP`, `condition_read`). The article reader's own prompt is not touched: it
is versioned and measured against the answer key, and a second job inside it would
move both.

**What is believed.** The model offers rules with a sentence for each thing it says —
the rule, where, its status, its size threshold. :func:`gate` keeps a rule only when
each sentence is the article's own (the extraction path's matcher), the place it is
filed under is named in them, its status is worded in them, and any threshold's
number is in them. A rule that fails is counted and dropped, never stored half-true.

**Two readings of one rule are one condition.** The model is shown the rules already
held for the states the article names and says which one it is reading
(`same_as`); otherwise a rule with the same place, need and kind is the same
(:func:`match_key`). Status, summary and dates come from the newest article
(:func:`settle`), so a pledge that becomes an order, and an order a court blocks,
read as one history.

**Which projects it reaches is code, never the model** (:func:`reach`): the place
(state, county, town, or grid region), the stage the rule covers, and its size
threshold. Every match says why, and says `likely` where the place is inferred. Two
limits are the data's, not the code's, and the reasons say so:

* **No project has a site coordinate.** All 1,150 stored coordinates are town
  centres derived from the city name (`tracker.ingest.geo`), so whether a campus is
  inside a town's limits cannot be checked; a town's rule matches a project filed
  under that town as `likely`.
* **Grid regions are inferred from the state** (:data:`STATE_REGIONS`): only states
  whose load is wholly or mostly in one operator's region are listed, and their
  matches are `likely`. A split state (Illinois, Indiana, Michigan, Kentucky,
  Missouri…) matches no grid-region rule.

A rule for the whole country or a whole-industry input (tariffs, chips, a war's
supply effects) reaches no project — flagging every row says nothing. Those are the
market section (:func:`market`), and a campus hears of them when an article about
that campus does.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tracker.models import (
    Condition,
    ConditionRead,
    ConditionSource,
    Event,
    Exposure,
    IngestUrl,
    Project,
    utcnow,
)
from tracker.normalize import norm_state, state_name
from tracker.vocab import (
    CONDITION_APPLIES,
    CONDITION_SCOPES,
    CONDITION_STATUSES,
    RISK_CATEGORIES,
)

if TYPE_CHECKING:
    from tracker.llm import Extractor

log = logging.getLogger(__name__)

#: Stamped on every page read. Change it when the prompt or the gate changes, and
#: every page is read again under the new one (`--again` does it for one run).
STAMP: Final = "conditions-1"

#: What a rule is. `other` is kept rather than refused: the need it hits and the
#: place matter more than the label.
KINDS: Final[tuple[str, ...]] = (
    "moratorium",
    "ban",
    "permit_pause",
    "zoning",
    "law",
    "executive_order",
    "ballot_measure",
    "tax",
    "tariff",
    "grid_rule",
    "pledge",
    "other",
)

#: Grid operators a grid-region rule may name.
REGIONS: Final[tuple[str, ...]] = ("ERCOT", "PJM", "MISO", "SPP", "CAISO", "NYISO", "ISO-NE")

_REGION_ALIASES: Final[dict[str, str]] = {
    "electric reliability council of texas": "ERCOT",
    "pjm interconnection": "PJM",
    "midcontinent independent system operator": "MISO",
    "midcontinent iso": "MISO",
    "southwest power pool": "SPP",
    "california iso": "CAISO",
    "california independent system operator": "CAISO",
    "new york iso": "NYISO",
    "new york independent system operator": "NYISO",
    "iso new england": "ISO-NE",
    "isone": "ISO-NE",
}

#: States whose load is all or nearly all in one grid operator's region. A project's
#: region is inferred from its state, so every grid-region match is `likely`, and a
#: state split between operators — Illinois, Indiana, Michigan, Kentucky, Missouri,
#: Mississippi, the Dakotas, North Carolina — is left out rather than guessed. Texas
#: outside ERCOT (El Paso, the Panhandle, the north-east corner) and California outside
#: CAISO (LADWP, SMUD, IID) are the known exceptions inside listed states.
STATE_REGIONS: Final[dict[str, str]] = {
    "TX": "ERCOT",
    "CA": "CAISO",
    "NY": "NYISO",
    **dict.fromkeys(("CT", "MA", "ME", "NH", "RI", "VT"), "ISO-NE"),
    **dict.fromkeys(("OH", "PA", "NJ", "MD", "DE", "DC", "WV", "VA"), "PJM"),
    **dict.fromkeys(("MN", "WI", "IA", "LA", "AR"), "MISO"),
    **dict.fromkeys(("KS", "OK", "NE"), "SPP"),
}

#: Statuses under which a rule reaches projects. Lifted and rejected rules obstruct
#: nothing; a blocked one still might, once the court is done.
EXPOSING: Final[frozenset[str]] = frozenset(
    {"proposed", "pledged", "passed", "in_force", "blocked"}
)

#: Scopes that reach projects. The others are market context.
PLACED: Final[frozenset[str]] = frozenset({"state", "county", "place", "grid_region"})

#: Phases each `applies_to` covers. "new" also needs no approved permit on record.
_STAGES: Final[dict[str, tuple[str, ...]]] = {
    "new": ("announced", "permitting"),
    "unbuilt": ("announced", "permitting", "construction", "paused"),
    "all": ("announced", "permitting", "construction", "operational", "paused"),
}

#: The article as the model sees it. A rule is stated near the top of a news story;
#: the tail is mostly links and boilerplate.
ARTICLE_CHARS: Final = 24_000

#: Pages read since this many days ago, when no date is given: the news step's
#: last two nights, so a night that did not run is caught up.
DEFAULT_DAYS: Final = 2

# --- which pages are worth a call ---------------------------------------------

#: Words a rule is stated in. Three of them, a word for a jurisdiction, and data
#: centers in the same article: measured on the saved pages of 2026-10-27 to 11-09,
#: about 150 of ~700 a day pass (≈40 that named no campus, ≈110 that named one).
_ACTION: Final = re.compile(
    r"\b(moratori\w+|ban(?:s|ned|ning)?|pause[ds]?|paus(?:ing)|freez\w+|executive order|"
    r"ordinance|charter amendment|referend\w+|ballot|bill|legislat\w+|governor(?:-elect)?|"
    r"tariffs?|export controls?|interconnection (?:rule|tariff|reform)|large[- ]load)\b",
    re.I,
)
_JURISDICTION: Final = re.compile(
    r"\b(statewide|state law|legislature|county|township|village|city council|"
    r"commission|governor|voters|ERCOT|PJM|MISO)\b",
    re.I,
)
_DATA_CENTER: Final = re.compile(r"data[- ]?cent(?:er|re)s?", re.I)
MIN_ACTION_WORDS: Final = 3


def worth_reading(text: str) -> bool:
    """Whether a page may state a rule about data centers. Free; decides the calls."""
    head = text[:60_000]
    return (
        bool(_DATA_CENTER.search(head))
        and bool(_JURISDICTION.search(head))
        and len(_ACTION.findall(head)) >= MIN_ACTION_WORDS
    )


@dataclass(frozen=True)
class Page:
    """One saved page the reader may read."""

    url: str
    title: str
    published_at: dt.datetime | None
    text: str


def _cached(url: str, cache_dir: Path) -> str:
    from tracker.ingest.fetch import cache_path

    path = cache_path(url, cache_dir)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:  # pragma: no cover - unreadable cache file
        return ""


def pages(
    session: Session,
    *,
    since: dt.datetime,
    cache_dir: Path,
    again: bool = False,
    url: str | None = None,
) -> tuple[list[Page], int]:
    """Saved pages read since `since` that may state a rule, newest first.

    Returns the pages and how many were looked at. A page already read under
    :data:`STAMP` is passed over unless `again`. Only pages the news step settled —
    found a campus or found none — are candidates: the others were never read.
    """
    query = select(IngestUrl.url, IngestUrl.title, IngestUrl.published_at).where(
        IngestUrl.status.in_(("ok", "no_project"))
    )
    if url:
        query = query.where(IngestUrl.url == url)
    else:
        query = query.where(IngestUrl.last_tried_at >= since)
    rows = session.execute(
        query.order_by(IngestUrl.last_tried_at.desc(), IngestUrl.id.desc())
    ).all()
    done = (
        set()
        if again
        else set(session.scalars(select(ConditionRead.url).where(ConditionRead.stamp == STAMP)))
    )
    out: list[Page] = []
    looked = 0
    for page_url, title, published in rows:
        if page_url in done:
            continue
        looked += 1
        text = _cached(page_url, cache_dir)
        if text and (url or worth_reading(text)):
            out.append(Page(page_url, title or "", published, text))
    return out, looked


# --- the question -------------------------------------------------------------

SYSTEM: Final = (
    """You read news articles for a tracker of US data-center campuses. Find RULES that
apply to a place or to the whole industry, not to one named campus: moratoriums,
bans, permit pauses, zoning or charter amendments, state laws and bills, executive
orders, ballot measures and their results, tax changes, tariffs, grid-operator rules
for large loads, and pledges by officials or candidates to do any of these.

Report only rules that cover data centers in the United States. Do NOT report a
single campus's own permit, rezoning, lawsuit or deal, opinion, polls, or general
backlash with no specific rule.

For each rule return:
- title: a short name with the place, e.g. "Piqua, OH ban on data centers over 25 MW"
- kind: one of """
    + ", ".join(KINDS)
    + """
- channel: the need it hits, one of """
    + ", ".join(RISK_CATEGORIES)
    + """
- scope: state, county, place (a city, town, village or township), grid_region,
  national, or input (one input to every campus, e.g. large power transformers)
- state: two-letter code, for a state, county or place rule
- county: the county's name, for a county rule
- place: the town's name as the article writes it, for a place rule
- region: one of """
    + ", ".join(REGIONS)
    + """, for a grid_region rule
- input: what is affected, for an input rule
- applies_to: "new" (projects not yet permitted), "unbuilt" (not yet running), or "all"
- min_mw: the size threshold in megawatts, if the rule has one, else null
- status: one of """
    + ", ".join(CONDITION_STATUSES)
    + """
  (pledged = promised by an official or candidate; passed = adopted but not yet in
  effect; rejected = voted down or turned down)
- effective_on, ends_on: YYYY-MM-DD if the article states them, else null
- summary: one plain sentence
- quotes: {"rule": ..., "place": ..., "status": ..., "threshold": ...}, each ONE
  sentence copied word for word from the article: the sentence stating the rule,
  the one naming where it applies, the one saying where it stands, the one with the
  size threshold. Repeat a sentence when it says several of these. Omit threshold
  when there is none.
- same_as: the number of a known rule below if this is the same rule, else null

Answer with JSON only: {"rules": [ ... ]}. {"rules": []} when the article states no
such rule."""
)


@dataclass(frozen=True)
class Known:
    """A rule already held, as the model is shown it."""

    id: int
    line: str


def states_named(text: str) -> set[str]:
    """Codes of the US states an article names by name."""
    from tracker.normalize import _STATE_NAMES

    found: set[str] = set()
    for name, code in _STATE_NAMES.items():
        if len(code) == 2 and re.search(rf"\b{re.escape(name.title())}\b", text):
            found.add(code)
    return found


def known_rules(session: Session, text: str, *, limit: int = 40) -> list[Known]:
    """The rules held for the states the article names, and every unplaced one."""
    states = states_named(text[:ARTICLE_CHARS])
    query = select(Condition).where(
        (Condition.state.in_(states)) | (Condition.scope.in_(("national", "input", "grid_region")))
    )
    rows = session.scalars(query.order_by(Condition.last_reported.desc(), Condition.id.desc()))
    out = []
    for c in list(rows)[:limit]:
        where = c.state or c.region or c.scope
        out.append(Known(c.id, f"#{c.id} [{where}, {c.scope}, {c.channel}, {c.status}] {c.title}"))
    return out


def question(page: Page, known: Sequence[Known]) -> str:
    """The user message: the rules held, then the article."""
    held = "\n".join(k.line for k in known) or "(none)"
    when = page.published_at.date().isoformat() if page.published_at else "unknown"
    return (
        f"Known rules (answer same_as with the number when the article describes one):\n"
        f"{held}\n\nArticle: {page.title}\nPublished: {when}\nURL: {page.url}\n\n"
        f"{page.text[:ARTICLE_CHARS]}"
    )


# --- what is believed ---------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One rule as offered, normalised; :func:`gate` decides whether it is kept."""

    title: str
    kind: str
    channel: str
    scope: str
    state: str | None
    county: str | None
    place: str | None
    region: str | None
    input: str | None
    applies_to: str
    min_mw: float | None
    status: str
    effective_on: dt.date | None
    ends_on: dt.date | None
    summary: str
    quotes: dict[str, str]
    same_as: int | None


def _text(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _region(value: Any) -> str | None:
    raw = _text(value, 80)
    if not raw:
        return None
    upper = raw.upper().replace(" ", "-")
    if upper in REGIONS:
        return upper
    folded = re.sub(r"[^a-z ]", "", raw.lower()).strip()
    return _REGION_ALIASES.get(folded) or _REGION_ALIASES.get(folded.replace(" ", ""))


def offered(payload: Any) -> tuple[list[Rule], list[tuple[str, str]]]:
    """The rules a reply offers, and (title, why) for each one too malformed to keep."""
    raw = payload.get("rules") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return [], [("(reply)", "no 'rules' list")]
    rules: list[Rule] = []
    bad: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"), 140) or "(untitled)"
        scope = _text(item.get("scope"), 20).lower()
        status = _text(item.get("status"), 20).lower().replace(" ", "_")
        channel = _text(item.get("channel"), 30).lower()
        if scope not in CONDITION_SCOPES:
            bad.append((title, f"scope {scope!r} is not one of {', '.join(CONDITION_SCOPES)}"))
            continue
        if status not in CONDITION_STATUSES:
            bad.append((title, f"status {status!r} is not one of {', '.join(CONDITION_STATUSES)}"))
            continue
        if channel not in RISK_CATEGORIES:
            bad.append((title, f"need {channel!r} is not an obstacle kind"))
            continue
        state = norm_state(item.get("state")) if item.get("state") else None
        county = _text(item.get("county"), 80) or None
        place = _text(item.get("place"), 80) or None
        region = _region(item.get("region"))
        what = _text(item.get("input"), 80) or None
        missing = {
            "state": scope in ("state", "county", "place") and not state,
            "county": scope == "county" and not county,
            "place": scope == "place" and not place,
            "region": scope == "grid_region" and not region,
            "input": scope == "input" and not what,
        }
        lacking = [name for name, gone in missing.items() if gone]
        if lacking:
            bad.append((title, f"a {scope} rule with no {', '.join(lacking)}"))
            continue
        try:
            min_mw = float(item["min_mw"]) if item.get("min_mw") not in (None, "") else None
        except (TypeError, ValueError):
            min_mw = None
        if min_mw is not None and min_mw <= 0:
            min_mw = None
        quotes_raw = item.get("quotes") if isinstance(item.get("quotes"), dict) else {}
        quotes = {
            k: _text(v, 1000)
            for k, v in quotes_raw.items()
            if k in ("rule", "place", "status", "threshold") and _text(v, 1000)
        }
        try:
            same_as = int(item["same_as"]) if item.get("same_as") not in (None, "") else None
        except (TypeError, ValueError):
            same_as = None
        applies = _text(item.get("applies_to"), 10).lower()
        kind = _text(item.get("kind"), 30).lower().replace(" ", "_")
        rules.append(
            Rule(
                title=title,
                kind=kind if kind in KINDS else "other",
                channel=channel,
                scope=scope,
                state=state if scope in ("state", "county", "place") else None,
                county=county if scope == "county" else None,
                place=place if scope == "place" else None,
                region=region if scope == "grid_region" else None,
                input=what if scope == "input" else None,
                applies_to=applies if applies in CONDITION_APPLIES else "unbuilt",
                min_mw=min_mw,
                status=status,
                effective_on=_date(item.get("effective_on")),
                ends_on=_date(item.get("ends_on")),
                summary=_text(item.get("summary"), 300) or title,
                quotes=quotes,
                same_as=same_as,
            )
        )
    return rules, bad


#: Words a sentence must contain to bear out each status. Loose on purpose — a news
#: sentence says "voters approved", "the order took effect", "she vowed" — but a
#: sentence about something else entirely does not pass.
_STATUS_WORDS: Final[dict[str, re.Pattern[str]]] = {
    status: re.compile(pattern, re.I)
    for status, pattern in {
        "proposed": r"propos|introduc|petition|ballot|referend|consider|draft|seek|would|"
        r"plan|calls? for|called for|push|question|vote on|weigh",
        "pledged": r"pledg|promis|vow|campaign|wants?|would|will|plan|calls? for|called for|"
        r"support|seek|intend|commit",
        "passed": r"pass|approv|adopt|enact|sign|voted|ratif|amend",
        "in_force": r"in (?:effect|force|place)|took effect|takes effect|effective|order|paus|"
        r"halt|froze|freez|ban|moratori|enact|approv|pass|imposed|bars?|prohibit",
        "blocked": r"court|judge|block|enjoin|injunction|struck|overturn|invalid",
        "lifted": r"lift|repeal|rescind|expir|ended|ends|reversed|withdr|revok",
        "rejected": r"reject|defeat|voted (?:down|against|no)|fail|turned down|denied|opposed",
    }.items()
}


def _place_words(rule: Rule) -> list[str]:
    """Spellings at least one of which a rule's sentences must contain."""
    if rule.scope == "state" and rule.state:
        return [state_name(rule.state) or rule.state]
    if rule.scope == "county" and rule.county:
        from tracker.ingest.geo import place_key

        return [place_key(rule.county) or rule.county]
    if rule.scope == "place" and rule.place:
        from tracker.ingest.geo import place_key

        return [place_key(rule.place) or rule.place]
    if rule.scope == "grid_region" and rule.region:
        aliases = [k for k, v in _REGION_ALIASES.items() if v == rule.region]
        return [rule.region, rule.region.replace("-", " "), *aliases]
    return []


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _names(words: Iterable[str], text: str) -> bool:
    """Whether the folded `text` contains one of `words` as whole words."""
    said = f" {_fold(text)} "
    return any(_fold(w) and f" {_fold(w)} " in said for w in words)


def _states_number(value: float, text: str) -> bool:
    """Whether `text` writes `value` as a number of its own — not inside "2025"."""
    whole = int(value) if float(value).is_integer() else None
    spellings = {f"{value:g}", *([f"{whole:,}", str(whole)] if whole is not None else [])}
    return any(re.search(rf"(?<![\d.,]){re.escape(s)}(?![\d]|[.,]\d)", text) for s in spellings)


def gate(rule: Rule, article: str, *, title: str = "") -> tuple[dict[str, str], str]:
    """`(verified sentences, refusal)`: exactly one is empty.

    The article's own sentence is kept for each quote, never the model's text, by
    the matcher the extraction path's evidence gate uses. The place a rule is filed
    under must be named in those sentences or the headline: "she wants a halt to new
    projects" says nothing about which state.
    """
    from tracker.ingest.crawl import _verbatim_run

    kept: dict[str, str] = {}
    for what, quote in rule.quotes.items():
        run = _verbatim_run(quote, article)
        if run.text:
            kept[what] = run.text
        elif what in ("rule", "status"):
            return {}, f"the {what} sentence is not in the article"
    if "rule" not in kept:
        return {}, "no sentence states the rule"
    status_line = kept.get("status") or kept["rule"]
    kept.setdefault("status", status_line)
    if not _STATUS_WORDS[rule.status].search(status_line):
        return {}, f"the status sentence does not say it is {rule.status.replace('_', ' ')}"
    sentences = " ".join(kept.values())
    words = _place_words(rule)
    if words and not _names(words, f"{sentences} {title}"):
        return {}, f"neither the sentences nor the headline name {words[0]}"
    if rule.min_mw is not None and not _states_number(rule.min_mw, sentences):
        return {}, f"no sentence states the {rule.min_mw:g} MW threshold"
    return kept, ""


# --- one rule, however many articles ------------------------------------------


def _town_key(name: str | None) -> str:
    from tracker.ingest.geo import place_key

    return place_key(name)


def match_key(
    scope: str,
    channel: str,
    *,
    state: str | None = None,
    county: str | None = None,
    place: str | None = None,
    region: str | None = None,
    input: str | None = None,
) -> str:
    """What two readings of one rule share: its scope, place and need."""
    where = {
        "state": state or "",
        "county": f"{state or ''}:{_town_key(county)}",
        "place": f"{state or ''}:{_town_key(place)}",
        "grid_region": region or "",
        "national": "US",
        "input": _fold(input or ""),
    }[scope]
    return f"{scope}|{where}|{channel}"


def _rule_key(rule: Rule) -> str:
    return match_key(
        rule.scope,
        rule.channel,
        state=rule.state,
        county=rule.county,
        place=rule.place,
        region=rule.region,
        input=rule.input,
    )


def _same_place(condition: Condition, rule: Rule) -> bool:
    return condition.match_key.split("|")[:2] == _rule_key(rule).split("|")[:2]


def _when(source: ConditionSource) -> dt.datetime:
    return source.published_at or source.read_at


def settle(condition: Condition) -> None:
    """Status, summary and dates from the condition's articles, newest first."""
    sources = sorted(condition.sources, key=lambda s: (_when(s), s.id or 0))
    if not sources:
        return
    newest = sources[-1]
    condition.status = newest.status
    condition.summary = newest.summary
    condition.first_reported = _when(sources[0]).date()
    condition.last_reported = _when(newest).date()


def attach(
    session: Session, rule: Rule, quotes: dict[str, str], page: Page, *, now: dt.datetime
) -> tuple[Condition, bool]:
    """File one verified rule under its condition, creating it if new. Returns (it, new)."""
    key = _rule_key(rule)
    condition = None
    if rule.same_as is not None:
        candidate = session.get(Condition, rule.same_as)
        if candidate is not None and _same_place(candidate, rule):
            condition = candidate
    if condition is None:
        condition = session.scalars(
            select(Condition)
            .where(Condition.match_key == key, Condition.kind == rule.kind)
            .order_by(Condition.id)
        ).first()
    new = condition is None
    if condition is None:
        condition = Condition(
            title=rule.title,
            kind=rule.kind,
            channel=rule.channel,
            scope=rule.scope,
            state=rule.state,
            county=rule.county,
            place=rule.place,
            region=rule.region,
            input=rule.input,
            applies_to=rule.applies_to,
            min_mw=rule.min_mw,
            status=rule.status,
            summary=rule.summary,
            effective_on=rule.effective_on,
            ends_on=rule.ends_on,
            match_key=key,
            created_at=now,
            updated_at=now,
        )
        session.add(condition)
        session.flush()
    source = session.scalars(
        select(ConditionSource).where(
            ConditionSource.condition_id == condition.id, ConditionSource.url == page.url
        )
    ).first()
    if source is None:
        source = ConditionSource(condition_id=condition.id, url=page.url)
        condition.sources.append(source)
    source.title = page.title or None
    source.published_at = page.published_at
    source.read_at = now
    source.status = rule.status
    source.summary = rule.summary
    source.quotes = json.dumps(quotes, ensure_ascii=False, sort_keys=True)
    session.flush()
    newest = max(condition.sources, key=lambda s: (_when(s), s.id or 0))
    if newest is source and not new:
        # The newest article also says what the rule now is: a pledge that became an
        # order, an order narrowed to new projects, a date set.
        condition.title, condition.kind = rule.title, rule.kind
        condition.applies_to, condition.min_mw = rule.applies_to, rule.min_mw
        condition.effective_on = rule.effective_on or condition.effective_on
        condition.ends_on = rule.ends_on or condition.ends_on
    settle(condition)
    condition.updated_at = now
    return condition, new


# --- reading ------------------------------------------------------------------


@dataclass
class Read:
    """What reading one page came to."""

    url: str
    title: str
    #: (condition id, title, status, new) for each rule kept.
    kept: list[tuple[int, str, str, bool]] = field(default_factory=list)
    #: (rule title, why) for each rule offered and refused.
    refused: list[tuple[str, str]] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class _Asked:
    page: Page
    user: str


def read(
    session: Session,
    items: Sequence[Page],
    *,
    extractor: Extractor,
    limit: int | None = None,
    workers: int = 1,
    commit: bool = True,
    now: dt.datetime | None = None,
    on_each: Callable[[Read], None] | None = None,
) -> Any:
    """Put each page to the model once, keep the rules that pass :func:`gate`.

    Through `tracker.judged.run`, so the night's money ceiling is asked before each
    wave and every page's answer is committed as it lands.
    """
    from tracker import judged
    from tracker.llm import LLMJsonError, parse_json_object

    now = now or utcnow()

    def prepare(page: Page) -> _Asked:
        return _Asked(page, question(page, known_rules(session, page.text)))

    def ask(asked: _Asked, _db: Session | None) -> Any:
        return extractor.complete(system=SYSTEM, user=asked.user, max_tokens=4096)

    def apply(page: Page, _asked: _Asked, reply: Any) -> judged.Done:
        result = Read(page.url, page.title)
        try:
            rules, bad = offered(parse_json_object(reply.text))
        except (LLMJsonError, ValueError) as exc:
            rules, bad = [], []
            result.error = f"unusable reply: {exc}"
        result.refused.extend(bad)
        for rule in rules:
            quotes, refusal = gate(rule, page.text, title=page.title)
            if refusal:
                result.refused.append((rule.title, refusal))
                continue
            if not commit:
                # A dry run reads through a read-only engine: say what would be kept.
                result.kept.append((rule.same_as or 0, rule.title, rule.status, True))
                continue
            condition, new = attach(session, rule, quotes, page, now=now)
            result.kept.append((condition.id, condition.title, condition.status, new))
        if not commit:
            return judged.Done(result)
        mark = session.get(ConditionRead, page.url) or ConditionRead(url=page.url)
        mark.stamp, mark.read_at = STAMP, now
        mark.found, mark.refused = len(result.kept), len(result.refused)
        mark.error = result.error or None
        session.add(mark)
        session.flush()
        return judged.Done(result)

    def failed(page: Page, error: BaseException) -> Read:
        return Read(page.url, page.title, error=str(error)[:300])

    return judged.run(
        session,
        items,
        kind=None,
        subject=lambda page: page.url,
        fingerprint=lambda page: STAMP,
        projects=lambda _page: (),
        prepare=prepare,
        ask=ask,
        apply=apply,
        failed=failed,
        limit=limit,
        workers=workers,
        commit=commit,
        stage="conditions",
        on_each=on_each,
        label="conditions",
    )


# --- which projects a rule reaches --------------------------------------------


@dataclass(frozen=True)
class Site:
    """What matching needs of a project."""

    id: int
    state: str
    county: str | None
    city: str | None
    phase: str
    mw: float | None
    permitted: bool


def sites(session: Session) -> list[Site]:
    """Every project, as matching sees it."""
    permitted = set(
        session.scalars(select(Event.project_id).where(Event.event_type == "permit_approved"))
    )
    rows = session.execute(
        select(
            Project.id,
            Project.state,
            Project.county,
            Project.city,
            Project.phase,
            Project.mw_planned,
        )
    ).all()
    return [
        Site(pid, state, county, city, phase, mw, pid in permitted)
        for pid, state, county, city, phase, mw in rows
    ]


def region_of(state: str) -> str | None:
    """The grid operator most of a state's load is in, or None for a split state."""
    return STATE_REGIONS.get(state)


def reach(condition: Condition, site: Site) -> tuple[str, str] | None:
    """`(fit, why)` when `condition` reaches `site`, else None. Code only."""
    if condition.status not in EXPOSING or condition.scope not in PLACED:
        return None
    if site.phase not in _STAGES[condition.applies_to]:
        return None
    if condition.applies_to == "new" and site.permitted:
        return None
    fit = "exact"
    where: str
    if condition.scope == "state":
        if site.state != condition.state:
            return None
        where = site.state
    elif condition.scope == "county":
        if site.state != condition.state or not site.county:
            return None
        if _town_key(site.county) != _town_key(condition.county):
            return None
        where = f"{site.county} County, {site.state}"
    elif condition.scope == "place":
        if site.state != condition.state or not site.city:
            return None
        if _town_key(site.city) != _town_key(condition.place):
            return None
        # No campus has a site coordinate, so being inside the town's limits is
        # inferred from the town it is filed under.
        fit = "likely"
        where = f"filed under {site.city}, {site.state}; town limits not checked"
    else:
        region = region_of(site.state)
        if region is None or region != condition.region:
            return None
        fit = "likely"
        where = f"{site.state} is mostly {region}"
    stage = site.phase + (" · no permit on record" if condition.applies_to == "new" else "")
    size = ""
    if condition.min_mw is not None:
        if site.mw is None:
            fit = "likely"
            size = f" · size unknown (rule is {condition.min_mw:g} MW and up)"
        elif site.mw < condition.min_mw:
            return None
        else:
            size = f" · {site.mw:g} MW"
    return fit, f"{where} · {stage}{size}"


@dataclass
class Matched:
    """What one rebuild of the exposures changed."""

    added: int = 0
    kept: int = 0
    dropped: int = 0
    #: condition id -> projects it reaches now.
    reach: dict[int, int] = field(default_factory=dict)


def rebuild(session: Session, *, now: dt.datetime | None = None, write: bool = True) -> Matched:
    """Recompute every exposure from the conditions and the projects as they are now.

    Wholesale, like `project.blocks`: an exposure is a function of the two, never an
    edit. `first_seen` is kept for a pair still matched, so "newly affected" holds.
    Without `write`, only counts what would change.
    """
    now = now or utcnow()
    out = Matched()
    everyone = sites(session)
    by_state: dict[str, list[Site]] = {}
    for site in everyone:
        by_state.setdefault(site.state, []).append(site)
    wanted: dict[tuple[int, int], tuple[str, str]] = {}
    for condition in session.scalars(select(Condition)):
        pool = (
            everyone
            if condition.scope == "grid_region"
            else by_state.get(condition.state or "", [])
        )
        for site in pool:
            got = reach(condition, site)
            if got is not None:
                wanted[(condition.id, site.id)] = got
                out.reach[condition.id] = out.reach.get(condition.id, 0) + 1
    held = {(e.condition_id, e.project_id): e for e in session.scalars(select(Exposure))}
    for key, exposure in held.items():
        if key not in wanted:
            out.dropped += 1
            if write:
                session.delete(exposure)
            continue
        out.kept += 1
        if write and (exposure.fit, exposure.why) != wanted[key]:
            exposure.fit, exposure.why = wanted[key]
    for key, (fit, why) in wanted.items():
        if key not in held:
            out.added += 1
            if write:
                session.add(
                    Exposure(
                        condition_id=key[0], project_id=key[1], fit=fit, why=why, first_seen=now
                    )
                )
    if write:
        session.flush()
    return out


# --- reading them back --------------------------------------------------------


def for_project(session: Session, project_id: int) -> list[tuple[Condition, Exposure]]:
    """The conditions a project is exposed to, newest report first."""
    rows = session.execute(
        select(Condition, Exposure)
        .join(Exposure, Exposure.condition_id == Condition.id)
        .where(Exposure.project_id == project_id)
        .order_by(Condition.last_reported.desc(), Condition.id.desc())
    ).all()
    return [(c, e) for c, e in rows]


def listing(
    session: Session, *, state: str | None = None, cleared: bool = False
) -> list[Condition]:
    """Conditions, newest report first; lifted and rejected ones only with `cleared`."""
    query = select(Condition)
    if state:
        query = query.where(Condition.state == state.upper())
    if not cleared:
        query = query.where(Condition.status.in_(sorted(EXPOSING)))
    return list(
        session.scalars(query.order_by(Condition.last_reported.desc(), Condition.id.desc()))
    )


def market(session: Session) -> list[Condition]:
    """Whole-country and whole-industry conditions still standing: context, not flags."""
    return list(
        session.scalars(
            select(Condition)
            .where(Condition.scope.in_(("national", "input")))
            .where(Condition.status.in_(sorted(EXPOSING)))
            .order_by(Condition.last_reported.desc(), Condition.id.desc())
        )
    )


def reach_counts(session: Session) -> dict[int, int]:
    """condition id -> projects it reaches."""
    from sqlalchemy import func

    return dict(
        session.execute(
            select(Exposure.condition_id, func.count()).group_by(Exposure.condition_id)
        ).all()
    )


def quotes_of(source: ConditionSource) -> dict[str, str]:
    try:
        loaded = json.loads(source.quotes or "{}")
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def drop(session: Session, condition_id: int) -> Condition | None:
    """Delete a condition read wrongly, with its articles and exposures."""
    condition = session.get(Condition, condition_id)
    if condition is None:
        return None
    session.execute(delete(Exposure).where(Exposure.condition_id == condition_id))
    session.delete(condition)
    session.flush()
    return condition


def as_json(condition: Condition, *, reach: int | None = None) -> dict[str, Any]:
    """One condition as the CLI's and the API's JSON."""
    return {
        "id": condition.id,
        "title": condition.title,
        "kind": condition.kind,
        "need": condition.channel,
        "scope": condition.scope,
        "state": condition.state,
        "county": condition.county,
        "place": condition.place,
        "region": condition.region,
        "input": condition.input,
        "applies_to": condition.applies_to,
        "min_mw": condition.min_mw,
        "status": condition.status,
        "summary": condition.summary,
        "effective_on": condition.effective_on.isoformat() if condition.effective_on else None,
        "ends_on": condition.ends_on.isoformat() if condition.ends_on else None,
        "first_reported": (
            condition.first_reported.isoformat() if condition.first_reported else None
        ),
        "last_reported": condition.last_reported.isoformat() if condition.last_reported else None,
        "projects": reach,
    }


def where_words(condition: Condition) -> str:
    """'Piqua, OH', 'Texas', 'ERCOT', 'large power transformers'."""
    if condition.scope == "state":
        return state_name(condition.state) or condition.state or ""
    if condition.scope == "county":
        return f"{condition.county} County, {condition.state}".replace(" County County", " County")
    if condition.scope == "place":
        return f"{condition.place}, {condition.state}"
    if condition.scope == "grid_region":
        return condition.region or ""
    if condition.scope == "input":
        return condition.input or "industry-wide"
    return "United States"


def since_default(now: dt.datetime | None = None) -> dt.datetime:
    return (now or utcnow()) - dt.timedelta(days=DEFAULT_DAYS)


def statuses_in(rows: Iterable[Condition]) -> dict[str, int]:
    from collections import Counter

    return dict(Counter(c.status for c in rows))


__all__ = [
    "ARTICLE_CHARS",
    "DEFAULT_DAYS",
    "EXPOSING",
    "KINDS",
    "PLACED",
    "REGIONS",
    "STAMP",
    "STATE_REGIONS",
    "SYSTEM",
    "Known",
    "Matched",
    "Page",
    "Read",
    "Rule",
    "Site",
    "as_json",
    "attach",
    "drop",
    "for_project",
    "gate",
    "known_rules",
    "listing",
    "market",
    "match_key",
    "offered",
    "pages",
    "question",
    "quotes_of",
    "reach",
    "reach_counts",
    "read",
    "rebuild",
    "region_of",
    "settle",
    "since_default",
    "sites",
    "states_named",
    "statuses_in",
    "where_words",
    "worth_reading",
]
