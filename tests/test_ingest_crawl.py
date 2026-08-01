"""Crawl ingest, entirely offline.

No network and no LLM: `FakeFetcher` and `FakeLLM` are injected through `run()`'s
own keyword parameters, so nothing is monkeypatched and no API key is needed.
A fresh clone must be able to run this.

The two assertions that matter most:

* :func:`test_extracts_at_least_nine_of_twelve_fields` — the PRD's definition of
  done, as a test rather than a hope.
* :func:`test_excerpt_is_a_real_substring_of_the_article` — proves the stored
  citation was *quoted* from the article rather than generated to look like one.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from tracker.ingest import crawl
from tracker.ingest.fetch import FetchResult, html_to_text, should_escalate
from tracker.llm import LLMError, LLMJsonError, LLMReply, parse_json_object
from tracker.models import IngestUrl, Project, Source
from tracker.prompts import load_prompt
from tracker.vocab import TRACKED_FIELDS

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://www.datacenterdynamics.com/en/news/microsoft-fairwater-mount-pleasant/"
NOW = dt.datetime(2026, 2, 1, 12, 0, 0)


def article() -> str:
    return (FIXTURES / "article_microsoft_wi.md").read_text(encoding="utf-8")


def canned(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fetched(url: str = URL, *, markdown: str | None = None, **kwargs) -> FetchResult:
    return FetchResult(
        url=url,
        ok=True,
        markdown=article() if markdown is None else markdown,
        status=200,
        fetched_at=NOW,
        via="httpx",
        **kwargs,
    )


class FakeFetcher:
    """Returns canned FetchResults. Records what it was asked for."""

    def __init__(self, mapping: dict[str, FetchResult]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        return self.mapping.get(
            url, FetchResult(url, False, error="not in fixture", fetched_at=NOW)
        )


class FakeLLM:
    """Returns canned replies in order. Records the prompts it received."""

    def __init__(self, replies: list[str], *, finish_reason: str = "stop") -> None:
        self.replies = list(replies)
        self.finish_reason = finish_reason
        self.seen: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply:
        self.seen.append((system, user))
        text = self.replies.pop(0) if self.replies else "{}"
        return LLMReply(text, self.finish_reason, 1200, 400, "fake-model")


class BoomLLM:
    def complete(self, **_: object) -> LLMReply:
        raise LLMError("provider exploded")


@pytest.fixture
def prompt():
    return load_prompt("extract-v1")


# --- JSON recovery ----------------------------------------------------------


def test_parses_a_fenced_reply_with_prose_around_it():
    """MiniMax ignores response_format, so replies arrive wrapped."""
    payload = parse_json_object(canned("llm_response_microsoft_wi.json"))
    assert payload["projects"][0]["name"] == "Fairwater"


@pytest.mark.parametrize(
    "text",
    [
        '{"projects": []}',
        '```json\n{"projects": []}\n```',
        'Sure!\n```JSON\n{"projects": []}\n```\nLet me know.',
        '<think>Let me consider...</think>{"projects": []}',
        '{"projects": [],}',
        "{“projects”: []}",
    ],
)
def test_json_recovery_handles_real_malformations(text):
    assert parse_json_object(text) == {"projects": []}


def test_repairs_the_missing_brace_inside_an_array():
    """A reported M2.5 failure: the `{` of an object inside an array is dropped."""
    payload = parse_json_object(canned("llm_response_malformed.json"))
    assert payload["projects"][0]["name"] == "Fairwater"


def test_unrecoverable_reply_raises():
    with pytest.raises(LLMJsonError):
        parse_json_object("I'm afraid I can't help with that.")


def test_braces_inside_quoted_strings_do_not_confuse_the_scanner():
    payload = parse_json_object('{"a": "a } brace", "b": "{ another"}')
    assert payload == {"a": "a } brace", "b": "{ another"}


# --- Source type classification ---------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://news.microsoft.com/x", "company_filing"),
        ("https://www.sec.gov/Archives/x", "company_filing"),
        ("https://ir.example.com/x", "company_filing"),
        ("https://dnr.wi.gov/permit", "government_doc"),
        ("https://www.datacenterdynamics.com/en/news/x", "trade_press"),
        ("https://www.utilitydive.com/news/x", "trade_press"),
        ("https://www.example-news.com/x", "general_media"),
    ],
)
def test_classify_source_type(url, expected):
    assert crawl.classify_source_type(url) == expected


def test_an_unknown_domain_is_never_treated_as_official():
    """Only the official weights can lift a project to confidence 2."""
    assert crawl.classify_source_type("https://random-blog.example/post") == "general_media"


# --- The evidence gate ------------------------------------------------------


def test_evidence_gate_keeps_quoted_values():
    kept, quotes, dropped = crawl.evidence_gate(
        {"mw_planned": 900.0},
        [{"field": "mw_planned", "quote": "will draw 900\nmegawatts at full buildout"}],
        article(),
    )
    assert kept == {"mw_planned": 900.0}
    assert "mw_planned" in quotes
    assert dropped == []


def test_evidence_gate_drops_values_with_no_quote():
    kept, _, dropped = crawl.evidence_gate({"mw_planned": 900.0}, [], article())
    assert kept == {}
    assert dropped == ["mw_planned"]


def test_evidence_gate_drops_a_quote_that_is_not_in_the_article(caplog):
    """The load-bearing check.

    Requiring *a* quote stops the model omitting citations. Requiring the quote to
    really appear in the fetched text stops it paraphrasing the article into a
    citation that sounds right but was never written -- which is exactly how a
    fabricated number acquires a source.
    """
    kept, quotes, dropped = crawl.evidence_gate(
        {"mw_planned": 4200.0},
        [{"field": "mw_planned", "quote": "The campus will draw 4,200 megawatts, sources said."}],
        article(),
    )
    assert kept == {}
    assert quotes == {}
    assert dropped == ["mw_planned"]
    assert "not in the article" in caplog.text


def test_evidence_gate_tolerates_whitespace_and_quote_style():
    """A quote reflowed by the model must still match.

    The article wraps "900\\nmegawatts" across a line; the model returns it as one
    line with the spacing it feels like. Both the substring check against the
    article and the value match have to survive that.
    """
    kept, _, _ = crawl.evidence_gate(
        {"mw_planned": 900.0},
        [
            {
                "field": "mw_planned",
                "quote": "the company says will draw 900   megawatts at full buildout",
            }
        ],
        article(),
    )
    assert kept == {"mw_planned": 900.0}


def test_evidence_gate_accepts_a_value_stated_under_another_field_label():
    """The regression this whole mechanism exists to prevent.

    Live case, T5@Augusta: the model returned `mw_planned: 200`, supplied the
    verbatim sentence "...a 140-acre, 200 megawatt campus...", and filed it under
    `name`. Label-matching threw the value away. Across the first 90 projects that
    bookkeeping requirement discarded 89 correctly-evidenced values, 60 of them
    `phase` -- which, being NOT NULL, silently became the `announced` default.
    """
    quote = "T5 Data Centers plans to build a 140-acre, 200 megawatt campus in Georgia"
    kept, quotes, dropped = crawl.evidence_gate(
        {"mw_planned": 200.0},
        [{"field": "name", "quote": quote}],
        f"Some preamble. {quote}. Some trailing text.",
    )
    assert kept == {"mw_planned": 200.0}
    assert dropped == []
    assert quotes["mw_planned"] == quote, "the supporting quote must be recorded for citation"


def test_evidence_gate_records_the_quote_that_states_the_value_not_the_label():
    """A mislabelled quote must not become the citation for a value it contradicts.

    The gate deliberately accepts a value evidenced under any label, because models
    are unreliable bookkeepers. The same unreliability means the quote a model
    *filed* under a field is not necessarily the one that proves it. That was
    harmless while these quotes only fed `_excerpt`, which blends three of them,
    but `source.quotes` now persists the pairing and the console prints one
    sentence beneath one value.

    Here the model files the investment sentence under `mw_planned`. The capacity
    is still evidenced -- by the other sentence -- so it survives, and the recorded
    quote must be the one with 900 in it.
    """
    mw_quote = "the company says will draw 900\nmegawatts at full buildout"
    money_quote = "a $3.3 billion investment"
    text = f"Preamble. {mw_quote}. Also {money_quote} was announced."

    _, quotes, dropped = crawl.evidence_gate(
        {"mw_planned": 900.0},
        [{"field": "mw_planned", "quote": money_quote}, {"field": "notes", "quote": mw_quote}],
        text,
    )
    assert dropped == []
    assert "900" in quotes["mw_planned"]
    assert quotes["mw_planned"] != money_quote


def test_evidence_gate_keeps_a_correct_label_over_an_equivalent_alternative():
    """The upgrade only fires when the labelled quote does not state the value."""
    quote = "the company says will draw 900\nmegawatts at full buildout"
    _, quotes, _ = crawl.evidence_gate(
        {"mw_planned": 900.0},
        [{"field": "mw_planned", "quote": quote}],
        article(),
    )
    assert quotes["mw_planned"] == quote.strip()


def test_evidence_gate_matches_quantities_by_value_not_by_string():
    """Storage form never matches the article's wording, so compare normalized."""
    text = "The site is designed for 1.2GW at full buildout, backed by $3.3 billion."
    kept, _, dropped = crawl.evidence_gate(
        {"mw_planned": 1200.0, "investment_usd": 3_300_000_000},
        [{"field": "notes", "quote": text}],
        text,
    )
    assert kept == {"mw_planned": 1200.0, "investment_usd": 3_300_000_000}
    assert dropped == []


