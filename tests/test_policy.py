"""The source policy: what it matches, and what it refuses to decide.

Two tests here are the load-bearing ones.

`test_x_com_does_not_block_equinix` is a regression against a bug this codebase has
already had once — `search.py` records that a substring host test made ``x.com``
block every ``equinix.com`` URL. Any future edit that reaches for ``in`` instead of
a label-boundary test fails here.

`test_the_policy_key_agrees_with_what_the_report_prints` is the one that stops the
whole feature being quietly useless. There are five different URL→host
normalisations in this codebase; `tracker sources` prints one of them and the queue
reasons in another. If the policy ever keys on anything but the one the report
prints, an operator copying a row out of the ranking writes a rule that never fires
and nothing tells them.
"""

from __future__ import annotations

import pytest

from tracker import policy, sources

# --- matching ----------------------------------------------------------------


def rule(domain: str, rank: str = policy.IGNORE) -> policy.Policy:
    return policy.Policy(entries=(policy.Entry(domain=domain, rank=rank),))


def test_x_com_does_not_block_equinix():
    """The documented regression. `search.py:114` explains what it cost last time."""
    p = rule("x.com")
    assert p.decide("https://x.com/user/status/1") == policy.IGNORE
    assert p.decide("https://mobile.x.com/user") == policy.IGNORE
    assert p.decide("https://www.equinix.com/newsroom/press-releases/a") is None


def test_a_subdomain_is_covered_by_its_registrable_domain():
    p = rule("example.com", policy.PRIORITY)
    assert p.decide("https://news.example.com/a") == policy.PRIORITY
    assert p.decide("https://example.com/a") == policy.PRIORITY
    assert p.decide("https://notexample.com/a") is None


def test_a_compound_suffix_is_one_publisher():
    p = rule("bbc.co.uk")
    assert p.decide("https://news.bbc.co.uk/story") == policy.IGNORE
    assert p.decide("https://guardian.co.uk/story") is None


def test_the_policy_key_agrees_with_what_the_report_prints():
    """The anti-sixth-normalisation test.

    `tracker sources` prints `sources.host_of`. A rule copied from that table has
    to fire. If these two ever disagree the file becomes decoration.
    """
    for url in (
        "https://www.datacenterfrontier.com/hyperscale/article/1",
        "http://news.example.co.uk/a?b=c",
        "https://SEC.gov/Archives/edgar/data/1/x.htm",
        "https://user@host.example.com:8443/path",
    ):
        assert policy.registrable_domain(url) == sources.host_of(url)


def test_no_opinion_is_the_default():
    assert policy.EMPTY.decide("https://anything.test/a") is None
    assert policy.EMPTY.decide("") is None


# --- ordering ----------------------------------------------------------------


def test_priority_sorts_first_and_ignored_drops_out():
    p = policy.Policy(
        entries=(
            policy.Entry("good.test", policy.PRIORITY),
            policy.Entry("bad.test", policy.IGNORE),
        )
    )
    kept, dropped = p.partition(
        ["https://plain.test/1", "https://bad.test/2", "https://good.test/3"]
    )
    assert kept == ["https://good.test/3", "https://plain.test/1"]
    assert dropped == ["https://bad.test/2"]


def test_order_within_a_band_is_preserved():
    """The queue has already sorted by publication date and by depth. Policy
    reorders *between* bands and must not shuffle within one."""
    p = rule("first.test", policy.PRIORITY)
    urls = [f"https://plain.test/{i}" for i in range(5)]
    kept, _ = p.partition(urls)
    assert kept == urls


# --- the file ----------------------------------------------------------------


def test_a_missing_file_is_an_empty_policy_not_an_error(tmp_path):
    assert policy.load(tmp_path / "nope.toml") == policy.EMPTY


def test_malformed_toml_degrades_rather_than_stopping_a_crawl(tmp_path, caplog):
    """A broken policy means no opinion, not a dead run — the posture `sync`
    already takes with a broken feed config."""
    path = tmp_path / "sources.toml"
    path.write_text("this is not toml {{{", encoding="utf-8")
    assert policy.load(path) == policy.EMPTY
    assert "ignoring the source policy" in caplog.text


def test_an_unknown_rank_is_refused_loudly():
    with pytest.raises(policy.PolicyError, match="expected one of"):
        policy.parse('[[source]]\ndomain = "a.test"\nrank = "maybe"\n')


def test_a_domain_cannot_carry_two_ranks():
    with pytest.raises(policy.PolicyError, match="twice"):
        policy.parse(
            '[[source]]\ndomain = "a.test"\nrank = "ignore"\n'
            '[[source]]\ndomain = "a.test"\nrank = "priority"\n'
        )


def test_a_hand_written_key_is_normalised_on_load():
    p = policy.parse('[[source]]\ndomain = "WWW.Example.COM"\nrank = "ignore"\n')
    assert p.entries[0].domain == "example.com"


def test_round_trips_and_writes_lf(tmp_path):
    """CRLF would make the next diff look like a full rewrite of a file whose
    whole purpose is being reviewable."""
    p = policy.Policy(
        entries=(
            policy.Entry("b.test", policy.IGNORE, "nothing"),
            policy.Entry("a.test", policy.PRIORITY, "lots"),
        )
    )
    path = policy.write(p, tmp_path / "sources.toml")
    assert b"\r\n" not in path.read_bytes()
    assert policy.load(path).by_domain.keys() == {"a.test", "b.test"}


