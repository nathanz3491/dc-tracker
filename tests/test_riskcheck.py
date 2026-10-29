"""Confirming or refuting the obstacles nobody could quote.

A third of the open obstacles on the live database are 待确认: a source named them
and the evidence gate could not find a sentence that says so. They are counted in
the exposure numbers anyway, because a reported obstacle is still an obstacle, and
until `tracker risks confirm` nobody had ever gone back to check which reading was
right.

What makes the answer trustworthy is not the model. Every quote it returns is
checked against the article with the same matcher the extraction path uses, and
against the same category vocabulary. These tests are mostly about the refusals.
"""

from __future__ import annotations

import datetime as dt

from tracker import riskcheck
from tracker.models import Project, Risk, Source

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)

ARTICLE = (
    "Dominion Energy told the commission that the Loudoun interconnection cannot be "
    "energised before 2029 without new transmission. The developer said it remains "
    "confident in the schedule. Residents raised concerns about noise at the hearing."
)


def _seed(session, *, category="transmission", quote=None, unconfirmed="no_quote", excerpt=ARTICLE):
    project = Project(
        name="Ashburn Campus",
        company="Someone",
        city="Ashburn",
        state="VA",
        dedup_key="k1",
        phase="construction",
        confidence=2,
        mw_planned=100.0,
    )
    session.add(project)
    session.flush()
    source = Source(
        project_id=project.id,
        url="https://example.test/a",
        source_type="trade_press",
        fetched_at=T0,
        excerpt=excerpt,
    )
    session.add(source)
    session.flush()
    risk = Risk(
        project_id=project.id,
        category=category,
        severity="material",
        status="open",
        summary="Interconnection cannot be energised before 2029.",
        quote=quote,
        unconfirmed=unconfirmed,
        source_id=source.id,
    )
    session.add(risk)
    session.flush()
    return project, risk, source


class _Model:
    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, *, system, user, max_tokens):
        self.prompts.append(user)

        class R:
            text = self.reply
            model = "test-model"

        return R()


def test_only_the_unquoted_obstacles_are_offered(session):
    project, risk, _ = _seed(session)
    quoted = Risk(
        project_id=project.id,
        category="water",
        severity="watch",
        status="open",
        summary="Cooling water.",
        quote="the sentence",
        unconfirmed=None,
    )
    session.add(quoted)
    session.flush()

    assert [r.id for r in riskcheck.unconfirmed_risks(session)] == [risk.id]


def test_a_verbatim_sentence_confirms_the_obstacle(session):
    _, risk, _ = _seed(session)
    model = _Model(
        '{"verdict": "confirmed", "confidence": 0.9, "reason": "states it", '
        '"quote": "Dominion Energy told the commission that the Loudoun '
        'interconnection cannot be energised before 2029 without new transmission."}'
    )
    (outcome,) = riskcheck.confirm(session, [risk], extractor=model)

    assert outcome.result == "confirmed"
    assert risk.unconfirmed is None
    assert "cannot be energised before 2029" in risk.quote


def test_a_paraphrase_is_refused_and_the_obstacle_is_left_alone(session):
    """The whole point. A confirmation looser than the gate it overturns would be a
    way to launder a paraphrase into a citation."""
    _, risk, _ = _seed(session)
    model = _Model(
        '{"verdict": "confirmed", "confidence": 0.95, "reason": "close enough", '
        '"quote": "The utility said the grid connection will be late."}'
    )
    (outcome,) = riskcheck.confirm(session, [risk], extractor=model)

    assert outcome.result == "unclear"
    assert risk.unconfirmed == "no_quote", "unchanged"
    assert outcome.judgement.rejected_quote
    assert "not in the article" in outcome.judgement.reason


def test_a_real_sentence_about_the_wrong_thing_is_refused(session):
    """Half of these rows are a real sentence filed under the wrong category."""
    _, risk, _ = _seed(session, category="water")
    model = _Model(
        '{"verdict": "confirmed", "confidence": 0.9, "reason": "it is in there", '
        '"quote": "The developer said it remains confident in the schedule."}'
    )
    (outcome,) = riskcheck.confirm(session, [risk], extractor=model)

    assert outcome.result == "unclear"
    assert "does not state a `water` obstacle" in outcome.judgement.reason