def test_evidence_gate_rejects_a_real_quote_citing_a_different_number():
    """Strictly stronger than the label check it replaced.

    A labelled quote never had to contain the number it was cited for, so an
    unrelated but genuine sentence used to be enough to launder an invented value.
    """
    text = "The campus will draw 200 megawatts at full buildout."
    kept, _, dropped = crawl.evidence_gate(
        {"mw_planned": 999.0},
        [{"field": "mw_planned", "quote": "The campus will draw 200 megawatts"}],
        text,
    )
    assert kept == {}
    assert dropped == ["mw_planned"]


def test_evidence_gate_reads_phase_from_article_wording():
    """`phase` is a judgement: an article says "broke ground", never "construction"."""
    text = "Crews broke ground on the 90-acre site last month."
    kept, _, _ = crawl.evidence_gate(
        {"phase": "construction"}, [{"field": "x", "quote": text}], text
    )
    assert kept == {"phase": "construction"}

    # Wording that evidences a different phase must not license this one.
    announced = "The company announced plans for a new campus."
    kept, _, dropped = crawl.evidence_gate(
        {"phase": "operational"}, [{"field": "x", "quote": announced}], announced
    )
    assert kept == {}
    assert dropped == ["phase"]


def test_evidence_gate_ignores_malformed_entries():
    kept, _, _ = crawl.evidence_gate(
        {"mw_planned": 900.0},
        ["not a dict", {"field": None, "quote": "x"}, {"field": "mw_planned"}],
        article(),
    )
    assert kept == {}


