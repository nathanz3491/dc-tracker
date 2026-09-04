"""Stopping a duplicate at write time instead of reporting it afterwards.

The arithmetic this exists for: a duplicate created at ingest costs a row nobody
wanted, one side of the pair held out of every `capex` total until somebody settles
it, and then a person or a merge that deletes a row. Measured here: 47 groups
holding 22,012 MW twice, cleared by a ten-hour agent run. The same judgement before
the insert costs one call and deletes nothing.

Every test below is really about one property — **it fails open.** Unsure, errored,
under-confident, unquoted: all of them insert, which is exactly what happens today.
That is what makes it safe to leave on.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker import gatekeeper
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.llm import LLMReply, ToolCall
from tracker.models import Project
from tracker.upsert import upsert_record

_URL = "https://example.test/racine-county"
_ARTICLE = (
    "Microsoft confirmed that its Racine County development is the Mount Pleasant "
    "campus it announced in 2023, on the same 1,030-acre parcel in the village. "
    "The project is a single campus and not a second site."
)


@pytest.fixture
def stored(session):
    """A city-level row. The arriving record below is its county twin."""
    project = Project(
        name="Fairwater",
        company="Microsoft",
        city="Mount Pleasant",
        county="Racine",
        state="WI",
        dedup_key="microsoft|city:mount pleasant|WI",
        phase="construction",
    )
    session.add(project)
    session.flush()
    return project


def _arriving() -> IngestRecord:
    """The same campus, filed under the county — the shape that makes duplicates."""
    return IngestRecord(
        project={
            "company": "Microsoft",
            "name": "Racine County Data Center",
            "county": "Racine",
            "state": "WI",
            "mw_planned": 1030.0,
        },
        sources=[
            SourceRecord(
                url=_URL,
                source_type="trade_press",
                fetched_at=dt.datetime(2026, 6, 1),
                excerpt="Microsoft's Racine County development.",
                claims={"mw_planned": 1030.0},
                quotes={"mw_planned": "the same 1,030-acre parcel in the village"},
            )
        ],
    )


@pytest.fixture
def cached(tmp_path, monkeypatch):
    from tracker import agent
    from tracker.ingest.fetch import cache_path

    cache_path(_URL, tmp_path).write_text(_ARTICLE, encoding="utf-8")
    real = agent.evidence_toolkit
    monkeypatch.setattr(
        agent,
        "evidence_toolkit",
        lambda s, **kw: real(s, cache_dir=tmp_path, allow_search=kw.get("allow_search", True)),
    )
    return tmp_path


class _Model:
    """Reads the arriving article, then returns the verdict it was built with."""

    def __init__(self, tool: str, *, confidence: float = 0.95, quote: str = "", read: bool = True):
        self.tool, self.confidence, self.quote, self.read = tool, confidence, quote, read
        self.turn = 0

    def converse(self, *, system, messages, tools=None, max_tokens=None):
        self.turn += 1
        if self.turn == 1 and self.read:
            args = {"url": _URL}
            return LLMReply(
                text="",
                tool_calls=(
                    ToolCall(
                        id="r", name="read_article", arguments=args, raw_arguments=json.dumps(args)
                    ),
                ),
            )
        args: dict = {"reason": "one campus filed at two precisions"}
        if self.tool != "unsure":
            args["confidence"] = self.confidence
        if self.quote:
            args["quote"] = self.quote
        return LLMReply(
            text="",
            tool_calls=(
                ToolCall(id="v", name=self.tool, arguments=args, raw_arguments=json.dumps(args)),
            ),
        )


# --- the point ---------------------------------------------------------------


def test_a_confirmed_same_site_routes_instead_of_inserting(session, stored, cached):
    """The duplicate is never created, and nothing is deleted to achieve that."""
    arbiter = gatekeeper.same_site_arbiter(
        _Model("same_site", confidence=0.95, quote="is the Mount Pleasant campus it announced")
    )

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.project_id == stored.id
    assert result.action != "insert"
    assert session.query(Project).count() == 1, "a second row was created anyway"
    assert stored.mw_planned == 1030.0, "the arriving claim did not reach the row"


def test_the_routing_decision_is_written_into_the_row(session, stored, cached):
    """A routing that leaves no trace is indistinguishable later from the row
    having always been this way."""
    arbiter = gatekeeper.same_site_arbiter(
        _Model("same_site", confidence=0.95, quote="is the Mount Pleasant campus it announced")
    )

    upsert_record(session, _arriving(), arbiter=arbiter)

    assert "routed an arriving record" in (stored.notes or "")
    assert "agent (0.95)" in (stored.notes or "")


# --- failing open, which is the whole safety argument ------------------------


def test_unsure_inserts_exactly_as_today(session, stored, cached):
    arbiter = gatekeeper.same_site_arbiter(_Model("unsure"))

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_different_site_inserts(session, stored, cached):
    arbiter = gatekeeper.same_site_arbiter(_Model("different_site", confidence=0.99))

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_confidence_below_the_floor_inserts(session, stored, cached):
    """Higher bar than a merge needs: this is decided from one arriving article."""
    arbiter = gatekeeper.same_site_arbiter(
        _Model("same_site", confidence=0.7, quote="is the Mount Pleasant campus it announced")
    )

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_a_same_site_verdict_without_a_real_quote_inserts(session, stored, cached):
    arbiter = gatekeeper.same_site_arbiter(
        _Model("same_site", confidence=0.99, quote="they share a substation and a switchyard")
    )

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_an_arbiter_that_raises_does_not_break_the_ingest(session, stored, cached):
    """An ingest of 300 articles must not die on one arbitration."""

    def _boom(**_kwargs):
        raise RuntimeError("provider on fire")

    result = upsert_record(session, _arriving(), arbiter=_boom)

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_a_provider_without_tools_inserts(session, stored, cached):
    class _Old:
        def complete(self, *, system, user, max_tokens=None):
            return LLMReply(text="{}")

    arbiter = gatekeeper.same_site_arbiter(_Old())

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.action == "insert"


def test_no_arbiter_is_the_old_behaviour(session, stored):
    """The default path is untouched, so every existing caller is unaffected."""
    result = upsert_record(session, _arriving())

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_a_record_with_no_citation_is_not_arbitrated(session, stored):
    """Hand-curated and ISO rows have no article, so there is nothing to read and
    nothing this can add. No call is made."""

    class _Never:
        def converse(self, **_kwargs):
            raise AssertionError("should not have called the model")

    arbiter = gatekeeper.same_site_arbiter(_Never())
    record = _arriving()
    record.sources.clear()

    result = upsert_record(session, record, arbiter=arbiter)

    assert result.action == "insert"


def test_an_exact_key_match_never_reaches_the_arbiter(session, stored):
    """Only an otherwise-new row is arbitrated. A record that already matches by
    key is an update and costs nothing."""

    class _Never:
        def converse(self, **_kwargs):
            raise AssertionError("should not have called the model")

    arbiter = gatekeeper.same_site_arbiter(_Never())
    record = _arriving()
    record.project["city"] = "Mount Pleasant"
    record.project.pop("county", None)

    result = upsert_record(session, record, arbiter=arbiter)

    assert result.project_id == stored.id
    assert session.query(Project).count() == 1


# --- the warm path: judging from what extraction already read -----------------
#
# The cold tests above all need the `cached` fixture, because the model has to go
# and fetch the article through `read_article` before it can rule on it. **Not one
# test below uses it.** That absence is the point: the article travels with the
# record, so nothing is fetched, and a run whose pages answer 403 can still
# prevent its duplicates.


def _context(url: str = _URL, article: str = _ARTICLE, limit: int = 24_000):
    from tracker.ingest.crawl import ExtractionContext

    return ExtractionContext(url=url, markdown=article, max_input_chars=limit)


class _WarmModel:
    """Rules in one turn from what it was handed, and records what that was."""

    def __init__(
        self,
        tool: str = "same_site",
        *,
        confidence: float = 0.95,
        quote: str = "",
        reply: LLMReply | None = None,
        raises: Exception | None = None,
    ):
        self.tool, self.confidence, self.quote = tool, confidence, quote
        self.reply, self.raises = reply, raises
        self.seen: list[dict] = []

    def converse(self, *, system, messages, tools=None, max_tokens=None):
        self.seen.append(
            {"system": system, "messages": messages, "tools": tools, "max_tokens": max_tokens}
        )
        if self.raises is not None:
            raise self.raises
        if self.reply is not None:
            return self.reply
        args: dict = {"reason": "one campus filed at two precisions"}
        if self.tool != "unsure":
            args["confidence"] = self.confidence
        if self.quote:
            args["quote"] = self.quote
        return LLMReply(
            text="",
            tool_calls=(
                ToolCall(id="v", name=self.tool, arguments=args, raw_arguments=json.dumps(args)),
            ),
        )

    @property
    def sent(self) -> str:
        """The single user turn it was given."""
        return self.seen[-1]["messages"][0]["content"]


_GOOD_QUOTE = "is the Mount Pleasant campus it announced in 2023"


def test_a_warm_verdict_routes_without_ever_fetching_the_article(session, stored):
    """No `cached` fixture, no cache file, no fetcher — and it still decides.

    This is the whole change: the cold path would have gone to the network for an
    article extraction had just finished reading.
    """
    model = _WarmModel("same_site", confidence=0.95, quote=_GOOD_QUOTE)
    arbiter = gatekeeper.same_site_arbiter(model).for_article(_context())

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.project_id == stored.id
    assert session.query(Project).count() == 1
    assert len(model.seen) == 1, "a one-turn decision should cost exactly one call"


def test_the_warm_turn_carries_the_article_the_proposal_and_the_stored_row(session, stored):
    """All three, or the model is being asked to rule on something it cannot see."""
    model = _WarmModel("same_site", confidence=0.95, quote=_GOOD_QUOTE)
    arbiter = gatekeeper.same_site_arbiter(model).for_article(_context())

    upsert_record(session, _arriving(), arbiter=arbiter)

    sent = model.sent
    assert _ARTICLE in sent, "the article it already read was not included"
    assert "Racine County Data Center" in sent, "the arriving proposal was not named"
    assert "Fairwater" in sent and f"#{stored.id}" in sent, "the stored row was not shown"
    assert "Mount Pleasant" in sent, "the stored row's locality was not shown"


def test_the_warm_turn_names_the_arriving_row_rather_than_its_position(session, stored):
    """`build_records` drops and truncates, so an ordinal can name the wrong campus."""
    model = _WarmModel("unsure")
    arbiter = gatekeeper.same_site_arbiter(model).for_article(_context())

    upsert_record(session, _arriving(), arbiter=arbiter)

    sent = model.sent
    assert "Racine County Data Center" in sent, "the arriving row was not named"
    assert "Racine" in sent and "WI" in sent, "its locality was not given"
    for ordinal in ("second project", "project #2", "the 2nd"):
        assert ordinal not in sent.lower()


def test_the_warm_turn_offers_only_the_three_verdict_tools(session, stored):
    """No `read_article`: it has the article, and a second differently-clipped copy
    would give the quote gate two haystacks that disagree."""
    model = _WarmModel("unsure")
    arbiter = gatekeeper.same_site_arbiter(model).for_article(_context())

    upsert_record(session, _arriving(), arbiter=arbiter)

    offered = {t["function"]["name"] for t in model.seen[-1]["tools"]}
    assert offered == {"same_site", "different_site", "unsure"}


def test_the_warm_system_prompt_keeps_the_judgement_rules(session, stored):
    """The builder/landlord/occupier warning guards the irreversible direction. A
    path that quietly lost it would route three legitimate rows into one."""
    model = _WarmModel("unsure")
    arbiter = gatekeeper.same_site_arbiter(model).for_article(_context())

    upsert_record(session, _arriving(), arbiter=arbiter)

    assert gatekeeper.RULES in model.seen[-1]["system"]
    assert "read_article" not in model.seen[-1]["system"], "warm has no tools to call"


# --- the rails, on the warm path ---------------------------------------------


def test_a_warm_same_site_still_needs_a_quote_from_the_article_it_was_shown(session, stored):
    """Without this the warm path arrives with an empty haystack and refuses
    everything, while the run prints '0 duplicates prevented' — indistinguishable
    from a model being careful."""
    routed = gatekeeper.same_site_arbiter(
        _WarmModel("same_site", confidence=0.95, quote=_GOOD_QUOTE)
    ).for_article(_context())

    assert upsert_record(session, _arriving(), arbiter=routed).project_id == stored.id


def test_a_warm_same_site_whose_quote_is_invented_inserts(session, stored):
    """The other half: confident, well-reasoned, and quoting a sentence nobody
    published. The gate is what stands between that and an unmergeable row."""
    invented = gatekeeper.same_site_arbiter(
        _WarmModel("same_site", confidence=0.99, quote="they share a substation and a switchyard")
    ).for_article(_context())

    assert upsert_record(session, _arriving(), arbiter=invented).action == "insert"


def test_a_quote_from_the_omitted_middle_of_a_truncated_article_is_refused(session, stored):
    """The gate proves the model read what it ruled on, so the haystack must be the
    text it was SHOWN. Verifying against the full stored article instead would let a
    sentence from the dropped middle through and make the gate meaningless."""
    secret = "The switchyard sits on the boundary of the two parcels."
    article = (
        ("Microsoft confirmed the Racine County campus. " * 8)[:300]
        + secret
        + (" Filler about permits, timelines and hearings." * 12)
    )
    model = _WarmModel("same_site", confidence=0.99, quote=secret)
    arbiter = gatekeeper.same_site_arbiter(model).for_article(_context(article=article, limit=400))

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert secret in article, "the sentence must really be in the stored article"
    assert secret not in model.sent, "it must NOT be in the text the model was shown"
    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_a_warm_verdict_just_under_the_floor_inserts(session, stored):
    """Higher bar than a merge needs: decided from one arriving article."""
    under = gatekeeper.same_site_arbiter(
        _WarmModel("same_site", confidence=0.89, quote=_GOOD_QUOTE)
    ).for_article(_context())

    assert upsert_record(session, _arriving(), arbiter=under).action == "insert"


def test_a_warm_verdict_exactly_at_the_floor_routes(session, stored):
    """Pinned at the boundary, so the comparison cannot drift to `>`."""
    at = gatekeeper.same_site_arbiter(
        _WarmModel("same_site", confidence=gatekeeper.MIN_CONFIDENCE, quote=_GOOD_QUOTE)
    ).for_article(_context())

    assert upsert_record(session, _arriving(), arbiter=at).project_id == stored.id


def test_geography_still_outranks_a_warm_verdict(session, stored):
    """A fact check, not a menu: two sets of real coordinates that far apart are
    not one campus whatever the model concluded."""
    stored.lat, stored.lon = 42.72, -87.88
    session.flush()
    model = _WarmModel("same_site", confidence=0.99, quote=_GOOD_QUOTE)
    arbiter = gatekeeper.same_site_arbiter(model).for_article(_context())
    record = _arriving()
    record.project["lat"], record.project["lon"] = 47.60, -122.33  # Seattle

    result = upsert_record(session, record, arbiter=arbiter)

    assert result.action == "insert"
    assert session.query(Project).count() == 2


def test_the_routing_decision_is_written_into_the_row_on_the_warm_path(session, stored):
    model = _WarmModel("same_site", confidence=0.95, quote=_GOOD_QUOTE)
    arbiter = gatekeeper.same_site_arbiter(model).for_article(_context())

    upsert_record(session, _arriving(), arbiter=arbiter)

    assert "routed an arriving record" in (stored.notes or "")
    assert "agent (0.95)" in (stored.notes or "")


def test_a_multi_line_reason_is_flattened_before_it_reaches_the_notes(session, stored):
    """`record_decision` writes one line and dedupes with `if line not in lines`, so
    a multi-line reason never matches itself and the notes grow on every re-crawl."""
    args = {
        "reason": "same place.\nSecond line.\nThird line.",
        "confidence": 0.95,
        "quote": _GOOD_QUOTE,
    }
    reply = LLMReply(
        text="",
        tool_calls=(
            ToolCall(id="v", name="same_site", arguments=args, raw_arguments=json.dumps(args)),
        ),
    )
    arbiter = gatekeeper.same_site_arbiter(_WarmModel(reply=reply)).for_article(_context())

    upsert_record(session, _arriving(), arbiter=arbiter)

    written = [ln for ln in (stored.notes or "").splitlines() if "routed an arriving" in ln]
    assert len(written) == 1
    assert "Second line." in written[0], "the reason was dropped rather than flattened"


# --- failing open, and saying so ---------------------------------------------


def test_a_provider_error_on_the_warm_turn_is_reported_not_silent(session, stored):
    """An outage that returned no verdict must not read, in the run summary, like a
    model that was merely cautious."""
    from tracker.llm import LLMError

    seen: list[dict] = []
    model = _WarmModel(raises=LLMError("provider unavailable"))
    arbiter = gatekeeper.same_site_arbiter(model, on_decision=seen.append).for_article(_context())

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.action == "insert"
    assert len(seen) == 1, "the tally would silently read zero"
    assert seen[0]["routed"] is False
    assert "provider unavailable" in seen[0]["note"]


def test_a_warm_turn_that_answers_in_prose_inserts(session, stored):
    """The extraction step is told to emit JSON and nothing else, so a reply that
    ignores the tools is a real possibility rather than a theoretical one."""
    model = _WarmModel(reply=LLMReply(text='{"projects": []}'))
    arbiter = gatekeeper.same_site_arbiter(model).for_article(_context())

    assert upsert_record(session, _arriving(), arbiter=arbiter).action == "insert"


def test_a_warm_turn_that_calls_a_tool_nobody_offered_inserts(session, stored):
    args = {"url": _URL}
    model = _WarmModel(
        reply=LLMReply(
            text="",
            tool_calls=(ToolCall(id="x", name="read_article", arguments=args, raw_arguments="{}"),),
        )
    )
    arbiter = gatekeeper.same_site_arbiter(model).for_article(_context())

    assert upsert_record(session, _arriving(), arbiter=arbiter).action == "insert"


def test_unparseable_verdict_arguments_insert(session, stored):
    model = _WarmModel(
        reply=LLMReply(
            text="",
            tool_calls=(
                ToolCall(
                    id="v", name="same_site", arguments={}, raw_arguments="{oops", parse_failed=True
                ),
            ),
        )
    )
    arbiter = gatekeeper.same_site_arbiter(model).for_article(_context())

    assert upsert_record(session, _arriving(), arbiter=arbiter).action == "insert"


def test_a_provider_without_converse_falls_back_to_the_cold_path(session, stored, cached):
    """Ollama has no multi-turn call. Falling back beats failing."""

    class _Old:
        def complete(self, *, system, user, max_tokens=None):
            return LLMReply(text="{}")

    arbiter = gatekeeper.same_site_arbiter(_Old()).for_article(_context())

    assert upsert_record(session, _arriving(), arbiter=arbiter).action == "insert"


def test_a_context_from_another_article_is_not_used(session, stored, cached):
    """One arbiter serves a whole run. Judging this record against the previous
    article's text would be wrong in a way nothing downstream could detect."""
    model = _Model("same_site", confidence=0.95, quote=_GOOD_QUOTE)
    arbiter = gatekeeper.same_site_arbiter(model).for_article(
        _context(url="https://example.test/some-other-article", article="A different campus.")
    )

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    # It fell back to cold, which is proved by the model being asked to fetch.
    assert model.turn > 1, "the stale context was used instead of the cold path"
    assert result.project_id == stored.id