def test_any_form_of_the_category_s_wording_confirms_it(session):
    """#1483 on 2026-10-19: "filed, not approved" was refused as a permitting obstacle
    because the check looked for the substring "approval". The sentence is stored as
    the article wrote it, and without the sentence after it."""
    article = (
        "The October application has been filed, not approved. The state has not yet "
        "advertised a protest period."
    )
    _, risk, _ = _seed(session, category="permitting", excerpt=article)
    model = _Model(
        '{"verdict": "confirmed", "confidence": 0.9, "reason": "states it", '
        '"quote": "The October application has been filed, not approved."}'
    )
    (outcome,) = riskcheck.confirm(session, [risk], extractor=model)

    assert outcome.result == "confirmed"
    assert risk.quote == "The October application has been filed, not approved."


def _declined(session, risk, source, *, reason: str, days_ago: float):
    """An `unclear` decline on this obstacle, recorded `days_ago`."""
    from tracker import declines
    from tracker.models import ModelDecline, utcnow

    declines.record(
        session,
        "risk",
        str(risk.id),
        riskcheck.evidence(risk, source),
        outcome="unclear",
        reason=reason,
        by="model",
    )
    row = session.query(ModelDecline).filter_by(kind="risk", subject=str(risk.id)).one()
    row.decided_at = utcnow() - dt.timedelta(days=days_ago)
    session.flush()


def test_an_obstacle_the_old_category_check_refused_is_read_again(session, monkeypatch):
    """131 of the 159 obstacles held back on 2026-10-19 were the model's answer
    overruled by the check, not the model's indecision. Once the check changes,
    those are worth one more call; the model's own "unclear" is not."""
    from tracker.models import utcnow

    monkeypatch.setattr(riskcheck, "CATEGORY_CHECK_REVISED_AT", utcnow() - dt.timedelta(days=1))
    _, risk, source = _seed(session)
    refused = "it says so — refused: the sentence does not state a `transmission` obstacle"

    _declined(session, risk, source, reason=refused, days_ago=2)
    assert riskcheck.fresh(session, [risk]) == ([risk], 0), "refused before the change"

    _declined(session, risk, source, reason=refused, days_ago=0)
    assert riskcheck.fresh(session, [risk]) == ([], 1), "refused by the current check"

    _declined(session, risk, source, reason="the article does not say", days_ago=2)
    assert riskcheck.fresh(session, [risk]) == ([], 1), "the model's own unclear holds"


def test_refuting_retires_the_obstacle_without_deleting_it(session):
    """The row records that a source was read this way once; the next crawl of the
    same article would otherwise recreate it with nothing to say it was rejected."""
    _, risk, _ = _seed(session)
    model = _Model('{"verdict": "refuted", "confidence": 0.9, "reason": "not in this article"}')
    (outcome,) = riskcheck.confirm(session, [risk], extractor=model)

    assert outcome.result == "refuted"
    assert risk.status == riskcheck.REFUTED_STATUS
    assert risk.unconfirmed == "no_quote", "the reason it was doubted is still on the row"


def test_below_the_confidence_floor_nothing_moves(session):
    _, risk, _ = _seed(session)
    model = _Model('{"verdict": "refuted", "confidence": 0.4, "reason": "probably not"}')
    (outcome,) = riskcheck.confirm(session, [risk], extractor=model)

    assert outcome.result == "unclear"
    assert risk.status == "open"
    assert "below the" in outcome.judgement.reason


def test_a_dry_run_judges_at_full_cost_and_writes_nothing(session):
    """The same code decides either way — only the assignment is skipped — so a
    preview cannot report an outcome the real run would not produce."""
    _, risk, _ = _seed(session)
    model = _Model(
        '{"verdict": "confirmed", "confidence": 0.9, "reason": "states it", '
        '"quote": "Dominion Energy told the commission that the Loudoun '
        'interconnection cannot be energised before 2029 without new transmission."}'
    )
    (outcome,) = riskcheck.confirm(session, [risk], extractor=model, apply=False)

    assert outcome.result == "confirmed"
    assert risk.quote is None and risk.unconfirmed == "no_quote"


def test_an_unreadable_reply_is_an_error_not_a_verdict(session):
    _, risk, _ = _seed(session)
    (outcome,) = riskcheck.confirm(session, [risk], extractor=_Model("sorry, I cannot"))

    assert outcome.result == "error"
    assert risk.status == "open"