# --- build_records ----------------------------------------------------------


def build(name: str, *, prompt, result: FetchResult | None = None):
    payload = parse_json_object(canned(name))
    return crawl.build_records(
        result or fetched(),
        payload,
        prompt=prompt,
        reply=LLMReply("", "stop", 1, 1, "fake-model"),
    )


def test_extracts_at_least_nine_of_twelve_fields(prompt):
    """The PRD definition of done, as an assertion."""
    records = build("llm_response_microsoft_wi.json", prompt=prompt)
    assert len(records) == 1
    populated = crawl.count_populated(records[0])
    claims = records[0].sources[0].claims
    present = sorted(f for f in TRACKED_FIELDS if claims.get(f) is not None)
    assert populated >= 9, f"only {populated}/12 populated: {present}"


def test_extracted_values_have_the_right_python_types(prompt):
    """The PRD's highest-severity risk, directly."""
    claims = build("llm_response_microsoft_wi.json", prompt=prompt)[0].sources[0].claims
    assert isinstance(claims["mw_planned"], float) and claims["mw_planned"] == 900.0
    assert isinstance(claims["mw_built"], float) and claims["mw_built"] == 150.0
    assert isinstance(claims["investment_usd"], int) and claims["investment_usd"] == 3_300_000_000
    assert isinstance(claims["first_announced"], dt.date)
    assert claims["phase"] == "construction"
    assert re.fullmatch(r"[A-Z]{2}", claims["state"])


def test_excerpt_is_a_real_substring_of_the_article(prompt):
    """Proves the citation is quoted, not generated."""
    excerpt = build("llm_response_microsoft_wi.json", prompt=prompt)[0].sources[0].excerpt
    assert excerpt
    haystack = re.sub(r"\s+", " ", article()).lower()
    for piece in excerpt.split(" ... "):
        cleaned = re.sub(r"\s+", " ", piece).strip().rstrip("…").lower()
        assert cleaned in haystack, f"excerpt fragment not in the article: {piece!r}"


def test_excerpt_respects_the_length_cap(prompt):
    excerpt = build("llm_response_microsoft_wi.json", prompt=prompt)[0].sources[0].excerpt
    assert len(excerpt) <= 500


def test_extractor_stamp_records_the_prompt_version(prompt):
    extractor = build("llm_response_microsoft_wi.json", prompt=prompt)[0].sources[0].extractor
    assert prompt.sha1[:8] in extractor
    assert extractor.startswith("crawl:extract-v1@")
    assert "fake-model" in extractor


def test_source_type_comes_from_the_url(prompt):
    assert build("llm_response_microsoft_wi.json", prompt=prompt)[0].sources[0].source_type == (
        "trade_press"
    )


def test_events_are_extracted_and_cite_the_article(prompt):
    events = build("llm_response_microsoft_wi.json", prompt=prompt)[0].events
    assert {e.event_type for e in events} == {"announced", "groundbreaking", "energized"}
    assert all(e.source_url == URL for e in events)


def test_the_dropped_note_lists_only_what_was_really_dropped(prompt):
    """Identity fields are restored after the gate, so they are not "dropped".

    Observed on a live run: the note claimed `name` and `state` had been discarded
    when both were present on the row. A false statement in the one place an
    operator looks to judge data quality is worse than no statement.
    """
    record = build("llm_response_ungrounded.json", prompt=prompt)[0]
    flagged = [n for n in record.notes if "unconfirmed" in n]
    assert flagged, "something was ungrounded, so there must be a note"

    # Parse the field list rather than substring-matching the sentence: the prose
    # around it contains English words ("states", "notes") that collide with field
    # names and made this assertion fire on its own explanatory text.
    listed = re.search(r"unconfirmed \(待确认\): (.+?) —", flagged[0])
    assert listed, f"cannot parse the unconfirmed list from {flagged[0]!r}"
    reported = {f.strip() for f in listed.group(1).split(",")}

    source = record.sources[0]
    for identity in ("name", "company", "city", "state"):
        if source.claims.get(identity) is not None:
            assert identity not in reported, f"{identity} is on the row but reported unconfirmed"
    assert "notes" not in reported, "the summary is recorded separately"
    assert reported == set(source.unconfirmed), "the note and the flag must agree"


def test_ungrounded_values_are_kept_as_unconfirmed_never_as_fact(prompt):
    """The PRD's 待确认 tier: mark it, do not guess, and do not delete it either.

    Destroying these was throwing away real information to avoid the risk of
    storing a bad value — 194 of them across 92 of 124 projects. What must never
    happen is one being mistaken for a fact, so each is excluded from
    `confirmed_claims`, which is the set `source.fields`, `confidence` and the
    9-of-12 count all read.
    """
    record = build("llm_response_ungrounded.json", prompt=prompt)[0]
    source = record.sources[0]

    for field in ("mw_planned", "investment_usd", "expected_online"):
        assert field in source.claims, f"{field} must survive as a candidate"
        assert field in source.unconfirmed, f"{field} must be flagged 待确认"
        assert field not in source.confirmed_claims(), f"{field} must not count as a fact"

    assert any("unconfirmed" in n for n in record.notes), "the operator must be told"


