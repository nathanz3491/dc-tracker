"""The conflict solver: what it puts in front of a model, and what it refuses.

Every value in the database was extracted from one article in isolation, so no
model has ever compared two sentences that contradict. This is the one path where
one does — and the tests that matter are the ones about what it is NOT allowed to
do: author a value, pick between two options it cannot separate, or argue with
itself indefinitely.

The stop rules were written before the module and are pinned here.
"""

from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import select

from tracker import conflicts
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.models import Project
from tracker.upsert import upsert_record

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)
T1 = dt.datetime(2026, 1, 11, 12, 0, 0)


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text
        self.finish_reason = "stop"
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.model = "fake"


class _Extractor:
    """Answers each call from a queue, and records what it was asked."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls: list[str] = []

    def complete(self, *, system, user, max_tokens=None):
        self.calls.append(user)
        return _Reply(self._replies.pop(0) if self._replies else "{}")


def _source(url, *, source_type="trade_press", when=T0, published=None, **claims):
    return SourceRecord(
        url=url,
        source_type=source_type,
        excerpt="excerpt",
        claims=claims,
        quotes={k: f"the campus represents {v}" for k, v in claims.items()},
        fetched_at=when,
    )


def _hyperion(session, *sources):
    result = upsert_record(
        session,
        IngestRecord(
            project={"company": "Meta", "name": "Hyperion", "city": "Richland", "state": "LA"},
            sources=list(sources),
        ),
    )
    session.commit()
    return session.get(Project, result.project_id)


def _contested(session):
    """#10's real shape, reduced: the superseded figure winning on crawl order.

    Both sources are weight 3 and both are quote-backed, so credibility separates
    nothing and the tie falls to *recency* — which means `fetched_at`, the order we
    happened to visit. Here the 2024 announcement was crawled second, so the row
    holds $10B over the 2026 restatement. That is the live defect, exactly.
    """
    return _hyperion(
        session,
        _source(
            "https://meta.example/2026",
            source_type="company_filing",
            investment_usd=50_000_000_000,
        ),
        _source(
            "https://gov.example/2024",
            source_type="government_doc",
            when=T1,
            investment_usd=10_000_000_000,
        ),
    )


# --- what counts as a dispute -----------------------------------------------


def test_two_quote_backed_figures_are_a_dispute(session):
    project = _contested(session)
    (dispute,) = [d for d in conflicts.disputes(project) if d.field == "investment_usd"]
    assert {o.value for o in dispute.options} == {10_000_000_000, 50_000_000_000}
    assert [o.key for o in dispute.options] == ["a", "b"]
    assert all(o.quote for o in dispute.options)


def test_an_identity_field_is_never_a_dispute(session):
    """"Hyperion" against "Richland Parish Data Center" is two names, not two facts.

    `FILL_ONLY` says churn in an identity field is worse than staleness, and ruling
    against a claim would not even move the value — `resolve` keeps what the row
    holds. Spending a model call on it buys nothing at all.
    """
    project = _hyperion(
        session,
        _source("https://a.example/x", name="Hyperion"),
        _source("https://b.example/y", when=T1, name="Richland Parish Data Center"),
    )
    assert [d.field for d in conflicts.disputes(project)] == []


def test_an_unquoted_rival_is_not_a_dispute(session):
    """A 待确认 claim already loses to a confirmed one by rule, under every policy."""
    project = _hyperion(
        session,
        _source("https://a.example/x", investment_usd=10_000_000_000),
        SourceRecord(
            url="https://b.example/y",
            source_type="company_filing",
            excerpt="e",
            claims={"investment_usd": 50_000_000_000},
            unconfirmed=frozenset({"investment_usd"}),
            fetched_at=T1,
        ),
    )
    assert [d.field for d in conflicts.disputes(project)] == []


def test_the_crawl_date_is_never_shown_as_a_publication_date(session):
    """The prompt reasons about supersession, so it must not be handed crawl order.

    `fetched_at` is when we happened to visit. A model shown a bare date would
    conclude the article we read second is the later one — which is the exact
    mistake this whole change exists to stop.
    """
    project = _contested(session)
    (dispute,) = [d for d in conflicts.disputes(project) if d.field == "investment_usd"]
    assert all("publication date unknown; crawled" in o.when for o in dispute.options)


# --- the two calls ----------------------------------------------------------


def test_it_picks_an_offered_value_and_supersedes_the_rest(session):
    project = _contested(session)
    (dispute,) = [d for d in conflicts.disputes(project) if d.field == "investment_usd"]
    later = next(o for o in dispute.options if o.value == 50_000_000_000)

    extractor = _Extractor(
        json.dumps({"pick": later.key, "confidence": 0.9, "reason": "the 2026 filing restates it"}),
        json.dumps({"stands": True, "reason": "the rival is the 2024 announcement"}),
    )
    outcome = conflicts.solve(dispute, extractor=extractor)

    assert outcome.verdict == "resolved"
    assert outcome.chosen.value == 50_000_000_000
    assert outcome.calls == 2
    assert outcome.checked is True
    assert outcome.superseded == ("https://gov.example/2024",)


def test_refusing_is_a_real_answer(session):
    project = _contested(session)
    (dispute,) = [d for d in conflicts.disputes(project) if d.field == "investment_usd"]

    extractor = _Extractor(json.dumps({"pick": "r", "confidence": 0.9, "reason": "both stand"}))
    outcome = conflicts.solve(dispute, extractor=extractor)

    assert outcome.verdict == "refused"
    assert outcome.calls == 1
    assert outcome.superseded == ()


def test_a_key_nobody_offered_is_a_refusal_not_a_guess(session):
    project = _contested(session)
    (dispute,) = [d for d in conflicts.disputes(project) if d.field == "investment_usd"]

    outcome = conflicts.solve(
        dispute,
        extractor=_Extractor(json.dumps({"pick": "z", "confidence": 1.0, "reason": "x"})),
    )
    assert outcome.verdict == "refused"


def test_a_hedged_answer_is_discarded(session):
    """Below the floor an answer is not a quieter opinion — it is one nothing sees.

    A confidence written into the database is a hedge nothing downstream can read,
    so the hedge has to be spent here.
    """
    project = _contested(session)
    (dispute,) = [d for d in conflicts.disputes(project) if d.field == "investment_usd"]

    outcome = conflicts.solve(
        dispute,
        extractor=_Extractor(json.dumps({"pick": "a", "confidence": 0.4, "reason": "leaning"})),
    )
    assert outcome.verdict == "refused"
    assert "discarded at 0.40" in outcome.reason


def test_a_knocked_down_answer_becomes_a_refusal_and_never_a_third_call(session):
    """The flowchart's "go round again" arrow has no limit on it. This one does."""
    project = _contested(session)
    (dispute,) = [d for d in conflicts.disputes(project) if d.field == "investment_usd"]

    extractor = _Extractor(
        json.dumps({"pick": "a", "confidence": 0.9, "reason": "the older figure"}),
        json.dumps({"stands": False, "reason": "option b is the later restatement"}),
    )
    outcome = conflicts.solve(dispute, extractor=extractor)

    assert outcome.verdict == "refused"
    assert "option b is the later restatement" in outcome.reason
    assert outcome.calls == conflicts.MAX_CALLS_PER_FIELD == len(extractor.calls) == 2