# --- the tally the CLI reads --------------------------------------------------


def test_on_decision_carries_every_key_the_cli_reads(session, stored):
    """`cli._identity_arbiter` subscripts these directly; a missing one raises
    inside the arbiter, where `upsert` would swallow it."""
    seen: list[dict] = []
    arbiter = gatekeeper.same_site_arbiter(
        _WarmModel("same_site", confidence=0.95, quote=_GOOD_QUOTE), on_decision=seen.append
    ).for_article(_context())

    upsert_record(session, _arriving(), arbiter=arbiter)

    assert len(seen) == 1
    assert {
        "key",
        "candidate_id",
        "outcome",
        "steps",
        "prompt_tokens",
        "completion_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "routed",
        "note",
        "via",
    } <= set(seen[0])
    assert seen[0]["routed"] is True
    assert seen[0]["via"] == "warm"


def test_a_failing_on_decision_never_changes_the_outcome(session, stored):
    """The tally is telemetry. A broken callback must not cost a prevented
    duplicate, nor leave a note claiming a route that did not happen."""

    def _boom(_decision):
        raise RuntimeError("tally on fire")

    arbiter = gatekeeper.same_site_arbiter(
        _WarmModel("same_site", confidence=0.95, quote=_GOOD_QUOTE), on_decision=_boom
    ).for_article(_context())

    result = upsert_record(session, _arriving(), arbiter=arbiter)

    assert result.project_id == stored.id, "telemetry broke the decision"
    assert session.query(Project).count() == 1
    assert "routed an arriving record" in (stored.notes or "")