def test_quotes_are_recorded_only_for_values_the_gate_confirmed(prompt):
    """`source.quotes` must never vouch for a 待确认 value.

    The gate collects a quote for every *labelled* evidence entry, including ones
    whose value it then discards, and `claims` additionally carries identity fields
    restored from the ungated values. Pairing either with a sentence would dress an
    unconfirmed value as a quoted fact — which is the one thing the tier exists to
    stop.
    """
    source = build("llm_response_ungrounded.json", prompt=prompt)[0].sources[0]
    assert set(source.quotes) <= set(source.confirmed_claims()), (
        "a quote was recorded for a field no verbatim sentence confirmed"
    )
    assert not (set(source.quotes) & set(source.unconfirmed))


def test_quotes_pair_each_confirmed_value_with_its_own_sentence(prompt):
    source = build("llm_response_microsoft_wi.json", prompt=prompt)[0].sources[0]
    assert source.quotes, "a grounded extraction must record its sentences"
    assert set(source.quotes) <= set(source.confirmed_claims())
    if "mw_planned" in source.quotes:
        assert "900" in source.quotes["mw_planned"]


def test_a_supported_risk_is_extracted(prompt):
    record = build("llm_response_microsoft_wi.json", prompt=prompt)[0]
    assert len(record.risks) == 1
    risk = record.risks[0]
    assert risk.category == "transmission"
    assert risk.severity == "material"
    assert risk.summary.startswith("Two 345-kilovolt upgrades")
    assert risk.first_seen == dt.date(2026, 2, 1)
    assert risk.source_url == URL


def test_a_risk_summary_may_be_a_paraphrase(prompt):
    """The point of the summary/quote split.

    "Two 345-kilovolt upgrades must finish before the campus can draw full load"
    shares almost no substring with the sentence evidencing it, and requiring that
    it did is what took the old free-text `blocker` field's coverage to zero.
    """
    risk = build("llm_response_microsoft_wi.json", prompt=prompt)[0].risks[0]
    assert crawl._normalize_for_match(risk.summary) not in crawl._normalize_for_match(article())
    assert crawl._normalize_for_match(risk.quote) in crawl._normalize_for_match(article())


def test_a_risk_without_a_quote_is_dropped(prompt):
    record = build("llm_response_ungrounded.json", prompt=prompt)[0]
    assert not any(r.category == "community_opposition" for r in record.risks)


def test_a_risk_whose_quote_is_not_in_the_article_is_dropped(prompt):
    """Same anti-fabrication guarantee the evidence gate gives every other field."""
    record = build("llm_response_ungrounded.json", prompt=prompt)[0]
    assert not any(r.category == "water" for r in record.risks)


def test_a_real_quote_under_the_wrong_category_is_dropped(prompt):
    """The check that `_SUMMARY_FIELDS` could not make.

    "Microsoft will operate the campus itself" is a genuine sentence from the
    article, so trusting the model's label would let it evidence a financing
    collapse. `_RISK_EVIDENCE` requires the quote to actually concern the category
    it is filed under.
    """
    record = build("llm_response_ungrounded.json", prompt=prompt)[0]
    assert not any(r.category == "financing" for r in record.risks)


def test_dropped_risks_are_disclosed(prompt):
    record = build("llm_response_ungrounded.json", prompt=prompt)[0]
    assert any("dropped unsupported risk" in n for n in record.notes)


def test_an_article_reporting_no_obstacle_yields_no_risk(prompt):
    """The common and correct case. Most projects have no blocker, and inventing
    one is worse than recording none — see tracker/gaps.py."""
    record = build("llm_response_two_projects.json", prompt=prompt)[0]
    assert record.risks == []


def test_a_risk_supports_the_derived_blocker_in_source_fields(prompt):
    """`blocker` is derived from `risk` rows, but it must still be traceable to a
    citation, or `test_every_field_is_cited` would have nothing to find."""
    record = build("llm_response_microsoft_wi.json", prompt=prompt)[0]
    assert record.sources[0].claims["blocker"] == record.risks[0].summary


def test_an_unknown_risk_category_is_dropped(prompt):
    """A category outside the vocabulary cannot be aggregated, and aggregation is
    the reason the table exists."""
    risks, _ = crawl._risks(
        {"risks": [{"category": "traffic", "severity": "watch", "summary": "s", "quote": "x"}]},
        article(),
        URL,
    )
    assert risks == []


def test_the_extractor_may_not_assert_unclassified(prompt):
    """`unclassified` exists for a human assertion, not for a model that could not
    decide — an unclassified row is invisible to every rollup."""
    risks, _ = crawl._risks(
        {
            "risks": [
                {
                    "category": "unclassified",
                    "severity": "watch",
                    "summary": "s",
                    "quote": "The principal obstacle is transmission",
                }
            ]
        },
        article(),
        URL,
    )
    assert risks == []


def test_an_unrecognized_severity_falls_back_to_watch(prompt):
    """Conservative direction: the obstacle is real and evidenced, only its stated
    effect is unclear, and overstating that turns a mention into a blocker."""
    risks, _ = crawl._risks(
        {
            "risks": [
                {
                    "category": "transmission",
                    "severity": "catastrophic",
                    "summary": "s",
                    "quote": "The principal obstacle is transmission: American Transmission Company must",
                }
            ]
        },
        article(),
        URL,
    )
    assert [r.severity for r in risks] == ["watch"]


def test_two_risks_of_one_category_and_date_collapse(prompt):
    """The stored UNIQUE is (project, category, first_seen), so keeping both would
    fail on insert. Same accepted cost `event` already documents."""
    entry = {
        "category": "transmission",
        "severity": "watch",
        "summary": "s",
        "quote": "The principal obstacle is transmission",
        "first_seen": "2026-02-01",
    }
    risks, _ = crawl._risks({"risks": [entry, dict(entry, summary="other")]}, article(), URL)
    assert len(risks) == 1


