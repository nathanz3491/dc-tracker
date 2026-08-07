"""Does the database rest on evidence, and is that share moving the right way?

Every other test module here asks whether the code is correct. These ask whether
the *data* is getting better, which is a different question and needs a different
shape of test: a metric, a planted fault it must detect, and a repair it must
register as an improvement.

The distinction the whole module turns on is between two values that both lack a
sentence:

* **待确认** — no quote, and the row says so. The evidence gate working. Every
  reader and every rollup already discounts it.
* **confirmed, no quote** — no quote, and nothing says so. The row presents it as
  established. This is the defect, it is invisible from inside any single row,
  and on the live database there were 89 of them, every one produced by a prompt
  vintage that predates the column quotes live in.

A metric that cannot tell those apart would report the gate's successes as
failures, so `test_a_gate_flagged_value_is_not_a_defect` is the load-bearing test
in this file.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import select

from tracker import quality
from tracker.ingest import crawl
from tracker.ingest.fetch import FetchResult
from tracker.llm import LLMReply
from tracker.models import IngestUrl, Project, Source
from tracker.prompts import load_prompt
from tracker.upsert import claims_by_field

_URLS = iter(range(1, 10_000))


def _project(session, **kwargs) -> Project:
    defaults = {
        "name": "Campus",
        "company": "Someone",
        "state": "WI",
        "city": "Mount Pleasant",
        "dedup_key": f"k{next(_URLS)}",
        "phase": "construction",
        "confidence": 2,
    }
    defaults.update(kwargs)
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


def _source(
    session,
    project,
    claims: dict,
    *,
    quotes: dict | None = None,
    unconfirmed: str | None = None,
    source_type: str = "trade_press",
    extractor: str = "crawl:extract-v1@aaaaaaaa:model:httpx",
    url: str | None = None,
    fetched_at: dt.datetime | None = None,
    published_at: dt.datetime | None = None,
) -> Source:
    """One citation, built the way ingest builds one.

    Three columns decide what `quality` sees, and they have to agree the way the
    write path makes them agree: `claims` holds every value, `fields` lists only
    the ones a quote confirmed, and `quotes` holds the sentence per field. A
    legacy row — the shape of all 89 live defects — has a field in `fields` and
    nothing in `quotes`, because the column did not exist when it was written.
    """
    unconfirmed_set = {f.strip() for f in (unconfirmed or "").split(",") if f.strip()}
    row = Source(
        project_id=project.id,
        url=url or f"https://example.test/{next(_URLS)}",
        source_type=source_type,
        claims=json.dumps(claims),
        fields=",".join(k for k in claims if k not in unconfirmed_set),
        unconfirmed_fields=unconfirmed,
        quotes=json.dumps(quotes) if quotes else None,
        extractor=extractor,
        fetched_at=fetched_at or dt.datetime(2026, 1, 1),
        published_at=published_at,
    )
    session.add(row)
    session.flush()
    return row


def _bucket_of(session, project, field: str) -> str | None:
    """Which bucket one specific stored value landed in.

    Scoped to a single field on purpose. Asserting on the whole-database counts
    reads more directly but is brittle for a reason worth keeping: `phase` is NOT
    NULL, so every fixture row has one, and nothing in these fixtures claims it —
    so it correctly counts as unconfirmed and silently inflates every total.
    """
    for basis in quality.value_bases(session, project_ids=[project.id]):
        if basis.field == field:
            return basis.bucket
    return None


# --- the three tiers a stored value can sit at -------------------------------


def test_a_quoted_value_is_quote_backed(session):
    """The healthy case: the winning source recorded the sentence."""
    project = _project(session, mw_planned=350.0)
    _source(
        session,
        project,
        {"mw_planned": 350.0},
        quotes={"mw_planned": "the campus will draw 350 megawatts at full buildout"},
    )
    session.refresh(project)

    assert _bucket_of(session, project, "mw_planned") == quality.QUOTE_BACKED
    assert not quality.silent_defects(session)


def test_an_unquoted_confirmed_value_is_a_silent_defect(session):
    """The legacy shape: listed in `fields`, absent from `quotes`.

    This is what all 89 live defects look like — written before migration 0007
    added the column, so the gate never had anywhere to record a sentence and the
    value has read as established ever since.
    """
    project = _project(session, mw_planned=350.0)
    _source(session, project, {"mw_planned": 350.0})  # no quotes at all
    session.refresh(project)

    assert _bucket_of(session, project, "mw_planned") == quality.SILENT_DEFECT
    defects = quality.silent_defects(session)
    assert [(d.project_id, d.field) for d in defects] == [(project.id, "mw_planned")]


def test_a_gate_flagged_value_is_not_a_defect(session):
    """待确认 is the gate working, and must never be counted as a failure.

    The load-bearing test in this file. A metric that lumped these together would
    report 286 live values as defects, send somebody to fix the evidence gate, and
    hide the 89 rows that actually need re-reading.
    """
    project = _project(session, mw_planned=350.0)
    _source(session, project, {"mw_planned": 350.0}, unconfirmed="mw_planned")
    session.refresh(project)

    assert _bucket_of(session, project, "mw_planned") == quality.FLAGGED
    assert quality.evidence_census(session).defects == 0
    assert not quality.silent_defects(session)


# --- the metric has to register a repair as an improvement -------------------


def test_recording_the_missing_quote_removes_the_defect(session):
    """The improvement assertion, in miniature.

    This is the shape the remediation test takes: measure, repair, measure again,
    and require the number to have *fallen*. Without this, a harness could report
    a constant and nobody would notice.
    """
    project = _project(session, mw_planned=350.0)
    source = _source(session, project, {"mw_planned": 350.0})
    session.refresh(project)
    before = quality.evidence_census(session)
    assert before.defects == 1

    # Exactly what re-extraction under the current prompt produces: the same
    # value, now with the sentence the gate verified it against.
    source.quotes = json.dumps({"mw_planned": "a 350 MW campus in Mount Pleasant"})
    session.flush()
    session.refresh(project)

    after = quality.evidence_census(session)
    assert after.defects < before.defects
    assert after.defects == 0
    assert _bucket_of(session, project, "mw_planned") == quality.QUOTE_BACKED
    #: The repair must not smuggle in new values — the total is the control.
    assert after.total == before.total


def test_a_repair_that_loses_a_value_is_not_an_improvement(session):
    """The negative control for the test above.

    Dropping the claim entirely also drives `defects` to zero, and a harness that
    only watched that number would call it a success. The total is what catches
    it, so the improvement assertion always has to check both.
    """
    project = _project(session, mw_planned=350.0)
    source = _source(session, project, {"mw_planned": 350.0})
    session.refresh(project)
    before = quality.evidence_census(session)

    project.mw_planned = None
    source.claims = json.dumps({})
    source.fields = None
    session.flush()
    session.refresh(project)

    after = quality.evidence_census(session)
    assert after.defects == 0  # looks like a win
    assert after.total < before.total  # and is not one


# --- prompt vintage ----------------------------------------------------------


def test_a_defect_is_attributed_to_the_prompt_that_wrote_it(session):
    """Which vintage a defect came from is what separates remediation from a bug."""
    project = _project(session, mw_planned=350.0)
    _source(
        session,
        project,
        {"mw_planned": 350.0},
        extractor="crawl:extract-v1@8eb51f2a:MiniMax-M2.7-highspeed:httpx",
    )
    session.refresh(project)

    census = quality.evidence_census(session)
    assert census.defects_by_vintage == {"extract-v1@8eb51f2a": 1}


def test_a_lookup_is_not_given_an_invented_prompt_version(session):
    """`derived:census-place-2020` has no prompt behind it and must not get one."""
    assert quality.vintage("crawl:extract-v1@5d479a68:MiniMax-M2.7:httpx") == "extract-v1@5d479a68"
    assert quality.vintage("derived:census-place-2020") == "derived"
    assert quality.vintage(None) == "unknown"


# --- recency inversions ------------------------------------------------------


def _published(session, url: str, when: dt.datetime) -> None:
    session.add(IngestUrl(url=url, run_id="t", status="ok", published_at=when))
    session.flush()


def _by_publication(monkeypatch, on: bool = True) -> None:
    """Turn the publication-date tiebreak on for one test.

    `get_settings` is lru_cached and the autouse fixture has already primed it, so
    setting the variable alone would change nothing.
    """
    from tracker.config import get_settings

    monkeypatch.setenv("TRACKER_MERGE_BY_PUBLICATION_DATE", "1" if on else "0")
    get_settings.cache_clear()


def test_an_older_article_winning_a_tie_is_an_inversion(session):
    """Hyperion's case: $10B published August beat $27B published November.

    Both trade press, both quote-backed, so the tiebreak decided it — and the
    tiebreak is `fetched_at`, which records when the crawler visited.
    """
    project = _project(session, investment_usd=10_000_000_000)
    old = _source(
        session,
        project,
        {"investment_usd": 10_000_000_000},
        quotes={
            "investment_usd": "the buildout itself is expected to cost in the $10 billion range"
        },
        url="https://example.test/older",
        fetched_at=dt.datetime(2026, 8, 1, 21, 54),  # crawled LAST
    )
    new = _source(
        session,
        project,
        {"investment_usd": 27_000_000_000},
        quotes={"investment_usd": "Meta's $27 billion Hyperion campus"},
        url="https://example.test/newer",
        fetched_at=dt.datetime(2026, 7, 30, 21, 25),  # crawled FIRST
    )
    _published(session, old.url, dt.datetime(2025, 8, 22))
    _published(session, new.url, dt.datetime(2025, 11, 5))
    session.refresh(project)

    found = quality.recency_inversions(session)
    assert len(found) == 1
    assert found[0].kept == 10_000_000_000
    assert found[0].passed_over == 27_000_000_000


def test_the_newer_article_winning_is_not_reported(session):
    """The control: same setup, crawl order agreeing with publication order."""
    project = _project(session, investment_usd=27_000_000_000)
    newer = _source(
        session,
        project,
        {"investment_usd": 27_000_000_000},
        quotes={"investment_usd": "Meta's $27 billion Hyperion campus"},
        url="https://example.test/newer",
        fetched_at=dt.datetime(2026, 8, 1),
    )
    older = _source(
        session,
        project,
        {"investment_usd": 10_000_000_000},
        quotes={"investment_usd": "in the $10 billion range"},
        url="https://example.test/older",
        fetched_at=dt.datetime(2026, 7, 30),
    )
    _published(session, newer.url, dt.datetime(2025, 11, 5))
    _published(session, older.url, dt.datetime(2025, 8, 22))
    session.refresh(project)

    assert quality.recency_inversions(session) == []


def test_a_stronger_source_winning_is_not_an_inversion(session):
    """Weight beating recency is the policy, not a tiebreak accident.

    Deliberately excluded: reporting it would turn a measurement of *arbitrary*
    outcomes into a complaint about the merge policy, and the two need separate
    arguments.
    """
    project = _project(session, investment_usd=10_000_000_000)
    filing = _source(
        session,
        project,
        {"investment_usd": 10_000_000_000},
        quotes={"investment_usd": "in the $10 billion range"},
        source_type="company_filing",  # weight 3
        url="https://example.test/filing",
    )
    press = _source(
        session,
        project,
        {"investment_usd": 27_000_000_000},
        quotes={"investment_usd": "a $27 billion campus"},
        source_type="general_media",  # weight 1
        url="https://example.test/press",
    )
    _published(session, filing.url, dt.datetime(2025, 8, 22))
    _published(session, press.url, dt.datetime(2025, 11, 5))
    session.refresh(project)

    assert quality.recency_inversions(session) == []


# --- the publication-date tiebreak (migration 0014, off by default) ----------


def _hyperion(session):
    """The live case: $10B crawled last, $27B published later, both quote-backed."""
    project = _project(session, investment_usd=10_000_000_000)
    _source(
        session,
        project,
        {"investment_usd": 10_000_000_000},
        quotes={"investment_usd": "expected to cost in the $10 billion range"},
        url="https://example.test/aug",
        fetched_at=dt.datetime(2026, 8, 1, 21, 54),
        published_at=dt.datetime(2025, 8, 22),
    )
    _source(
        session,
        project,
        {"investment_usd": 27_000_000_000},
        quotes={"investment_usd": "Meta's $27 billion Hyperion campus"},
        url="https://example.test/nov",
        fetched_at=dt.datetime(2026, 7, 30, 21, 25),
        published_at=dt.datetime(2025, 11, 5),
    )
    session.refresh(project)
    return project


def test_by_default_crawl_order_still_decides(session):
    """The flag is off, so nothing moves until somebody turns it on deliberately.

    Asserted rather than assumed: a staged change that quietly took effect would
    move stored values across the database on the next recompute.
    """
    project = _hyperion(session)
    claims = claims_by_field(list(project.sources))["investment_usd"]

    assert claims[0].value == 10_000_000_000
    assert len(quality.recency_inversions(session)) == 1


def test_the_flag_makes_publication_order_decide(session, monkeypatch):
    """With it on, Hyperion stops holding the figure its own notes call superseded."""
    project = _hyperion(session)
    _by_publication(monkeypatch)

    claims = claims_by_field(list(project.sources))["investment_usd"]
    assert claims[0].value == 27_000_000_000
    assert quality.recency_inversions(session) == []


def test_a_source_with_no_publication_date_falls_back_to_the_crawl_time(session, monkeypatch):
    """Most of the corpus has a date; a hand-supplied URL never will.

    Sorting an unknown date to the beginning of time would let any dated claim
    beat every undated one regardless of merit, which is a bigger change than the
    one being made.
    """
    project = _project(session, investment_usd=5_000_000_000)
    _source(
        session,
        project,
        {"investment_usd": 5_000_000_000},
        quotes={"investment_usd": "a $5 billion campus"},
        url="https://example.test/undated-recent",
        fetched_at=dt.datetime(2026, 8, 1),
    )
    _source(
        session,
        project,
        {"investment_usd": 1_000_000_000},
        quotes={"investment_usd": "a $1 billion first phase"},
        url="https://example.test/undated-old",
        fetched_at=dt.datetime(2026, 1, 1),
    )
    session.refresh(project)
    _by_publication(monkeypatch)

    claims = claims_by_field(list(project.sources))["investment_usd"]
    assert claims[0].value == 5_000_000_000, "undated claims still rank by fetched_at"


def test_the_publication_date_is_carried_from_the_queue_on_write(session):
    """`discover` records it on `ingest_url`; the merge needs it on `source`."""
    url = "https://example.test/from-a-feed"
    _published(session, url, dt.datetime(2025, 11, 5))
    project = _project(session, investment_usd=27_000_000_000)
    row = _source(
        session,
        project,
        {"investment_usd": 27_000_000_000},
        quotes={"investment_usd": "a $27 billion campus"},
        url=url,
    )
    assert row.published_at is None, "the fixture writes the row directly, bypassing upsert"

    from tracker.upsert import _published_at

    assert _published_at(session, url) == dt.datetime(2025, 11, 5)
    assert _published_at(session, "https://example.test/never-queued") is None


# --- the default-collapse detector -------------------------------------------


def test_an_axis_stuck_on_one_value_is_reported_as_collapsed(session):
    """`risk.severity` is the cautionary case: every live risk reads `watch`.

    A column that is fully populated and always says the same thing looks like
    data and carries none, so the census has to catch it rather than reporting
    100% coverage and calling it healthy.
    """
    stat = quality.AxisStats(axis="modality", populated=100, total=100, values={"planned": 100})
    assert stat.coverage == 1.0
    assert stat.modal_share == 1.0
    assert stat.modal_share > quality.DEFAULT_COLLAPSE_CEILING


def test_a_genuinely_varied_axis_is_not_reported_as_collapsed(session):
    """Skew is fine — most claims really are about the site in front of you."""
    stat = quality.AxisStats(
        axis="scope",
        populated=100,
        total=100,
        values={"this_site": 80, "programme": 12, "region": 8},
    )
    assert stat.modal_share == 0.80
    assert stat.modal_share < quality.DEFAULT_COLLAPSE_CEILING


def test_the_axis_census_is_empty_before_the_envelope_exists(session):
    """Baseline zero, so the first run after the envelope lands has a number to beat."""
    project = _project(session, mw_planned=350.0)
    _source(session, project, {"mw_planned": 350.0}, quotes={"mw_planned": "350 MW"})
    session.refresh(project)

    assert quality.axis_census(session) == {}


# --- remediation, end to end -------------------------------------------------
#
# The headline assertion: re-reading a legacy row under the current prompt has to
# move the defect count *down*, measured by the same function that reports on the
# live database. Entirely offline — the fetcher and the extractor are injected,
# so this runs on a fresh clone with no API key.

FIXTURES = Path(__file__).parent / "fixtures"
_ARTICLE_URL = "https://www.datacenterdynamics.com/en/news/microsoft-fairwater-mount-pleasant/"


class _FakeFetcher:
    """Returns one canned article, and records what it was asked for."""

    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        return FetchResult(
            url=url,
            ok=True,
            markdown=self.markdown,
            status=200,
            fetched_at=dt.datetime(2026, 2, 1, 12, 0, 0),
            via="httpx",
        )


class _FakeLLM:
    """Replays one canned extraction. `seen` is what proves nothing was spent."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply:
        self.seen.append((system, user))
        return LLMReply(self.reply, "stop", 1200, 400, "fake-model")