def test_a_row_with_no_article_text_is_reported_rather_than_guessed(session):
    _, risk, _ = _seed(session, excerpt="")
    model = _Model("{}")
    (outcome,) = riskcheck.confirm(session, [risk], extractor=model)

    assert outcome.result == "no_article"
    assert model.prompts == [], "nothing to read, so nothing was asked"


def test_no_article_is_remembered_until_the_article_arrives(session):
    """It was recorded nowhere, so the same obstacle was offered again every round.
    Deliberate change: it is now held like `unclear`, on a hash that covers the
    article text, so the day the page is cached it is a new question."""
    from tracker import declines
    from tracker.models import ModelDecline, utcnow

    _, risk, source = _seed(session, excerpt="")
    riskcheck.confirm(session, [risk], extractor=_Model("{}"))
    row = session.query(ModelDecline).filter_by(kind="risk", subject=str(risk.id)).one()
    assert row.outcome == "no_article"

    row.decided_at = utcnow() - dt.timedelta(hours=declines.TONIGHT_HOURS + 1)
    session.flush()
    assert riskcheck.fresh(session, [risk]) == ([], 1), "still nothing to read"
    source.excerpt = ARTICLE
    session.flush()
    assert riskcheck.fresh(session, [risk]) == ([risk], 0), "the article is new evidence"


def test_a_failed_call_is_kept_and_the_batch_goes_on(session):
    """One obstacle's failure no longer ends the batch, and what came before stays."""
    _, first, _ = _seed(session)
    second = Risk(
        project_id=first.project_id,
        category="water",
        severity="watch",
        status="open",
        summary="Cooling water.",
        unconfirmed="no_quote",
        source_id=first.source_id,
    )
    session.add(second)
    session.flush()

    class Flaky(_Model):
        def complete(self, *, system, user, max_tokens):
            if len(self.prompts) == 1:
                self.prompts.append(user)
                raise RuntimeError("socket closed")
            return super().complete(system=system, user=user, max_tokens=max_tokens)

    model = Flaky('{"verdict": "refuted", "confidence": 0.9, "reason": "not in this article"}')
    outcomes = riskcheck.confirm(session, [second, first], extractor=model)
    assert [o.result for o in outcomes] == ["refuted", "error"]
    assert second.status == riskcheck.REFUTED_STATUS, "the first answer is committed"


def test_only_an_obstacle_with_a_decline_has_its_article_read(session, monkeypatch):
    """`fresh` hashed every candidate's whole article each round to compare with a
    decline most of them did not have."""
    _, risk, _ = _seed(session)
    read: list[int] = []
    real = riskcheck.article_for
    monkeypatch.setattr(
        riskcheck, "article_for", lambda source, **kw: read.append(1) or real(source, **kw)
    )
    assert riskcheck.fresh(session, [risk]) == ([risk], 0)
    assert read == []


def test_the_prompt_carries_the_other_obstacles_on_the_row(session):
    """A model cannot recognise a miscategorised obstacle without seeing what the
    other categories on the row already claim."""
    project, risk, _ = _seed(session)
    session.add(
        Risk(
            project_id=project.id,
            category="water",
            severity="watch",
            status="open",
            summary="Cooling water withdrawals under review.",
        )
    )
    session.flush()
    session.refresh(project)

    model = _Model('{"verdict": "unclear", "confidence": 0.9, "reason": "cannot tell"}')
    riskcheck.confirm(session, [risk], extractor=model)

    assert "Cooling water withdrawals under review." in model.prompts[0]
    assert "Dominion Energy told the commission" in model.prompts[0], "the article is included"


def test_the_cached_article_beats_the_excerpt(session, tmp_path):
    """The excerpt is the fragment the extraction that already failed chose; asking
    a second model to re-read it would mostly reproduce the first answer."""
    from tracker.ingest.fetch import cache_path

    _, _risk, source = _seed(session, excerpt="a short teaser")
    cache_dir = tmp_path / "articles"
    cache_dir.mkdir()
    cache_path(source.url, cache_dir).write_text(ARTICLE, encoding="utf-8")

    assert riskcheck.article_for(source, cache_dir=cache_dir).startswith("Dominion Energy")
    assert riskcheck.article_for(source, cache_dir=tmp_path / "empty") == "a short teaser"