def test_risks_per_project_are_capped(prompt, monkeypatch):
    monkeypatch.setattr(crawl, "MAX_RISKS_PER_PROJECT", 2)
    quote = "The principal obstacle is transmission"
    entries = [
        {"category": c, "severity": "watch", "summary": "s", "quote": quote}
        for c in ("transmission", "grid_capacity", "permitting")
    ]
    # All three categories list "interconnection"/"transmission"-adjacent wording,
    # so the cap rather than the gate is what limits this.
    risks, _ = crawl._risks({"risks": entries}, article(), URL)
    assert len(risks) <= 2


def test_two_projects_in_one_article_become_two_records(prompt):
    records = build("llm_response_two_projects.json", prompt=prompt)
    assert len(records) == 2
    assert {r.project["city"] for r in records} == {"Mount Pleasant", "Madison"}
    # The same URL legitimately cites both.
    assert {r.sources[0].url for r in records} == {URL}


def test_a_project_without_identity_is_dropped(prompt):
    """A passing mention with no company or locality is not a project."""
    payload = {"projects": [{"name": "Something", "mw_planned": 100, "evidence": []}]}
    assert (
        crawl.build_records(
            fetched(), payload, prompt=prompt, reply=LLMReply("", "stop", 1, 1, "m")
        )
        == []
    )


def test_too_many_projects_are_capped(prompt, caplog):
    payload = {
        "projects": [
            {
                "name": f"Site {i}",
                "company": "Microsoft",
                "city": "Mount Pleasant",
                "state": "WI",
                "evidence": [
                    {"field": "company", "quote": "Microsoft will operate the campus itself"},
                    {
                        "field": "city",
                        "quote": "a data\ncenter campus in Mount Pleasant, Wisconsin",
                    },
                    {"field": "state", "quote": "MOUNT PLEASANT, Wis."},
                ],
            }
            for i in range(12)
        ]
    }
    records = crawl.build_records(
        fetched(), payload, prompt=prompt, reply=LLMReply("", "stop", 1, 1, "m")
    )
    assert len(records) == crawl.MAX_PROJECTS_PER_ARTICLE
    assert "kept the first" in caplog.text, "silent truncation would read as full coverage"


def test_payload_without_a_projects_list_raises(prompt):
    with pytest.raises(LLMJsonError):
        crawl.build_records(
            fetched(), {"result": "ok"}, prompt=prompt, reply=LLMReply("", "stop", 1, 1, "m")
        )


# --- Input budget -----------------------------------------------------------


def test_truncate_keeps_head_and_tail():
    text = "A" * 1000 + "B" * 1000 + "C" * 1000
    out = crawl.truncate(text, 600)
    assert len(out) <= 600 + len(crawl.TRUNCATION_MARKER)
    assert out.startswith("A")
    assert out.endswith("C"), "the tail carries timelines and objections"
    assert crawl.TRUNCATION_MARKER in out


def test_truncate_leaves_short_text_alone():
    assert crawl.truncate("short", 100) == "short"


def test_oversized_article_is_truncated_before_the_llm_sees_it(prompt, session):
    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    huge = fetched(markdown=article() + ("filler " * 20_000))
    crawl.extract_one(huge, prompt=prompt, extractor=llm)
    _, user = llm.seen[0]
    settings = crawl.get_settings()
    assert len(user) <= settings.max_input_chars + len(prompt.user_template) + 500
    assert crawl.TRUNCATION_MARKER in user


# --- extract_one ------------------------------------------------------------


def test_extract_one_sends_the_prompt_file_verbatim(prompt):
    """The prompt file must be what actually ships, or versioning is a fiction."""
    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    crawl.extract_one(fetched(), prompt=prompt, extractor=llm)
    system, user = llm.seen[0]
    assert system == prompt.system
    assert URL in user
    assert "Return the JSON object now." in user


def test_extract_one_retries_once_then_gives_up(prompt):
    llm = FakeLLM(["not json at all", "still not json", "third"])
    outcome = crawl.extract_one(fetched(), prompt=prompt, extractor=llm)
    assert outcome.status == "parse_error"
    # llm_max_attempts defaults to 2: one attempt plus one corrective retry.
    assert len(llm.seen) == 2, "cost per URL must be bounded"
    assert "JSON" in llm.seen[1][1], "the retry must tell the model what went wrong"


def test_extract_one_recovers_on_the_retry(prompt):
    llm = FakeLLM(["garbage", canned("llm_response_microsoft_wi.json")])
    outcome = crawl.extract_one(fetched(), prompt=prompt, extractor=llm)
    assert outcome.status == "ok"
    assert len(outcome.records) == 1


def test_truncated_reply_is_retried(prompt):
    llm = FakeLLM(['{"projects": [', '{"projects": ['], finish_reason="length")
    outcome = crawl.extract_one(fetched(), prompt=prompt, extractor=llm)
    assert outcome.status == "parse_error"
    assert "truncated" in (outcome.error or "")


def test_provider_error_is_reported_not_raised(prompt):
    outcome = crawl.extract_one(fetched(), prompt=prompt, extractor=BoomLLM())
    assert outcome.status == "llm_error"
    assert "exploded" in outcome.error