# --- and it must not be consulted when there is nothing to ask ----------------


def test_a_warm_context_alone_does_not_consult_the_model(session):
    """No stored row means no candidate, so there is no question. Guards against
    hoisting the call out of the `candidate is not None` branch to 'reuse' a
    conversation that is already paid for."""

    class _Never:
        def converse(self, **_kwargs):
            raise AssertionError("should not have called the model")

    arbiter = gatekeeper.same_site_arbiter(_Never()).for_article(_context())

    assert upsert_record(session, _arriving(), arbiter=arbiter).action == "insert"


def test_an_exact_key_match_never_reaches_the_warm_model(session, stored):
    class _Never:
        def converse(self, **_kwargs):
            raise AssertionError("should not have called the model")

    arbiter = gatekeeper.same_site_arbiter(_Never()).for_article(_context())
    record = _arriving()
    record.project["city"] = "Mount Pleasant"
    record.project.pop("county", None)

    assert upsert_record(session, record, arbiter=arbiter).project_id == stored.id


def test_existing_only_refuses_before_the_model_is_consulted(session, stored):
    class _Never:
        def converse(self, **_kwargs):
            raise AssertionError("should not have called the model")

    arbiter = gatekeeper.same_site_arbiter(_Never()).for_article(_context())
    record = _arriving()
    record.project["company"] = "Somebody Else"

    assert upsert_record(session, record, arbiter=arbiter, existing_only=True).action == "refused"


def test_force_new_never_consults_the_model(session, stored):
    class _Never:
        def converse(self, **_kwargs):
            raise AssertionError("should not have called the model")

    arbiter = gatekeeper.same_site_arbiter(_Never()).for_article(_context())

    assert upsert_record(session, _arriving(), arbiter=arbiter, force_new=True).action == "insert"


def test_a_record_with_no_citation_is_not_arbitrated_even_with_a_warm_context(session, stored):
    class _Never:
        def converse(self, **_kwargs):
            raise AssertionError("should not have called the model")

    arbiter = gatekeeper.same_site_arbiter(_Never()).for_article(_context())
    record = _arriving()
    record.sources.clear()

    assert upsert_record(session, record, arbiter=arbiter).action == "insert"
