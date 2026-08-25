"""Chasing the operators we have no rows for. Offline throughout.

The load-bearing assertion is
:func:`test_a_campus_the_model_invented_never_reaches_the_database`, the sibling of
the same test in `test_search.py`. Letting a model name an operator's campuses is
only safe because those names are query material and nothing else; if that ever
stops being true, this file should fail.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from tracker import prospect, roster
from tracker.ingest.discover import Candidate, load_config
from tracker.ingest.enrich import ArchiveSweep
from tracker.ingest.search import QuotaExhausted, SearchError, SearchHit
from tracker.llm import LLMError, LLMReply
from tracker.models import IngestUrl, Project

NEBIUS = roster.Operator(name="Nebius", kind="neocloud", aliases=("Nebius Group",))


class FakeProvider:
    def __init__(self, mapping: dict[str, list[SearchHit]] | None = None, *, fail=()):
        self.mapping = mapping or {}
        self.fail = set(fail)
        self.calls: list[str] = []

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        self.calls.append(query)
        if query in self.fail:
            raise SearchError("boom")
        return self.mapping.get(query, [])


class AnyQueryProvider:
    """Answers every query with the same hit, whatever it was asked."""

    def __init__(self, hits: list[SearchHit]):
        self.hits = hits
        self.calls: list[str] = []

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        self.calls.append(query)
        return self.hits


class QuotaProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        self.calls.append(query)
        raise QuotaExhausted("out of quota")


class FakeLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.seen: list[str] = []

    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply:
        self.seen.append(user)
        return LLMReply(self.replies.pop(0), "stop", 10, 20, "fake-model")


class BoomLLM:
    def complete(self, **_: object) -> LLMReply:
        raise LLMError("provider down")


def hit(url: str, title: str, snippet: str = "", query: str = "q") -> SearchHit:
    return SearchHit(url=url, title=title, snippet=snippet, query=query)


def presence(operator: roster.Operator, projects: int = 0) -> roster.Presence:
    return roster.Presence(
        operator=operator, projects=projects, with_capacity=0, mw_planned=0.0, states=()
    )


@pytest.fixture
def spec():
    return load_config()[1]


# --- Templated queries ------------------------------------------------------


def test_templated_queries_quote_the_name_and_anchor_one_to_the_us():
    leads = prospect.queries_for(NEBIUS)
    assert len(leads) == 4
    assert all(lead.origin == "template" for lead in leads)
    assert all('"Nebius"' in lead.query for lead in leads), (
        "an unquoted operator name invites a search engine to substitute a synonym"
    )
    assert any("United States" in lead.query for lead in leads)


def test_templated_queries_need_no_model():
    """The whole point: an operator name is already the specific thing wanted."""
    assert prospect.queries_for(NEBIUS) == prospect.queries_for(NEBIUS)


# --- Model-proposed campuses ------------------------------------------------


def test_campuses_are_parsed_and_deduplicated():
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "campuses": [
                        {"name": "Kansas City", "city": "Kansas City", "state": "mo"},
                        {"name": "Kansas City", "city": "Kansas City", "state": "MO"},
                        {"name": "Vineland", "city": "", "state": ""},
                        {"name": "  "},
                    ]
                }
            )
        ]
    )
    campuses = prospect.propose_campuses(llm, NEBIUS)
    assert [c.name for c in campuses] == ["Kansas City", "Vineland"]
    assert campuses[0].state == "MO", "a lowercase state is normalized, not rejected"
    assert campuses[0].label == "Kansas City (Kansas City, MO)"
    assert campuses[1].label == "Vineland"


def test_an_empty_campus_list_is_a_legitimate_answer():
    llm = FakeLLM([json.dumps({"campuses": []})])
    assert prospect.propose_campuses(llm, NEBIUS) == []


def test_the_prompt_carries_the_operator_and_its_kind():
    llm = FakeLLM([json.dumps({"campuses": []})])
    prospect.propose_campuses(llm, NEBIUS)
    assert "Nebius" in llm.seen[0]
    assert "neocloud" in llm.seen[0]


def test_a_model_that_will_not_answer_is_an_error_not_a_silent_zero():
    with pytest.raises(prospect.ProspectError, match="Nebius"):
        prospect.propose_campuses(BoomLLM(), NEBIUS)


def test_unparseable_json_is_an_error():
    with pytest.raises(prospect.ProspectError, match="campus list"):
        prospect.propose_campuses(FakeLLM(["not json at all"]), NEBIUS)


def test_campus_queries_keep_the_operator_name_in_them():
    """A campus name alone is not unique: two industries use "Hyperion"."""
    leads = prospect.campus_queries(NEBIUS, [prospect.Campus("Hyperion", "Kansas City", "MO")])
    assert leads[0].origin == "model"
    assert '"Nebius"' in leads[0].query and '"Hyperion"' in leads[0].query
    assert "Kansas City MO" in leads[0].query


# --- The key-free half ------------------------------------------------------


def _sweep(*urls: str) -> ArchiveSweep:
    return ArchiveSweep(candidates=[Candidate(url=u, title="", feed="s") for u in urls])


def test_archive_leads_match_the_operator_in_a_slug():
    sweep = _sweep(
        "https://dc.test/2025/nebius-kansas-city-data-center/",
        "https://dc.test/2025/meta-hyperion-louisiana/",
    )
    assert [c.url for c in prospect.archive_leads(sweep, NEBIUS)] == [
        "https://dc.test/2025/nebius-kansas-city-data-center/"
    ]


def test_archive_leads_require_every_word_of_a_multiword_name():
    """ "Core Scientific" must not collect every CoreWeave article."""
    operator = roster.Operator(name="Core Scientific", kind="neocloud")
    sweep = _sweep(
        "https://dc.test/coreweave-denton-texas/",
        "https://dc.test/core-scientific-denton-texas/",
    )
    assert [c.url for c in prospect.archive_leads(sweep, operator)] == [
        "https://dc.test/core-scientific-denton-texas/"
    ]


def test_an_operator_named_only_by_generic_words_matches_nothing():
    operator = roster.Operator(name="Data Centers", kind="landlord")
    assert prospect.archive_leads(_sweep("https://dc.test/any-data-center/"), operator) == []


def test_the_headline_counts_too_not_only_the_url():
    sweep = ArchiveSweep(
        candidates=[
            Candidate(url="https://dc.test/2025/03/kc-expansion/", title="Nebius expands", feed="s")
        ]
    )
    assert len(prospect.archive_leads(sweep, NEBIUS)) == 1


# --- Running ----------------------------------------------------------------


def test_queued_candidates_are_attributed_to_the_operator(session):
    provider = AnyQueryProvider(
        [hit("https://dc.test/nebius-kc", "Nebius plans a 300 MW data center campus in Missouri")]
    )
    report = prospect.run(session, [presence(NEBIUS)], provider=provider, per_operator=1)

    assert report.queued == 1
    row = session.scalars(select(IngestUrl)).one()
    assert row.feed == "prospect:Nebius", (
        "a queued row has to say which operator we went looking for"
    )
    assert row.url == "https://dc.test/nebius-kc"


def test_archive_and_search_leads_are_both_queued(session):
    provider = AnyQueryProvider(
        [hit("https://dc.test/from-search", "Nebius data center campus 300 MW announced")]
    )
    sweep = _sweep("https://dc.test/nebius-data-center-campus-megawatts/")
    report = prospect.run(
        session, [presence(NEBIUS)], provider=provider, sweep=sweep, per_operator=1
    )
    outcome = report.outcomes[0]
    assert outcome.archive_hits == 1
    assert len(outcome.queued) == 2


def test_a_run_with_no_provider_still_uses_the_archives(session):
    """Prospecting without a search key is a configuration, not a failure."""
    sweep = _sweep("https://dc.test/nebius-data-center-campus-megawatts/")
    report = prospect.run(session, [presence(NEBIUS)], provider=None, sweep=sweep)
    assert report.queries_run == 0
    assert report.queued == 1


def test_the_model_is_only_asked_when_one_is_supplied(session):
    provider = FakeProvider()
    prospect.run(session, [presence(NEBIUS)], provider=provider, extractor=None)
    assert len(provider.calls) == 4, "templates only, no campus queries"

    llm = FakeLLM([json.dumps({"campuses": [{"name": "Kansas City", "state": "MO"}]})])
    provider2 = FakeProvider()
    prospect.run(session, [presence(NEBIUS)], provider=provider2, extractor=llm)
    assert len(provider2.calls) == 5, "four templates plus one campus"


def test_a_failed_brainstorm_does_not_end_the_run(session):
    """The templated half needs no model, so it still runs."""
    provider = FakeProvider()
    report = prospect.run(session, [presence(NEBIUS)], provider=provider, extractor=BoomLLM())
    assert len(provider.calls) == 4
    assert report.errors and report.errors[0][0] == "Nebius"


def test_per_operator_caps_the_queries(session):
    provider = FakeProvider()
    prospect.run(session, [presence(NEBIUS)], provider=provider, per_operator=2)
    assert len(provider.calls) == 2


def test_an_exhausted_quota_stops_before_the_next_operator(session):
    provider = QuotaProvider()
    targets = [presence(NEBIUS), presence(roster.Operator(name="Vultr", kind="neocloud"))]
    report = prospect.run(session, targets, provider=provider)
    assert report.quota_exhausted
    assert len(provider.calls) == 1, "every further query would fail the same way"
    assert len(report.outcomes) == 1


def test_dry_run_writes_nothing(session, engine):
    provider = AnyQueryProvider(
        [hit("https://dc.test/nebius-kc", "Nebius data center campus 300 MW in Missouri")]
    )
    report = prospect.run(session, [presence(NEBIUS)], provider=provider, dry_run=True)
    assert report.queued == 1, "the report still says what a real run would have queued"
    assert list(session.scalars(select(IngestUrl))) == []


def test_a_campus_the_model_invented_never_reaches_the_database(session):
    """The load-bearing property, and the reason a model may speculate here.

    It names a site, the search finds nothing, and the run ends having written no
    project, no source, and not even a queued URL.
    """
    llm = FakeLLM([json.dumps({"campuses": [{"name": "Atlantis Compute Park", "state": "NV"}]})])
    provider = FakeProvider({})  # the invented campus has no coverage
    report = prospect.run(session, [presence(NEBIUS)], provider=provider, extractor=llm)

    assert report.outcomes[0].campuses[0].name == "Atlantis Compute Park"
    assert report.queued == 0
    assert list(session.scalars(select(Project))) == [], "no project from an unverified name"
    assert list(session.scalars(select(IngestUrl))) == [], "not even a queued URL"


def test_prospect_only_ever_produces_queued_urls_never_projects(session):
    provider = AnyQueryProvider(
        [hit("https://dc.test/nebius-kc", "Nebius data center campus 300 MW in Missouri")]
    )
    prospect.run(session, [presence(NEBIUS)], provider=provider)
    assert list(session.scalars(select(Project))) == [], "only the crawl path may write a project"


def test_an_off_topic_hit_is_filtered_out(session):
    provider = AnyQueryProvider([hit("https://dc.test/quarterly-earnings", "Nebius Q3 revenue")])
    report = prospect.run(session, [presence(NEBIUS)], provider=provider, per_operator=1)
    assert report.outcomes[0].hits == 1
    assert report.queued == 0, "the same keyword filter feed discovery uses"


def test_queued_urls_keep_the_roster_order(session):
    first = roster.Operator(name="Nebius", kind="neocloud")
    second = roster.Operator(name="Vultr", kind="neocloud")
    sweep = ArchiveSweep(
        candidates=[
            Candidate(url="https://dc.test/vultr-data-center-campus/", title="", feed="s"),
            Candidate(url="https://dc.test/nebius-data-center-campus/", title="", feed="s"),
        ]
    )
    report = prospect.run(session, [presence(first), presence(second)], sweep=sweep)
    assert report.queued_urls == [
        "https://dc.test/nebius-data-center-campus/",
        "https://dc.test/vultr-data-center-campus/",
    ], "the caller crawls a prefix of this, so the roster's priority has to survive"


# --- The cheapest source: what we already queued and never read --------------


def _queued(session, url: str, title: str = "", status: str = "discovered"):
    from tracker.models import IngestUrl

    row = IngestUrl(url=url, run_id="test", status=status, title=title)
    session.add(row)
    session.flush()
    return row


def test_queue_leads_find_an_unread_candidate_naming_the_operator(session):
    """The failure this closes: a queued Nebius URL and no Nebius row.

    The extract phase is depth-first by design, so an article about an operator with
    no rows matches no known project and waits behind better candidates forever.
    """
    _queued(session, "https://dc.test/nebius-kansas-city/", "Nebius picks Missouri")
    _queued(session, "https://dc.test/meta-hyperion/", "Meta expands in Louisiana")

    assert prospect.queue_leads(session, NEBIUS) == ["https://dc.test/nebius-kansas-city/"]


def test_queue_leads_include_what_previously_failed(session):
    """Same reason `sync --retry-failed` exists: a 403 once is not a 403 forever."""
    _queued(session, "https://dc.test/nebius-one/", status="fetch_error")
    _queued(session, "https://dc.test/nebius-two/", status="parse_error")

    assert len(prospect.queue_leads(session, NEBIUS)) == 2
    assert prospect.queue_leads(session, NEBIUS, include_failed=False) == []


def test_queue_leads_ignore_urls_already_read(session):
    """An `ok` row has been extracted; re-reading it is `refresh`'s job, not this."""
    _queued(session, "https://dc.test/nebius-done/", status="ok")
    assert prospect.queue_leads(session, NEBIUS) == []