def test_empty_projects_list_is_no_project_not_an_error(prompt):
    llm = FakeLLM(['{"projects": []}'])
    outcome = crawl.extract_one(fetched(), prompt=prompt, extractor=llm)
    assert outcome.status == "no_project"
    assert outcome.records == []


def test_token_usage_is_accounted(prompt):
    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    outcome = crawl.extract_one(fetched(), prompt=prompt, extractor=llm)
    assert outcome.prompt_tokens == 1200
    assert outcome.completion_tokens == 400


# --- run() end to end -------------------------------------------------------


def test_run_persists_project_source_and_events(session):
    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    report = crawl.run(
        session, [URL], fetcher=FakeFetcher({URL: fetched()}), extractor=llm, run_id="t1"
    )
    assert report.inserted == 1
    project = session.scalar(select(Project))
    assert project.name == "Fairwater"
    assert project.mw_planned == 900.0
    assert project.investment_usd == 3_300_000_000
    assert project.city == "Mount Pleasant"
    assert project.county == "Racine County"
    assert report.events == 3

    source = session.scalar(select(Source))
    assert source.url == URL
    assert source.source_type == "trade_press"
    assert set(json.loads(source.claims)) == set(source.fields.split(","))


def test_run_needs_no_api_key(session):
    """A fresh clone with no key must still run the suite."""
    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    report = crawl.run(
        session, [URL], fetcher=FakeFetcher({URL: fetched()}), extractor=llm, run_id="t"
    )
    assert report.inserted == 1


def test_a_single_article_reaches_confidence_two(session):
    """One trade-press citation is solid but uncorroborated."""
    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    crawl.run(session, [URL], fetcher=FakeFetcher({URL: fetched()}), extractor=llm, run_id="t")
    assert session.scalar(select(Project)).confidence == 2


def test_fetch_failure_costs_no_llm_call(session):
    """Never pay for a page we could not read."""
    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    bad = FetchResult(URL, False, error="HTTP 403", status=403, fetched_at=NOW)
    report = crawl.run(session, [URL], fetcher=FakeFetcher({URL: bad}), extractor=llm, run_id="t")
    assert report.fetch_error == 1
    assert llm.seen == [], "no LLM call for a failed fetch"
    assert session.scalar(select(Project)) is None

    row = session.scalar(select(IngestUrl))
    assert row.status == "fetch_error"
    assert row.http_status == 403


def test_fetch_error_is_recorded_in_ingest_url_not_as_a_source(session):
    """The PRD asks to "mark the source fetch_error", which the schema cannot do:
    source_type has no such member and a source row requires a project_id."""
    llm = FakeLLM([])
    bad = FetchResult(URL, False, error="timeout", fetched_at=NOW)
    crawl.run(session, [URL], fetcher=FakeFetcher({URL: bad}), extractor=llm, run_id="t")
    assert session.scalar(select(Source)) is None
    assert session.scalar(select(IngestUrl)).status == "fetch_error"


def test_rerunning_skips_urls_already_done(session):
    fetcher = FakeFetcher({URL: fetched()})
    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    crawl.run(session, [URL], fetcher=fetcher, extractor=llm, run_id="t1")

    second = crawl.run(session, [URL], fetcher=fetcher, extractor=FakeLLM([]), run_id="t2")
    assert second.filtered == 1
    assert len(fetcher.calls) == 1, "a completed URL must not be re-fetched"


def test_force_reprocesses_a_completed_url(session):
    fetcher = FakeFetcher({URL: fetched()})
    crawl.run(
        session,
        [URL],
        fetcher=fetcher,
        extractor=FakeLLM([canned("llm_response_microsoft_wi.json")]),
        run_id="t1",
    )
    report = crawl.run(
        session,
        [URL],
        fetcher=fetcher,
        extractor=FakeLLM([canned("llm_response_microsoft_wi.json")]),
        run_id="t2",
        force=True,
    )
    assert report.filtered == 0
    assert report.unchanged == 1, "the same input must produce no change"
    assert len(fetcher.calls) == 2


def test_duplicate_urls_are_fetched_once(session):
    fetcher = FakeFetcher({URL: fetched()})
    crawl.run(
        session,
        [URL, URL, URL],
        fetcher=fetcher,
        extractor=FakeLLM([canned("llm_response_microsoft_wi.json")]),
        run_id="t",
    )
    assert len(fetcher.calls) == 1


def test_dry_run_writes_nothing(session):
    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    report = crawl.run(
        session,
        [URL],
        fetcher=FakeFetcher({URL: fetched()}),
        extractor=llm,
        run_id="t",
        dry_run=True,
    )
    assert report.inserted == 1
    assert session.scalar(select(Project)) is None


def test_cache_avoids_a_second_fetch(session, tmp_path: Path):
    """Prompt iteration is the inner loop and must never re-fetch."""
    cache = tmp_path / "cache"
    fetcher = FakeFetcher({URL: fetched()})
    crawl.run(
        session,
        [URL],
        fetcher=fetcher,
        extractor=FakeLLM([canned("llm_response_microsoft_wi.json")]),
        run_id="t1",
        cache_dir=cache,
    )
    assert len(fetcher.calls) == 1

    crawl.run(
        session,
        [URL],
        fetcher=fetcher,
        extractor=FakeLLM([canned("llm_response_microsoft_wi.json")]),
        run_id="t2",
        force=True,
        cache_dir=cache,
    )
    assert len(fetcher.calls) == 1, "the second run must come from cache"