# --- writing ----------------------------------------------------------------


def test_applying_moves_the_value_by_superseding_the_loser(session):
    """The field is never assigned. Marking the loser and re-deriving is the write.

    Assigning it directly would make the row something somebody typed, and the next
    `backfill derive` would put it back — the database's one guarantee is that a
    value equals what its citations imply.
    """
    project = _contested(session)
    assert project.investment_usd == 10_000_000_000

    (dispute,) = [d for d in conflicts.disputes(project) if d.field == "investment_usd"]
    later = next(o for o in dispute.options if o.value == 50_000_000_000)
    outcome = conflicts.solve(
        dispute,
        extractor=_Extractor(
            json.dumps({"pick": later.key, "confidence": 0.9, "reason": "restated in 2026"}),
            json.dumps({"stands": True, "reason": "nothing rivals it"}),
        ),
    )
    assert conflicts.apply_outcome(session, project, outcome) == 1
    session.commit()

    assert project.investment_usd == 50_000_000_000
    loser = session.scalar(select(conflicts.Source).where(conflicts.Source.url.like("%gov%")))
    assert json.loads(loser.unconfirmed_reasons)["investment_usd"] == "superseded"
    # The claim itself is untouched: the article still said what it said in 2024.
    assert json.loads(loser.claims)["investment_usd"] == 10_000_000_000


def test_a_refusal_writes_nothing(session):
    project = _contested(session)
    (dispute,) = [d for d in conflicts.disputes(project) if d.field == "investment_usd"]
    outcome = conflicts.solve(
        dispute, extractor=_Extractor(json.dumps({"pick": "r", "confidence": 0.9, "reason": "no"}))
    )
    assert conflicts.apply_outcome(session, project, outcome) == 0
    assert project.investment_usd == 10_000_000_000


def test_superseding_twice_is_a_no_op(session):
    project = _contested(session)
    source = project.sources[0]
    assert conflicts.supersede(source, "investment_usd") is True
    assert conflicts.supersede(source, "investment_usd") is False
    assert "investment_usd" in (source.unconfirmed_fields or "")
    # `fields` keeps it, and that is not an oversight: the column means "a verbatim
    # quote supports this", and the 2024 article really did say $10 billion. This is
    # the shape `upsert_record` writes when it carries a superseded reason across a
    # re-crawl, so the two paths agree.
    assert "investment_usd" in (source.fields or "")


def test_a_superseded_claim_survives_a_re_read_of_the_same_article(session):
    """The article has not changed; the world has.

    Re-crawling the page that said $10B must not clear the mark and hand the merge
    straight back to crawl order. `upsert.DECIDED_REASONS` is what carries it, and
    this pins that the solver's write is the kind that gets carried.
    """
    project = _contested(session)
    older = next(s for s in project.sources if "gov.example" in s.url)
    conflicts.supersede(older, "investment_usd")
    session.commit()

    _hyperion(
        session,
        _source("https://gov.example/2024", source_type="government_doc", investment_usd=10_000_000_000),
    )
    source = session.scalar(
        select(conflicts.Source).where(conflicts.Source.url == "https://gov.example/2024")
    )
    assert json.loads(source.unconfirmed_reasons)["investment_usd"] == "superseded"


def test_r_is_never_an_option_key():
    """`r` is the refusal key. An option lettered `r` would turn a refusal into a
    silent pick of that option — the one outcome this module exists to prevent."""
    assert "r" not in conflicts._KEYS