def test_the_queue_is_read_even_with_no_provider_and_no_archives(session):
    _queued(session, "https://dc.test/nebius-kansas-city/", "Nebius picks Missouri")
    report = prospect.run(session, [presence(NEBIUS)], provider=None, sweep=None)
    outcome = report.outcomes[0]
    assert outcome.from_queue == ["https://dc.test/nebius-kansas-city/"]
    assert report.from_queue == 1
    assert report.queued == 0, "it was already queued; what it lacked was a turn"
    assert report.queued_urls == ["https://dc.test/nebius-kansas-city/"]


def test_the_already_queued_are_read_before_the_newly_found(session):
    """Cheapest first, and the caller crawls a prefix of this list."""
    _queued(session, "https://dc.test/nebius-old/", "Nebius data center campus")
    provider = AnyQueryProvider(
        [hit("https://dc.test/nebius-new/", "Nebius data center campus 300 MW in Missouri")]
    )
    report = prospect.run(session, [presence(NEBIUS)], provider=provider, per_operator=1)
    assert report.queued_urls == ["https://dc.test/nebius-old/", "https://dc.test/nebius-new/"]


def test_an_operator_with_only_queue_leads_says_so_rather_than_nothing_found(session):
    _queued(session, "https://dc.test/nebius-old/", "Nebius data center campus")
    report = prospect.run(session, [presence(NEBIUS)], provider=None, sweep=None)
    assert report.outcomes[0].note == "1 already queued"