def test_conflicting_sources_keep_both_and_flag_it(session):
    """A queue row and an article disagreeing on capacity, end to end."""
    from tracker.ingest import pjm

    pjm.run(
        session,
        Path(__file__).parent / "fixtures" / "pjm_queue_sample.csv",
        iso="pjm",
        trust_gen_mw=True,
    )
    before = session.scalar(
        select(Project).where(Project.dedup_key == "microsoft|county:racine|WI")
    )
    assert before is not None and before.mw_planned == 600.0

    # Now an article about the same company/county asserting 900 MW.
    payload = json.loads(
        canned("llm_response_microsoft_wi.json").split("```json")[1].split("```")[0]
    )
    payload["projects"][0]["city"] = None
    payload["projects"][0]["county"] = "Racine County"
    llm = FakeLLM([json.dumps(payload)])
    crawl.run(session, [URL], fetcher=FakeFetcher({URL: fetched()}), extractor=llm, run_id="t")

    project = session.scalar(
        select(Project).where(Project.dedup_key == "microsoft|county:racine|WI")
    )
    assert len(project.sources) >= 2, "both claims must survive in their own source rows"
    assert project.mw_planned == 900.0, "trade_press (2) outweighs iso_queue (1)"
    assert "conflict mw_planned" in project.notes
    assert "33% spread" in project.notes


# --- URL list parsing -------------------------------------------------------


def test_read_urls_skips_comments_blanks_and_dedupes(tmp_path: Path):
    path = tmp_path / "urls.txt"
    path.write_text(
        "# a comment\n"
        "https://a.example/1\n"
        "\n"
        "   https://b.example/2   \n"
        "https://a.example/1\n"
        "not-a-url\n",
        encoding="utf-8",
    )
    assert crawl.read_urls(path) == ["https://a.example/1", "https://b.example/2"]


# --- Fetch helpers ----------------------------------------------------------


def test_html_to_text_strips_markup_and_keeps_paragraphs():
    html = """
    <html><head><style>p{color:red}</style></head><body>
    <nav>Menu</nav>
    <article><h1>Headline</h1><p>First&nbsp;paragraph.</p><p>Second &amp; last.</p></article>
    <script>track()</script><footer>Legal</footer></body></html>
    """
    text = html_to_text(html)
    assert "Headline" in text
    assert "First paragraph." in text
    assert "Second & last." in text
    assert "track()" not in text
    assert "color:red" not in text


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (FetchResult("u", False, status=403, via="httpx"), True),
        (FetchResult("u", False, status=503, via="httpx"), True),
        (FetchResult("u", True, markdown="x" * 50, via="httpx"), True),
        (FetchResult("u", True, markdown="x" * 5000, via="httpx"), False),
        (FetchResult("u", False, status=404, via="httpx"), False),
        (FetchResult("u", False, status=403, via="crawl4ai"), False),
    ],
)
def test_should_escalate(result, expected):
    """A browser is worth trying for a block or a JS shell, not for a 404."""
    assert should_escalate(result) is expected


# --- escalation lifecycle ----------------------------------------------------


class _RecordingBrowser:
    """Stands in for Crawl4AIFetcher, and insists on the same contract.

    Refusing to fetch before `__aenter__` is the point: that is exactly what the
    real one does, and what nothing was doing for it.
    """

    def __init__(self):
        self.entered = 0
        self.exited = 0
        self.fetched: list[str] = []
        self._open = False

    async def __aenter__(self):
        self.entered += 1
        self._open = True
        return self

    async def __aexit__(self, *exc):
        self.exited += 1
        self._open = False

    async def fetch(self, url):
        if not self._open:
            raise RuntimeError("Crawl4AIFetcher must be used as an async context manager")
        self.fetched.append(url)
        return FetchResult(url, True, markdown="x" * 5000, via="crawl4ai", fetched_at=NOW)


class _Blocked:
    """Plain HTTP that gets a 403, which is what triggers escalation."""

    async def fetch(self, url):
        return FetchResult(url, False, status=403, via="httpx", fetched_at=NOW)


class _Fine:
    async def fetch(self, url):
        return FetchResult(url, True, markdown="y" * 5000, via="httpx", fetched_at=NOW)


async def test_the_browser_fetcher_is_started_before_it_is_used():
    """`--browser` never worked: nobody entered the context manager.

    The CLI built a `Crawl4AIFetcher` and handed it down; `crawl.run` owns the
    `asyncio.run`, so no synchronous caller could enter it, and the first page
    that needed escalating died with "must be used as an async context manager".
    `fetch_all` is the only place inside the loop, so it is the place that does it.
    """
    from tracker.ingest.fetch import fetch_all

    browser = _RecordingBrowser()
    results = await fetch_all(["https://blocked.test/a"], fetcher=_Blocked(), escalate=browser)
    assert browser.entered == 1
    assert browser.fetched == ["https://blocked.test/a"]
    assert results[0].via == "crawl4ai"


async def test_the_browser_is_shut_down_afterwards():
    """Chromium does not exit on its own and outlives the command that spawned it."""
    from tracker.ingest.fetch import fetch_all

    browser = _RecordingBrowser()
    await fetch_all(["https://blocked.test/a"], fetcher=_Blocked(), escalate=browser)
    assert browser.exited == 1


async def test_the_browser_starts_once_for_many_pages():
    from tracker.ingest.fetch import fetch_all

    browser = _RecordingBrowser()
    urls = [f"https://blocked.test/{i}" for i in range(5)]
    await fetch_all(urls, fetcher=_Blocked(), escalate=browser)
    assert browser.entered == 1, "one browser per run, not one per page"
    assert len(browser.fetched) == 5


