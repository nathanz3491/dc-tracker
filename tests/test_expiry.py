"""When a decision stops holding (`tracker.expiry`, `tracker logic expire`).

Each test is one promise: a ruling records when and by whom; a sentence reporting a
change reads as one and an echo of an old figure does not; an answer reopens on a
change report published after it and on nothing else, and then is neither scored nor
applied until it is loaded again; a `superseded` ruling lifts when a later report goes
back to its figure and a `misread` never does; an answer's claim is dated, so it
grows stale like any other.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker import answer_apply, answer_key, expiry
from tracker.conflicts import DATED_FROM, lift_ruling, ruling_of, supersede
from tracker.ingest.crawl import reports_change
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.models import AnswerKey, Project, Source
from tracker.upsert import recompute_from_sources, upsert_record

# --- reading a change -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "value", "field", "year"),
    [
        (
            "Amazon recently announced it was expanding its planned data center investment "
            "in Caddo and Bossier Parishes, increasing its investment from $12 billion to "
            "$18 billion.",
            18e9,
            "investment_usd",
            2026,
        ),
        ("In July this year, it raised the investment to more than $50 billion.", 50e9,
         "investment_usd", 2026),
        ("Dalton, Georgia is being expanded to 450 megawatts gross", 450.0, "mw_planned", 2026),
        ("The campus will now total $18 billion, up from $12 billion.", 18e9, "investment_usd",
         2026),
        ("The first phase, now delayed to 2028, was due in 2027.", "2028-01-01",
         "expected_online", 2026),
        ("Opening moved from late 2027 to mid-2028.", "2028-07-01", "expected_online", 2026),
    ],
)  # fmt: skip
def test_a_sentence_saying_the_figure_moved_reports_a_change(quote, value, field, year):
    assert reports_change(quote, value, field, year)


@pytest.mark.parametrize(
    ("quote", "value", "field", "year"),
    [
        # The echo: an old launch figure, printed again two years on.
        ("Meta announced it will build a $10 billion artificial intelligence data center",
         10e9, "investment_usd", 2026),
        # A retelling of an old change, dated by its own clause.
        ("In September 2025, Microsoft revealed it was increasing its investment in the "
         "project to $7.3 billion.", 7.3e9, "investment_usd", 2026),
        # A possibility is a ceiling, not a change.
        ("It has since scaled to 2.7GW with a potential buildout to 10GW", 10000.0,
         "mw_planned", 2026),
        ("The project could grow to 5 GW.", 5000.0, "mw_planned", 2026),
        ("Oracle is brushing off the growing worries around Project Jupiter, its $165 "
         "billion AI data centre campus.", 165e9, "investment_usd", 2026),
        # The old figure of a change is not where it landed.
        ("increasing its investment from $12 billion to $18 billion", 12e9, "investment_usd",
         2026),
        ("Completion is scheduled for 2028.", "2028-01-01", "expected_online", 2026),
    ],
)  # fmt: skip
def test_an_echo_or_a_possibility_is_not_a_change(quote, value, field, year):
    assert not reports_change(quote, value, field, year)


# --- rows -----------------------------------------------------------------------------


def _row(session, name, *sources):
    """A campus cited by `(url, field, value, quote, published)` sources."""
    records = [
        SourceRecord(
            url=url,
            source_type="trade_press",
            excerpt="excerpt",
            claims={field: value},
            quotes={field: quote},
            fetched_at=dt.datetime(2026, 11, 13),
        )
        for url, field, value, quote, _ in sources
    ]
    result = upsert_record(
        session,
        IngestRecord(
            # A company of its own, so two rows in one test are two campuses.
            project={
                "company": f"Owner of {name}",
                "name": name,
                "city": "Englewood",
                "state": "CO",
            },
            sources=records,
        ),
    )
    session.flush()
    project = session.get(Project, result.project_id)
    for url, _f, _v, _q, published in sources:
        source = next(s for s in project.sources if s.url == url)
        source.published_at = published
    session.flush()
    recompute_from_sources(session, project)
    session.commit()
    return project


def _source(project, url) -> Source:
    return next(s for s in project.sources if s.url == url)


def _answer(session, project, field, value, recorded, **kw) -> AnswerKey:
    answer = AnswerKey(
        project_id=project.id,
        field=field,
        value=value,
        status="verified",
        method="test",
        recorded_at=recorded,
        **kw,
    )
    session.add(answer)
    session.commit()
    return answer


# --- rulings carry a date and a name ---------------------------------------------------


def test_a_ruling_records_when_and_by_whom_and_a_lift_forgets_it(session):
    project = _row(
        session,
        "Campus R",
        ("https://a.example/1", "mw_planned", 300.0, "a 300 MW campus", None),
    )
    source = _source(project, "https://a.example/1")

    assert supersede(source, "mw_planned", by="operator", reason="misread")
    ruling = ruling_of(source, "mw_planned")
    assert ruling.reason == "misread" and ruling.by == "operator"
    assert ruling.on == dt.datetime.now(dt.UTC).date()

    assert lift_ruling(source, "mw_planned")
    assert ruling_of(source, "mw_planned") is None
    assert "mw_planned" not in json.loads(source.rulings or "{}")


def test_an_undated_ruling_counts_as_made_when_dating_began(session):
    project = _row(
        session,
        "Campus U",
        ("https://a.example/1", "mw_planned", 300.0, "a 300 MW campus", None),
    )
    source = _source(project, "https://a.example/1")
    source.unconfirmed_reasons = json.dumps({"mw_planned": "superseded"})
    source.rulings = None

    ruling = ruling_of(source, "mw_planned")
    assert ruling.on is None and ruling.made == DATED_FROM


def test_an_entry_left_behind_by_a_dropped_ruling_is_ignored(session):
    project = _row(
        session,
        "Campus D",
        ("https://a.example/1", "mw_planned", 300.0, "a 300 MW campus", None),
    )
    source = _source(project, "https://a.example/1")
    source.rulings = json.dumps({"mw_planned": {"on": "2026-01-01", "by": "agent"}})

    assert ruling_of(source, "mw_planned") is None, "no decision stands, whatever the entry"


# --- answers ----------------------------------------------------------------------------


def test_an_answer_reopens_on_a_later_report_that_the_figure_changed(session):
    project = _row(
        session,
        "Campus A",
        ("https://a.example/old", "investment_usd", 3e9, "a $3 billion campus",
         dt.datetime(2026, 3, 1)),
        ("https://a.example/new", "investment_usd", 5e9,
         "the developer raised its investment to $5 billion", dt.datetime(2026, 12, 2)),
    )  # fmt: skip
    answer = _answer(
        session,
        project,
        "investment_usd",
        "3000000000",
        dt.datetime(2026, 11, 1),
        source_url="https://a.example/old",
        quote="a $3 billion campus",
    )

    found = expiry.expire(session, apply=True)
    session.commit()

    (item,) = found.answers
    assert item.report.source_id == _source(project, "https://a.example/new").id
    assert answer.reopened_on is not None
    assert answer.reopened_by == item.report.source_id
    ruling = ruling_of(_source(project, "https://a.example/old"), "investment_usd")
    assert ruling.reason == "superseded" and ruling.by == expiry.BY
    assert project.investment_usd == 5e9
    assert "answer_reopened" in (project.notes or "")

    assert answer_key.answers(session) == [], "a reopened answer is not scored or applied"
    assert answer_key.score(session).reopened == 1
    assert answer_apply.plan(session) == []
    assert expiry.expire(session, apply=False).answers == [], "reopened once"


def test_an_echo_after_the_answer_does_not_reopen_it(session):
    project = _row(
        session,
        "Campus E",
        ("https://a.example/old", "investment_usd", 3e9, "a $3 billion campus",
         dt.datetime(2026, 3, 1)),
        ("https://a.example/echo", "investment_usd", 10e9,
         "Meta announced it will build a $10 billion data center", dt.datetime(2026, 12, 2)),
    )  # fmt: skip
    _answer(session, project, "investment_usd", "3000000000", dt.datetime(2026, 11, 1))

    assert expiry.expire(session, apply=False).answers == []


def test_a_change_reported_before_the_answer_was_recorded_does_not_reopen_it(session):
    project = _row(
        session,
        "Campus B",
        ("https://a.example/new", "investment_usd", 5e9,
         "the developer raised its investment to $5 billion", dt.datetime(2026, 6, 1)),
    )  # fmt: skip
    _answer(session, project, "investment_usd", "3000000000", dt.datetime(2026, 11, 1))

    assert expiry.expire(session, apply=False).answers == [], "the researcher saw it"


def test_a_later_stage_reopens_a_phase_answer_and_an_earlier_one_does_not(session):
    project = _row(
        session,
        "Campus P",
        ("https://a.example/built", "phase", "operational",
         "the data center began operations this month", dt.datetime(2027, 2, 1)),
    )  # fmt: skip
    _answer(session, project, "phase", "construction", dt.datetime(2026, 11, 1))
    assert [i.field for i in expiry.expire(session, apply=False).answers] == ["phase"]

    other = _row(
        session,
        "Campus Q",
        ("https://a.example/news", "phase", "announced",
         "the project was announced last year", dt.datetime(2027, 2, 1)),
    )  # fmt: skip
    _answer(session, other, "phase", "construction", dt.datetime(2026, 11, 1))
    assert [i.project_id for i in expiry.expire(session, apply=False).answers] == [project.id]


def test_loading_an_answer_again_clears_its_reopening(session):
    project = _row(
        session,
        "Campus L",
        ("https://a.example/x", "mw_planned", 300.0, "a 300 MW campus", None),
    )
    answer = _answer(
        session,
        project,
        "mw_planned",
        "300",
        dt.datetime(2026, 11, 1),
        reopened_on=dt.date(2026, 12, 3),
    )
    record = answer_key.parse(
        {
            "project_id": project.id,
            "name": project.name,
            "field": "mw_planned",
            "value": "300",
            "status": "verified",
            "method": "re-checked",
        }
    )
    answer_key.load(session, [record], replace=True)

    assert answer.reopened_on is None and answer.reopened_by is None


# --- superseded rulings ------------------------------------------------------------------


def _ruled(source, field, reason, on):
    source.unconfirmed_reasons = json.dumps({field: reason})
    source.unconfirmed_fields = field
    source.rulings = json.dumps({field: {"on": on, "by": "model"}})


def test_a_superseded_ruling_lifts_when_a_later_report_goes_back_to_its_figure(session):
    project = _row(
        session,
        "Campus S",
        ("https://a.example/2024", "mw_planned", 300.0, "a 300 MW campus",
         dt.datetime(2024, 5, 1)),
        ("https://a.example/2025", "mw_planned", 500.0, "now a 500 MW campus",
         dt.datetime(2025, 5, 1)),
        ("https://a.example/2026", "mw_planned", 300.0,
         "the developer scaled back the campus to 300 MW", dt.datetime(2026, 5, 1)),
    )  # fmt: skip
    old = _source(project, "https://a.example/2024")
    _ruled(old, "mw_planned", "superseded", "2025-06-01")
    session.commit()

    found = expiry.expire(session, apply=True)
    session.commit()

    (item,) = found.rulings
    assert item.source_id == old.id
    assert ruling_of(old, "mw_planned") is None
    assert "ruling_reopened" in (project.notes or "")


def test_a_misread_never_lifts_and_a_report_before_the_ruling_lifts_nothing(session):
    project = _row(
        session,
        "Campus M",
        ("https://a.example/misread", "mw_planned", 300.0, "Building A is 300 MW",
         dt.datetime(2024, 5, 1)),
        ("https://a.example/early", "mw_planned", 200.0, "a 200 MW campus",
         dt.datetime(2024, 5, 1)),
        ("https://a.example/2026", "mw_planned", 300.0,
         "the developer scaled back the campus to 300 MW", dt.datetime(2026, 5, 1)),
        ("https://a.example/2026b", "mw_planned", 200.0,
         "the developer cut the campus to 200 MW", dt.datetime(2026, 5, 1)),
    )  # fmt: skip
    _ruled(_source(project, "https://a.example/misread"), "mw_planned", "misread", "2025-06-01")
    _ruled(_source(project, "https://a.example/early"), "mw_planned", "superseded", "2026-09-01")
    session.commit()

    assert expiry.expire(session, apply=False).rulings == []


# --- answer claims are dated --------------------------------------------------------------


def test_an_answers_claim_is_dated_and_grows_stale_like_any_other(session):
    project = _row(
        session,
        "Campus K",
        ("https://a.example/wrong", "mw_planned", 900.0, "a 900 MW campus",
         dt.datetime(2026, 1, 1)),
    )  # fmt: skip
    _answer(
        session,
        project,
        "mw_planned",
        "300",
        dt.datetime(2026, 1, 15),
        source_url="https://key.example/page",
        quote="a 300 MW campus",
    )
    answer_apply.apply(session, answer_apply.plan(session))
    session.commit()
    cited = _source(project, "https://key.example/page")
    assert json.loads(cited.claim_meta)["mw_planned"]["as_of"] == "2026-01-15"
    assert project.mw_planned == 300.0

    # Over a year later a lighter, first-hand article reports another figure. The
    # answer's claim, dated, is stale beside it and no longer wins on weight alone.
    upsert_record(
        session,
        IngestRecord(
            project={"company": "Someone", "name": "Campus K", "city": "Englewood", "state": "CO"},
            sources=[
                SourceRecord(
                    url="https://news.example/2027",
                    source_type="general_media",
                    excerpt="excerpt",
                    claims={"mw_planned": 450.0},
                    quotes={"mw_planned": "a 450 MW campus"},
                    fetched_at=dt.datetime(2027, 6, 1),
                )
            ],
        ),
        route_to=project.id,
    )
    session.flush()
    _source(project, "https://news.example/2027").published_at = dt.datetime(2027, 6, 1)
    recompute_from_sources(session, project)
    assert project.mw_planned == 450.0


def test_an_undated_answer_claim_is_dated_by_expire(session):
    project = _row(
        session,
        "Campus T",
        ("https://key.example/page", "mw_planned", 300.0, "a 300 MW campus", None),
    )
    _answer(
        session,
        project,
        "mw_planned",
        "300",
        dt.datetime(2026, 10, 31),
        source_url="https://key.example/page",
        quote="a 300 MW campus",
    )

    assert expiry.date_answer_claims(session, apply=True) == 1
    meta = json.loads(_source(project, "https://key.example/page").claim_meta)
    assert meta["mw_planned"]["as_of"] == "2026-10-31"
    assert expiry.date_answer_claims(session, apply=True) == 0, "idempotent"