def _ingest_once(session) -> None:
    article = (FIXTURES / "article_microsoft_wi.md").read_text(encoding="utf-8")
    reply = (FIXTURES / "llm_response_microsoft_wi.json").read_text(encoding="utf-8")
    crawl.run(
        session,
        [_ARTICLE_URL],
        fetcher=_FakeFetcher(article),
        extractor=_FakeLLM(reply),
        run_id="quality-test",
        force=True,
    )


def _make_it_legacy(session) -> int:
    """Turn a freshly-ingested row into the shape of the 89 live defects.

    Not a synthetic fixture: this is exactly what a row written before migration
    `0007` looks like — the values and `fields` intact, the per-field quotes
    absent because the column did not exist, and an `extractor` stamp naming the
    prompt that produced it.
    """
    stripped = 0
    for source in session.scalars(select(Source)).all():
        if source.quotes:
            source.quotes = None
            stripped += 1
        source.extractor = "crawl:extract-v1@8eb51f2a:MiniMax-M2.7-highspeed:httpx"
    session.flush()
    return stripped


def test_re_reading_a_legacy_row_reduces_the_defect_count(session):
    """Measure, remediate, measure again — and require the number to have fallen.

    This is the test the whole `quality` module exists to make possible. Every
    other test in this file checks that the metric is correct; this one checks
    that the *data* improved, which is a different claim and the only one that
    answers "did this work".
    """
    _ingest_once(session)
    assert _make_it_legacy(session), "the fixture must produce quotes to strip"
    session.expire_all()

    before = quality.evidence_census(session)
    assert before.defects > 0, "stripping the quotes must create defects to fix"
    assert before.defects_by_vintage == {"extract-v1@8eb51f2a": before.defects}

    _ingest_once(session)  # the same article, re-read under the current prompt
    session.expire_all()

    after = quality.evidence_census(session)
    assert after.defects < before.defects, "re-reading must reduce the defect count"
    assert after.defects == 0

    #: Two controls, because "defects fell" is satisfiable by deleting data.
    #: The total must hold, and the values that were already sound must stay sound.
    assert after.total == before.total
    assert after.buckets.get(quality.QUOTE_BACKED, 0) > before.buckets.get(quality.QUOTE_BACKED, 0)


def test_remediation_leaves_nothing_on_a_superseded_prompt(session):
    """The selector's own postcondition: after re-reading, nothing is stale.

    Worth asserting separately from the defect count. A run that repaired the
    quotes but left `source.extractor` naming the old prompt would look fixed and
    would be re-queued forever — which is the bug `backfill` has, since it writes
    `source.blocks` and never updates the stamp beside it.
    """
    _ingest_once(session)
    _make_it_legacy(session)
    session.expire_all()

    stamp = load_prompt("extract-v1").stamp
    assert crawl.stale_by_prompt(session, stamp=stamp) == [_ARTICLE_URL]

    _ingest_once(session)
    session.expire_all()

    assert crawl.stale_by_prompt(session, stamp=stamp) == []