async def test_the_browser_never_starts_when_nothing_needs_it():
    """Launching Chromium costs seconds and a process; most runs never escalate."""
    from tracker.ingest.fetch import fetch_all

    browser = _RecordingBrowser()
    await fetch_all(["https://fine.test/a"], fetcher=_Fine(), escalate=browser)
    assert browser.entered == 0
    assert browser.exited == 0


async def test_a_browser_that_will_not_start_degrades_loudly(caplog):
    """A missing Chromium must not take the whole run down with it.

    The plain-HTTP result still stands, the failure is logged once rather than
    per URL, and `tracker queue --failed` will show what could not be read.
    """
    from tracker.ingest.fetch import fetch_all

    class _Broken(_RecordingBrowser):
        async def __aenter__(self):
            raise RuntimeError("chromium is not installed")

    urls = [f"https://blocked.test/{i}" for i in range(3)]
    with caplog.at_level("ERROR"):
        results = await fetch_all(urls, fetcher=_Blocked(), escalate=_Broken())
    assert [r.status for r in results] == [403, 403, 403]
    assert sum("could not start the browser" in r.message for r in caplog.records) == 1


def test_a_foreign_language_quote_cannot_evidence_a_phase():
    """The one hole the summary-field carve-out opened.

    Quantities and dates are protected from a translated repost for free: "230兆瓦"
    matches no MW pattern. But `phase` is a `_SUMMARY_FIELD`, so the gate trusts
    the model's *label* over a verified quote -- and measured against a real
    Chinese repost, `phase=construction` sailed through while every number on the
    same article was correctly dropped.
    """
    chinese = (
        "Stack Infrastructure正计划在美国俄勒冈州希尔斯伯勒市开发230兆瓦"
        "数据中心园区，投资12亿美元，预计2027年投入运营。"
    )
    values = {
        "mw_planned": 230.0,
        "investment_usd": 1_200_000_000,
        "phase": "construction",
        "city": "Hillsboro",
    }
    kept, _, dropped = crawl.evidence_gate(
        values, [{"field": f, "quote": chinese} for f in values], chinese
    )
    assert kept == {}, "nothing may be evidenced by a translated repost"
    assert set(dropped) == set(values)


def test_an_english_quote_still_evidences_a_phase():
    """The language check must not break the ordinary path."""
    english = "Crews broke ground on the Hillsboro campus in March."
    kept, _, _ = crawl.evidence_gate(
        {"phase": "construction"}, [{"field": "phase", "quote": english}], english
    )
    assert kept == {"phase": "construction"}


def test_a_first_party_release_outranks_trade_press():
    """The bug the newsroom path would have hit on day one.

    The subdomain rules recognise `news.microsoft.com` and `about.fb.com`, but
    STACK publishes at `www.stackinfra.com/news/...`. Without the operator
    registry that falls through to `general_media`, weight 1 — scoring a
    first-party announcement below the trade-press rewrite of it.
    """
    url = "https://www.stackinfra.com/news/new-hillsboro-campus/"
    assert crawl.classify_source_type(url) == "general_media"
    assert (
        crawl.classify_source_type(url, operator_hosts=frozenset({"stackinfra.com"}))
        == "company_filing"
    )


def test_the_operator_registry_does_not_override_a_known_outlet():
    """A trade-press domain must keep its own classification."""
    assert (
        crawl.classify_source_type(
            "https://www.datacenterfrontier.com/article/1",
            operator_hosts=frozenset({"stackinfra.com"}),
        )
        == "trade_press"
    )


def test_an_unreadable_feed_config_does_not_stop_ingest(monkeypatch):
    """Misclassifying a source beats failing the run over a config problem."""
    crawl.operator_hosts.cache_clear()
    monkeypatch.setattr(
        "tracker.ingest.discover.newsroom_companies",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad toml")),
    )
    assert crawl.operator_hosts() == frozenset()
    crawl.operator_hosts.cache_clear()


# --- Money that is real but not this project's -------------------------------


def test_a_programme_total_quoted_for_one_site_is_demoted_not_stored():
    """The gate cannot catch this and the ratio can.

    An article about one 1,167 MW campus quotes the programme it belongs to. The
    number really is in the text, so the evidence gate passes it; only its ratio
    to the capacity on the same row shows it belongs to something larger.
    """
    note = crawl._implausible_investment({"investment_usd": 165_000_000_000, "mw_planned": 1167})
    assert note is not None
    assert "programme-wide" in note
    assert "待确认" in note


@pytest.mark.parametrize(
    ("usd", "mw"),
    [
        (25_000_000_000, 1200),  # Stargate Abilene, real
        (4_700_000_000, 350),  # Fairwater, real
        (25_000_000_000, 1400),  # Lighthouse, real
        (173_000_000, 700),  # AMD, real and cheap per MW
    ],
)
def test_real_figures_are_left_alone(usd: int, mw: float):
    """A GPU-heavy campus legitimately costs far more per MW than a shell.

    The ceiling sits in a measured gap — the live distribution runs to $23M/MW and
    then jumps to $83M — so it must not clip the top of the real range.
    """
    assert crawl._implausible_investment({"investment_usd": usd, "mw_planned": mw}) is None


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"investment_usd": 5_000_000_000},
        {"mw_planned": 300},
        {"investment_usd": 5_000_000_000, "mw_planned": 0},
        {"investment_usd": True, "mw_planned": 300},
    ],
)
def test_the_check_needs_both_numbers_to_say_anything(claims: dict):
    assert crawl._implausible_investment(claims) is None