def test_writing_twice_produces_identical_bytes(tmp_path):
    p = policy.Policy(entries=(policy.Entry("a.test", policy.PRIORITY, "why"),))
    first = policy.write(p, tmp_path / "s.toml").read_bytes()
    assert policy.write(p, tmp_path / "s.toml").read_bytes() == first


# --- the analysis ------------------------------------------------------------


class FakeHost:
    def __init__(self, host, cited, decisive, contested=0):
        self.host, self.cited, self.decisive, self.contested = host, cited, decisive, contested

    @property
    def yield_per_citation(self):
        return self.decisive / self.cited if self.cited else 0.0


class FakeSurvey:
    MIN_CITED_FOR_RATIO = 5

    def __init__(self, hosts, decisions=None, sources_read=None):
        self.hosts = hosts
        self.sources_read = sources_read or sum(h.cited for h in hosts)
        self.decisions = decisions if decisions is not None else sum(h.decisive for h in hosts)


def test_it_refuses_to_judge_a_publisher_cited_twice():
    a = policy.analyse(FakeSurvey([FakeHost("thin.test", 2, 2, 2)]), policy.EMPTY)
    assert a.proposals == []
    assert a.by_class()["too few to judge"][0].domain == "thin.test"


def test_ignore_fires_on_zero_not_on_thin():
    """`funnel.LOW_YIELD` is documented there as reported and never proposed.
    A thin publisher is a prompt to look; a publisher that has never once backed a
    stored value is a proposal."""
    hosts = [FakeHost("zero.test", 12, 0), FakeHost("thin.test", 12, 1, 1)]
    a = policy.analyse(FakeSurvey(hosts, decisions=40, sources_read=60), policy.EMPTY)
    assert [(p.domain, p.rank) for p in a.proposals] == [("zero.test", policy.IGNORE)]
    assert a.by_class()["thin"][0].domain == "thin.test"


def test_a_blocked_publisher_is_never_proposed_for_ignore():
    """The `datacenterdynamics` mistake, one level down. Few citations because we
    cannot fetch it is not the same as few citations because it is worthless."""
    survey = FakeSurvey([FakeHost("blocked.test", 12, 0)], decisions=40, sources_read=60)
    a = policy.analyse(survey, policy.EMPTY, unread_hosts=[("news.blocked.test", 30)])
    assert a.proposals == []
    assert a.by_class()["cannot read"][0].domain == "blocked.test"


def test_a_configured_feed_is_retired_in_feeds_toml_not_here():
    survey = FakeSurvey([FakeHost("feed.test", 12, 0)], decisions=40, sources_read=60)
    a = policy.analyse(survey, policy.EMPTY, feed_hosts=frozenset({"feed.test"}))
    assert a.proposals == []
    assert a.by_class()["still a feed"][0].domain == "feed.test"


def test_an_operators_own_newsroom_is_never_ignored():
    survey = FakeSurvey([FakeHost("operator.test", 12, 0)], decisions=40, sources_read=60)
    a = policy.analyse(survey, policy.EMPTY, newsroom_hosts=frozenset({"operator.test"}))
    assert a.proposals == []
    assert a.by_class()["own newsroom"][0].domain == "operator.test"


def test_priority_needs_a_win_against_a_disagreeing_rival():
    """Unopposed wins are cheap: a host cited on a single-source project wins every
    field with nothing to beat."""
    survey = FakeSurvey([FakeHost("unopposed.test", 20, 20, 0)], decisions=20, sources_read=40)
    assert policy.analyse(survey, policy.EMPTY).proposals == []


def test_priority_is_measured_against_the_fleet_not_a_constant():
    """Par is the fleet's own decisions-per-citation. A fixed bar promoted 75 of 94
    judgeable publishers on the live database, which is not an ordering."""
    hosts = [FakeHost("great.test", 20, 18, 6), FakeHost("ordinary.test", 20, 4, 2)]
    a = policy.analyse(FakeSurvey(hosts, decisions=100, sources_read=200), policy.EMPTY)
    assert [(p.domain, p.rank) for p in a.proposals] == [("great.test", policy.PRIORITY)]


def test_a_hand_edited_reason_survives_a_re_run():
    """The measured ignore list contains publishers an operator would veto. Having
    vetoed one, they must not have to veto it again after every run."""
    existing = policy.Policy(
        entries=(policy.Entry("great.test", policy.IGNORE, "we do not trust them"),)
    )
    hosts = [FakeHost("great.test", 20, 18, 6)]
    a = policy.analyse(FakeSurvey(hosts, decisions=100, sources_read=200), existing)
    kept = a.proposals[0]
    assert kept.rank == policy.IGNORE
    assert kept.why == "we do not trust them"
    assert a.stale, "it should say the entry no longer matches the evidence"


def test_an_entry_about_an_unseen_publisher_is_carried_through():
    """Applying must never delete a decision somebody made deliberately."""
    existing = policy.Policy(entries=(policy.Entry("gone.test", policy.IGNORE, "by hand"),))
    a = policy.analyse(FakeSurvey([]), existing)
    assert [p.domain for p in a.proposals] == ["gone.test"]


def test_the_analysis_is_idempotent():
    hosts = [FakeHost("great.test", 20, 18, 6), FakeHost("zero.test", 12, 0)]
    survey = FakeSurvey(hosts, decisions=100, sources_read=200)
    once = policy.to_policy(policy.analyse(survey, policy.EMPTY).proposals)
    twice = policy.to_policy(policy.analyse(survey, once).proposals)
    assert policy.render(once) == policy.render(twice)
