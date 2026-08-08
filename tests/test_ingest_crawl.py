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
        ("https://www.sec.gov/Archives/x", "company_filing"),
        ("https://dnr.wi.gov/permit", "government_doc"),
        ("https://www.datacenterdynamics.com/en/news/x", "trade_press"),
        ("https://www.utilitydive.com/news/x", "trade_press"),
        ("https://www.example-news.com/x", "general_media"),
        # A newsroom subdomain, with nothing saying whose newsroom it is.
        ("https://news.microsoft.com/x", "general_media"),
        ("https://ir.example.com/x", "general_media"),
    ],
)
def test_classify_source_type(url, expected):
    assert crawl.classify_source_type(url) == expected


# --- a newsroom subdomain is only evidence together with a known operator -----
#
# `^(news|about|blog|ir|investor|newsroom|press)\.` used to return
# `company_filing` — weight 3, the heaviest in the system — for any host whose
# first label matched, with no check on whose domain it was. It is not a
# structural signal: `news.microsoft.com` and `news.17173.com` are the same
# shape, and only one of them is a data center operator.


@pytest.mark.parametrize("host", ["news.17173.com", "news.futunn.com"])
def test_a_newsroom_subdomain_of_an_unknown_domain_is_not_official(host):
    """Both were live: a Chinese gaming portal and a stock brokerage, at weight 3.

    On Fairwater (#1) the gaming site was the *only* `company_filing` on the row.
    It decided the stored $3.3B investment and supplied the "strongest source is
    company_filing" line in the confidence rationale.
    """
    hosts = crawl.operator_hosts()
    assert crawl.classify_source_type(f"https://{host}/x", operator_hosts=hosts) == "general_media"


def test_a_newsroom_subdomain_of_a_known_operator_is_official():
    """The case the rule was written for, now requiring the domain to be known."""
    hosts = frozenset({"example-operator.com"})
    assert (
        crawl.classify_source_type("https://news.example-operator.com/x", operator_hosts=hosts)
        == "company_filing"
    )


def test_the_operator_domain_itself_still_counts():
    """`about.fb.com` is listed whole in `feeds.toml`, and must keep its weight."""
    hosts = crawl.operator_hosts()
    assert (
        crawl.classify_source_type("https://about.fb.com/news/x", operator_hosts=hosts)
        == "company_filing"
    )


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
    assert dropped == {}


def test_evidence_gate_drops_values_with_no_quote():
    kept, _, dropped = crawl.evidence_gate({"mw_planned": 900.0}, [], article())
    assert kept == {}
    assert dropped == {"mw_planned": "no_quote"}


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
    assert dropped == {"mw_planned": "quote_unverified"}
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
    assert dropped == {}
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
    assert dropped == {}
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
    assert dropped == {}


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
    assert dropped == {"mw_planned": "quote_off_target"}


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
    assert dropped == {"phase": "no_quote"}


def test_evidence_gate_ignores_malformed_entries():
    kept, _, _ = crawl.evidence_gate(
        {"mw_planned": 900.0},
        ["not a dict", {"field": None, "quote": "x"}, {"field": "mw_planned"}],
        article(),
    )
    assert kept == {}


# --- Recovering a quote the model edited ------------------------------------
#
# Exact containment was throwing away real capacity and money figures. Measured
# over 131 evidence quotes from 8 cached articles: 33 failed the substring test,
# and the dominant cause was not fabrication but the model *resolving references*
# while it quotes — substituting the company name for "the company", the site
# name for "the campus". Helpful for a reader, fatal for a substring test.
#
# The fix keeps the anti-fabrication guarantee by storing the article's own
# longest verbatim run rather than the model's edit. Acceptance went 75% -> 95%
# with zero crossings against an unrelated article.


def test_a_quote_the_model_edited_keeps_the_articles_words_not_the_models():
    """The load-bearing property of the whole recovery path.

    The model resolved "the company" to "Microsoft". We accept the evidence,
    but what we store — and therefore what `_stated_in` is then tested against,
    and what a reader sees in the drawer — is the sentence as published.
    """
    kept, quotes, dropped = crawl.evidence_gate(
        {"mw_planned": 900.0},
        [
            {
                "field": "mw_planned",
                "quote": (
                    "Microsoft has begun construction on Fairwater, a data center campus "
                    "in Mount Pleasant, Wisconsin, that Microsoft says will draw 900 "
                    "megawatts at full buildout."
                ),
            }
        ],
        article(),
    )
    assert kept == {"mw_planned": 900.0}
    assert dropped == {}
    assert "the company says" in quotes["mw_planned"]
    assert "that Microsoft says" not in quotes["mw_planned"]
    # And what we stored is genuinely in the article, which is the invariant.
    assert crawl._normalize_for_match(quotes["mw_planned"]) in crawl._normalize_for_match(article())


def test_a_recovered_quote_is_stored_as_prose_not_as_the_source_wrapping():
    """Filings and PDF-derived text wrap mid-sentence.

    The offsets point back into the original, so without collapsing the run
    the drawer renders a quote broken across lines at the source's column width.
    """
    kept, quotes, _ = crawl.evidence_gate(
        {"mw_planned": 900.0},
        [
            {
                "field": "mw_planned",
                "quote": (
                    "Microsoft has begun construction on Fairwater, a data center campus "
                    "in Mount Pleasant, Wisconsin, that Microsoft says will draw 900 "
                    "megawatts at full buildout."
                ),
            }
        ],
        article(),
    )
    assert kept
    assert "\n" not in quotes["mw_planned"]


def test_widening_recovers_a_figure_the_matched_run_stopped_short_of():
    """Recovery is worthless if it drops the number it was cited for.

    Here the model's edit lands immediately before "$3.3 billion", so the
    longest common run ends at the sentence break and the recovered quote no
    longer evidences the value. Widening to the sentence edge — still the
    article's own text — is what makes the recovery actually keep the field.
    """
    quote = (
        "Microsoft first announced the Racine County project in March 2023. "
        "Microsoft has committed $3.3 billion to the site."
    )
    run = crawl._verbatim_run(quote, article())
    assert run.text is not None
    assert "$3.3 billion" in run.text

    kept, quotes, dropped = crawl.evidence_gate(
        {"investment_usd": 3_300_000_000.0},
        [{"field": "investment_usd", "quote": quote}],
        article(),
    )
    assert kept == {"investment_usd": 3_300_000_000.0}
    assert dropped == {}
    assert "The company has committed" in quotes["investment_usd"]


def test_a_fabricated_quote_is_still_rejected_however_plausible(caplog):
    """Recovery must not become a way in for invented text.

    This reads exactly like the article and is about the same project, but no
    stretch of it was ever published, so there is nothing to recover.
    """
    kept, quotes, dropped = crawl.evidence_gate(
        {"mw_planned": 4200.0},
        [
            {
                "field": "mw_planned",
                "quote": (
                    "Officials confirmed the Mount Pleasant site is now expected to "
                    "reach 4,200 megawatts once every phase is energized."
                ),
            }
        ],
        article(),
    )
    assert kept == {}
    assert quotes == {}
    assert dropped == {"mw_planned": "quote_unverified"}
    assert "not in the article" in caplog.text


def test_a_rejected_quote_is_logged_with_what_was_offered_and_how_close_it_came(caplog):
    """The warning has to be actionable on its own.

    Naming only the field says a value was lost and nothing about why, so
    deciding whether the gate is too strict needed an instrumented replay of
    the extraction path. Logging the quote and the longest run it really
    matched makes every ordinary run produce that dataset for free.
    """
    caplog.set_level("WARNING")
    crawl.evidence_gate(
        {"mw_planned": 4200.0},
        [
            {
                "field": "mw_planned",
                "quote": (
                    "Officials confirmed the Mount Pleasant site is now expected to "
                    "reach 4,200 megawatts once every phase is energized."
                ),
            }
        ],
        article(),
    )
    assert "'mw_planned'" in caplog.text
    assert "Officials confirmed the Mount Pleasant site" in caplog.text
    assert re.search(r"best run \d+ of \d+ chars, \d+%", caplog.text)


def test_a_logged_quote_is_one_line_and_bounded():
    """A wall of these is the normal case on a thin page, so each stays short."""
    sprawling = ("The campus will draw nine hundred megawatts.\n" * 20).strip()
    logged = crawl._for_log(sprawling)
    assert "\n" not in logged
    assert len(logged) <= crawl._LOGGED_QUOTE_CHARS


def test_a_real_quote_from_a_different_article_is_rejected():
    """The negative control the thresholds were tuned against.

    Every one of the 131 sampled quotes was also run against an unrelated
    article. If generic infrastructure phrasing were enough to clear the bar,
    a citation could be satisfied by a story about a different site entirely.
    """
    elsewhere = (
        "Vantage has begun construction on a data center campus in Port Washington, "
        "Ohio, that the company says will draw 1,400 megawatts at full buildout. "
        "The developer has committed $8.1 billion to the site."
    )
    assert crawl._verbatim_run(elsewhere, article()).text is None

    kept, _, dropped = crawl.evidence_gate(
        {"mw_planned": 1400.0},
        [{"field": "mw_planned", "quote": elsewhere}],
        article(),
    )
    assert kept == {}
    assert dropped == {"mw_planned": "quote_unverified"}


def test_a_genuine_fragment_cannot_carry_a_mostly_invented_quote():
    """`MIN_RUN_FRACTION`, and why a length floor alone is not enough.

    Opening with one real clause and continuing into invention is the cheapest
    way to defeat a "some of this is real" test, so most of the quote has to be
    the article, not just some qualifying stretch of it.
    """
    quote = (
        "Microsoft has begun construction on Fairwater, a data center campus, "
        "and executives told investors the site will ultimately support more than "
        "two gigawatts of critical load across six additional buildings now in "
        "design, with the first of them due to break ground before the end of the year."
    )
    assert crawl._verbatim_run(quote, article()).text is None


def test_a_short_generic_phrase_is_not_enough_however_much_of_it_matches():
    """`MIN_RUN_CHARS`, which the fraction floor does not cover.

    "broke ground on the site" is 25 characters of near-boilerplate that appears
    in every construction story ever filed, and it is 78% of this quote — so the
    fraction test waves it through. The floor is what stops it, and it matters
    because widening would then hand the model a whole sentence it never quoted:
    it guessed 2024, and the sentence it would be credited with says 2023.
    """
    assert crawl._verbatim_run("broke ground on the site in 2024", article()).text is None


def test_widening_crosses_a_sentence_that_ends_at_a_line_wrap():
    """Fetched articles are hard wrapped, so most sentences end at ".\\n".

    Looking for a literal ". " finds the minority of them. This is the same
    mid-figure truncation as above, in the form it actually arrives in.
    """
    text = (
        "Vantage first announced the Ohio project in March 2024.\n"
        "The company has committed $2.1 billion to the site.\n"
        "Local officials welcomed the plan.\n"
    )
    quote = (
        "Vantage first announced the Ohio project in March 2024. "
        "Vantage has committed $2.1 billion to the site."
    )
    run = crawl._verbatim_run(quote, text)
    assert run.text is not None
    assert "$2.1 billion" in run.text

    kept, _, dropped = crawl.evidence_gate(
        {"investment_usd": 2_100_000_000.0},
        [{"field": "investment_usd", "quote": quote}],
        text,
    )
    assert kept == {"investment_usd": 2_100_000_000.0}
    assert dropped == {}


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


# --- the event gate ------------------------------------------------------------
#
# Events were the last extracted structure with no gate at all: the prompt said
# "Only milestones whose date you can quote" and nothing enforced it, so every
# milestone fed the track strip on the model's say-so. Observed on Fairwater
# (#1): a `groundbreaking` dated 2026-06-23 whose own description reads "Open
# house event held to announce opening". Same shape as the risk gate — the
# quote must be real, the description stays the model's words, and a failure
# demotes rather than deletes.


def _one_event(entry: dict) -> object:
    events = crawl._events({"events": [entry]}, article(), URL)
    assert len(events) == 1, "the gate demotes; it must never delete"
    return events[0]


def test_an_event_with_a_real_quote_is_confirmed():
    got = _one_event(
        {
            "event_date": "2023-06-15",
            "event_type": "groundbreaking",
            "description": "Crews broke ground.",
            "quote": "Construction crews broke ground on the site on 15 June 2023.",
        }
    )
    assert got.unconfirmed is None
    assert got.quote is not None
    assert "broke ground" in got.quote


def test_an_event_quote_nobody_published_is_stripped_and_flagged():
    """The fabrication case: the event survives, the sentence does not.

    A quote that fails verification must not be stored either — showing an
    invented sentence as though it were the article's is worse than showing
    nothing, which is exactly the rule risks follow.
    """
    got = _one_event(
        {
            "event_date": "2023-06-15",
            "event_type": "groundbreaking",
            "description": "Crews broke ground.",
            "quote": "A ceremonial shovel event was attended by the governor of Wisconsin.",
        }
    )
    assert got.unconfirmed == "quote_unverified"
    assert got.quote is None


def test_an_event_offered_without_a_quote_is_kept_as_uncited():
    """The pre-0017 shape, now declared instead of silent."""
    got = _one_event(
        {
            "event_date": "2023-06-15",
            "event_type": "groundbreaking",
            "description": "Crews broke ground.",
        }
    )
    assert got.unconfirmed == "no_quote"
    assert got.quote is None


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


def _risk(record, category):
    return next((r for r in record.risks if r.category == category), None)


def test_a_risk_without_a_quote_is_kept_as_unconfirmed_not_deleted(prompt):
    """The one place in the ingest path that still destroyed extracted information.

    Every *field* in this position is kept and flagged 待确认 (migration 0006).
    A risk was deleted, which fell hardest on the field the database is worst at:
    no press release names its own blocker, so an adversarial second source is
    the only thing that ever records one.
    """
    risk = _risk(build("llm_response_ungrounded.json", prompt=prompt)[0], "community_opposition")
    assert risk is not None
    assert risk.unconfirmed == "no_quote"
    assert risk.quote is None
    assert risk.summary


def test_a_risk_whose_quote_is_not_in_the_article_keeps_the_risk_and_drops_the_quote(prompt):
    """The anti-fabrication guarantee is unchanged; only the remedy is.

    The model wrote a sentence nobody published, so that sentence is not stored —
    but the obstacle it was reaching for is not evidence *against* an obstacle,
    and deleting it threw away the category and the severity too.
    """
    risk = _risk(build("llm_response_ungrounded.json", prompt=prompt)[0], "water")
    assert risk is not None
    assert risk.unconfirmed == "quote_unverified"
    assert risk.quote is None, "a fabricated sentence is never stored"


def test_a_risk_quote_the_model_edited_is_recovered_too():
    """Risks lose evidence to reference resolution exactly like fields do.

    They feed the blocker column and the risk aggregation, so dropping them on a
    substring mismatch costs the same kind of real information.
    """
    text = (
        "The principal obstacle is transmission: American Transmission Company must\n"
        "complete two 345-kilovolt upgrades before the campus can draw its full load.\n"
    )
    kept, _ = crawl._risks(
        {
            "risks": [
                {
                    "category": "transmission",
                    "severity": "material",
                    "summary": "Two upgrades must finish first.",
                    # "the campus" resolved to the site's name.
                    "quote": (
                        "The principal obstacle is transmission: American Transmission "
                        "Company must complete two 345-kilovolt upgrades before Fairwater "
                        "can draw its full load."
                    ),
                }
            ]
        },
        text,
        URL,
    )
    assert len(kept) == 1
    assert "before the campus can draw" in kept[0].quote
    assert crawl._normalize_for_match(kept[0].quote) in crawl._normalize_for_match(text)


def test_recovery_does_not_let_a_mislabelled_risk_through():
    """The category check runs against the recovered text, not the model's edit.

    That is strictly harder to satisfy: the model could otherwise smuggle the
    category's keyword into its own paraphrase of a real sentence and have the
    label accepted on the strength of a word the article never used.
    """
    text = "Microsoft will operate the campus itself and has not announced any tenants.\n"
    kept, notes = crawl._risks(
        {
            "risks": [
                {
                    "category": "financing",
                    "severity": "material",
                    "summary": "Funding looks shaky.",
                    "quote": (
                        "Microsoft will operate the campus itself and has not announced "
                        "any tenants, leaving the financing unresolved."
                    ),
                }
            ]
        },
        text,
        URL,
    )
    assert len(kept) == 1
    assert kept[0].unconfirmed == "quote_off_target"
    assert kept[0].quote is None, "the model's keyword must not be credited as evidence"
    assert notes


def test_a_real_quote_under_the_wrong_category_does_not_evidence_it(prompt):
    """The check that `_SUMMARY_FIELDS` could not make.

    "Microsoft will operate the campus itself" is a genuine sentence from the
    article, so trusting the model's label would let it evidence a financing
    collapse. `_RISK_EVIDENCE` requires the quote to actually concern the category
    it is filed under — and failing that now unpairs the quote from the risk
    rather than deleting both, because a mislabelled quote is a correction to
    make, not a source to go and find.
    """
    risk = _risk(build("llm_response_ungrounded.json", prompt=prompt)[0], "financing")
    assert risk is not None
    assert risk.unconfirmed == "quote_off_target"
    assert risk.quote is None


def test_unconfirmed_risks_are_disclosed(prompt):
    record = build("llm_response_ungrounded.json", prompt=prompt)[0]
    assert any("risk(s) kept as 待确认" in n for n in record.notes)


def test_an_unconfirmed_risk_does_not_launder_itself_into_the_blocker_field(prompt):
    """`blocker` is one of the twelve tracked fields.

    It is derived from the risk rows, so an obstacle the gate refused would
    otherwise arrive in `source.fields` reading as cited — counted by confidence
    and by the 9-of-12 measure. It may still *fill* the column, since a 待确认
    value is better than nothing; it may not be called confirmed.
    """
    record = build("llm_response_ungrounded.json", prompt=prompt)[0]
    source = record.sources[0]
    assert all(r.unconfirmed for r in record.risks), "fixture premise"
    assert source.claims.get("blocker")
    assert "blocker" in source.unconfirmed


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


# --- Refusing a page that is not an article ---------------------------------

#: The Applied Digital campus-update card, as fetched. 598 characters, of which
#: the only sentence is a site-wide banner about a *different* campus — which is
#: precisely the project the model invented and then could not evidence.
TEASER = """\
Applied Digital

Applied Digital has signed a 210 MW lease at Delta Forge 2... Read More >>

INSIGHTS

COMPANY

INVESTORS

Contact

Contact

< Back

Video

POLARIS FORGE 1 CAMPUS UPDATE | MAY 2026

<< Return to Insights

Awards & Recognition

Company

About Leadership Careers Contact Applied Digital CARES

Solutions

Digital Infrastructure

AI Factories

Insights

Insights

Behind The Build

Investors

Connect

Subscribe to Applied Digital Email Updates

Thank you for subscribing!

Oops! Something went wrong while submitting the form.

© 2026 — Copyright

Terms & Conditions

Privacy Policy
"""

#: A real Meta 8-K excerpt: 590 characters, eight fewer than the teaser above.
#: Raw length cannot tell these two apart, which is why the floor is on prose.
SHORT_FILING = """\
Meta — SEC 8-K filed 2026-04-29

We continue to expect to deliver operating income this year that is above 2025 operating income.

We anticipate 2026 capital expenditures, including principal payments on finance leases, to be \
in the range of $125-145 billion, increased from our prior range of $115-135 billion. This \
reflects our expectations for higher component pricing this year and, to a lesser extent, \
additional data center costs to support future year capacity.

Absent any changes to our tax landscape, we expect our tax rate for the remaining quarters of \
2026 to be between 13-16%.
"""


def test_a_teaser_card_is_refused_before_it_costs_an_llm_call(prompt, caplog):
    """The largest single share of the reported symptom, and the cheapest fix.

    Every quote failing together on one page is what a nav-only body looks like:
    the model has a title and nothing to quote, so it invents a project and the
    gate correctly refuses all of it — after the call has been paid for, and
    after `build_records` has restored the identity fields and written the row.
    """
    caplog.set_level("WARNING")
    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    outcome = crawl.extract_one(fetched(markdown=TEASER), prompt=prompt, extractor=llm)
    assert outcome.status == "thin_content"
    assert outcome.records == []
    assert llm.seen == [], "the whole point is that nothing was spent"
    assert "not an article" in caplog.text


def test_a_short_but_real_filing_is_not_refused(prompt):
    """The stated risk in refusing short pages, and the reason the floor is on prose.

    This 8-K excerpt is *shorter* than the teaser card above — 590 characters
    against 598 — so any raw-length floor that catches the card also destroys
    real filings. It scores 553 characters of prose against the card's 74.
    """
    assert len(SHORT_FILING) < len(TEASER)
    assert crawl.prose_length(SHORT_FILING) > crawl.MIN_PROSE_CHARS
    assert crawl.prose_length(TEASER) < crawl.MIN_PROSE_CHARS

    llm = FakeLLM(['{"projects": []}'])
    outcome = crawl.extract_one(fetched(markdown=SHORT_FILING), prompt=prompt, extractor=llm)
    assert outcome.status == "no_project"


def test_prose_is_measured_in_characters_so_chinese_is_not_scored_at_zero():
    """Why the line test counts characters and not words.

    Chinese is not whitespace-delimited, so a words-per-line measure scores
    every Chinese-language page at zero and refuses it as `thin_content` —
    recording a reason that is simply false about a 4,392-character article.
    This is the body of a real repost that the character measure keeps.

    It is still the stricter direction for Chinese, because the same meaning
    fits in fewer characters; see `MIN_PROSE_CHARS` for what that costs.
    """
    chinese = (
        "IT之家 3 月 9 日消息，Oracle 甲骨文北京时间今日在 X 平台表示，近期媒体关于"
        "“星际之门”首个站点 —— 得克萨斯州阿比林 (Abilene) 园区的报道存在虚假与不实内容。\n"
        "Oracle 澄清称，该企业正与 Crusoe 紧密协作，以创纪录的速度建设阿比林站点，"
        "两栋建筑已全面投入运营，园区其余部分也按计划推进；Oracle 还已完成额外 4.5GW 的"
        "租赁签约，以兑现对 OpenAI 的承诺。\n"
        "此外，Oracle 持续评估全球各地站点，通过与优秀合作伙伴及客户的紧密协作，"
        "满足对 OCI 云服务日益增长的需求。\n"
    )
    assert crawl.prose_length(chinese) > crawl.MIN_PROSE_CHARS

    # A whitespace-delimited sentence is one "word" long, which is what the
    # measure this replaced would have scored the whole page at.
    unbroken = (
        "两栋建筑已全面投入运营，园区其余部分也按计划推进，公司持续评估全球各地站点，"
        "通过与优秀合作伙伴及客户的紧密协作，满足对云服务日益增长的需求。"
    )
    assert len(unbroken.split()) == 1
    assert crawl.prose_length(unbroken) == len(unbroken)


def test_a_refused_page_is_retryable_not_settled(session, prompt):
    """A site that serves a teaser today may serve the article tomorrow.

    `no_project` would be wrong here and would bury it: that status means a model
    read the page and found nothing, and discovery never retries it. Nothing read
    this page at all.
    """
    from tracker.ingest import discover as disc

    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    report = crawl.run(
        session,
        [URL],
        fetcher=FakeFetcher({URL: fetched(markdown=TEASER)}),
        extractor=llm,
        run_id="thin",
    )
    assert report.thin_content == 1
    assert report.written == 0

    row = session.scalar(select(IngestUrl))
    assert row.status == "thin_content"
    assert [r.url for r in disc.failed(session)] == [URL]


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


# --- capacity blocks --------------------------------------------------------
#
# The containment property is the whole point. `evidence_gate` deliberately lets
# any verified quote support any value, which recovered 89 correctly-evidenced
# values across 64 projects. At block granularity that same tolerance is the
# project-39 bug: an SEC filing's sentence about "AZP-3 Phase 3" must not be
# allowed to evidence AZP-2's capacity, because block megawatts get summed.

BLOCK_ARTICLE = (
    "Iron Mountain is building AZP-2 in Phoenix, a three-story facility spanning\n"
    "530,000 square feet and up to 48 megawatts of IT capacity at full buildout.\n"
    "Separately the company reported that AZP-3 Phase 3 will add 8 megawatts,\n"
    "fully pre-leased, in the third quarter of 2026.\n"
)


def test_a_block_is_extracted_with_its_own_quote():
    kept, _ = crawl._blocks(
        {
            "blocks": [
                {
                    "label": "AZP-2",
                    "mw": 48,
                    "status": "under_construction",
                    "evidence": [{"field": "mw", "quote": "up to 48 megawatts of IT capacity"}],
                }
            ]
        },
        BLOCK_ARTICLE,
        URL,
    )
    assert len(kept) == 1
    assert kept[0].mw == 48.0
    assert "mw" in kept[0].quotes
    assert "mw" not in kept[0].unconfirmed


def test_a_quote_about_another_tranche_cannot_evidence_this_one():
    """Project 39, as a test.

    The sentence is real, it is in the article, and it is about a different
    facility. `evidence_gate` alone would accept it — this is the check that does
    not, and the value survives as 待确认 rather than as a quoted fact.
    """
    kept, notes = crawl._blocks(
        {
            "blocks": [
                {
                    "label": "AZP-2",
                    "mw": 8,
                    "status": "planned",
                    # Verbatim from the article, and about AZP-3.
                    "evidence": [{"field": "mw", "quote": "AZP-3 Phase 3 will add 8 megawatts"}],
                }
            ]
        },
        BLOCK_ARTICLE,
        URL,
    )
    assert len(kept) == 1, "the block is kept; only its evidence is refused"
    assert "mw" in kept[0].unconfirmed, "a quote naming another tranche must not confirm this one"
    assert "mw" not in kept[0].quotes
    assert notes, "the operator is told the figure is unquoted"


def test_evidence_pools_are_sealed_between_blocks():
    """Two blocks, and neither may reach into the other's evidence.

    Without this the 8 MW quote confirms AZP-2 and the 48 MW quote confirms AZP-3,
    and both blocks end up with a real citation for the wrong number.
    """
    kept, _ = crawl._blocks(
        {
            "blocks": [
                {
                    "label": "AZP-2",
                    "mw": 48,
                    "evidence": [{"field": "mw", "quote": "up to 48 megawatts of IT capacity"}],
                },
                {
                    "label": "AZP-3 Phase 3",
                    "mw": 8,
                    "evidence": [{"field": "mw", "quote": "AZP-3 Phase 3 will add 8 megawatts"}],
                },
            ]
        },
        BLOCK_ARTICLE,
        URL,
    )
    by_label = {b.label: b for b in kept}
    assert by_label["AZP-2"].mw == 48.0
    assert by_label["AZP-3 Phase 3"].mw == 8.0
    assert "mw" in by_label["AZP-3 Phase 3"].quotes


def test_an_ordinal_and_a_type_word_are_enough_to_name_a_block():
    """ "The first phase 8 megawatts" has to evidence a block labelled "Phase 1".

    This is the second acceptance arm, and without it the commonest phrasing in
    the corpus cannot cite the block it describes.
    """
    article = "The first phase 8 megawatts of customer capacity is targeted for Q3 2026.\n"
    kept, _ = crawl._blocks(
        {
            "blocks": [
                {
                    "label": "Phase 1",
                    "mw": 8,
                    "evidence": [{"field": "mw", "quote": "The first phase 8 megawatts"}],
                }
            ]
        },
        article,
        URL,
    )
    assert kept[0].mw == 8.0
    assert "mw" in kept[0].quotes, "an ordinal plus a type word names the block"


def test_a_fabricated_block_quote_is_still_refused():
    """The anti-fabrication guarantee is unchanged at block granularity."""
    kept, _ = crawl._blocks(
        {
            "blocks": [
                {
                    "label": "AZP-2",
                    "mw": 900,
                    "evidence": [{"field": "mw", "quote": "AZP-2 will reach 900 megawatts"}],
                }
            ]
        },
        BLOCK_ARTICLE,
        URL,
    )
    assert "mw" in kept[0].unconfirmed
    assert kept[0].quotes == {}


def test_no_blocks_key_is_the_common_and_correct_answer():
    """A campus an article treats as one thing is one thing."""
    assert crawl._blocks({}, BLOCK_ARTICLE, URL) == ([], [])
    assert crawl._blocks({"blocks": []}, BLOCK_ARTICLE, URL) == ([], [])
    assert crawl._blocks({"blocks": "nonsense"}, BLOCK_ARTICLE, URL) == ([], [])


def test_two_labels_resolving_to_one_block_keep_the_first():
    """The UNIQUE would abort the whole article; dedup in Python instead."""
    kept, _ = crawl._blocks(
        {"blocks": [{"label": "Phase 1", "mw": 10}, {"label": "phase one", "mw": 20}]},
        BLOCK_ARTICLE,
        URL,
    )
    assert len(kept) == 1


def test_an_unusable_status_falls_back_rather_than_failing():
    kept, _ = crawl._blocks({"blocks": [{"label": "AZP-2", "status": "vibes"}]}, BLOCK_ARTICLE, URL)
    assert kept[0].status == "planned"


def test_a_runaway_block_list_is_truncated_and_disclosed():
    entries = [{"label": f"Phase {n}"} for n in range(1, 13)]
    kept, notes = crawl._blocks({"blocks": entries}, BLOCK_ARTICLE, URL)
    assert len(kept) == crawl.MAX_BLOCKS_PER_PROJECT
    assert any("further block" in n for n in notes)


def test_a_real_quote_that_does_not_state_the_value_keeps_no_quote():
    """The gate verified the sentence; it does not evidence *this* number.

    `evidence_gate` records a quote for every labelled evidence entry, including
    ones whose value it then discards. Pairing one with a 待确认 figure would dress
    an unconfirmed value as a quoted fact — the one thing the tier exists to stop —
    so the block keeps the same filter `SourceRecord.quotes` applies.
    """
    kept, _ = crawl._blocks(
        {
            "blocks": [
                {
                    "label": "AZP-2",
                    "mw": 999,
                    # Verbatim, and about AZP-2 — but it says 48, not 999.
                    "evidence": [{"field": "mw", "quote": "up to 48 megawatts of IT capacity"}],
                }
            ]
        },
        BLOCK_ARTICLE,
        URL,
    )
    assert kept[0].mw == 999.0, "the value survives as a candidate"
    assert "mw" in kept[0].unconfirmed
    assert kept[0].quotes == {}, "no quote may vouch for a figure it does not state"


# --- ARTICLE_DATE -----------------------------------------------------------
#
# RULE 5 resolves relative timing ("construction starts next year") against
# ARTICLE_DATE, and with the date unknown it correctly forces every such phrase
# to null. So an absent date fabricates nothing — it quietly costs schedule
# fields instead, which is the harder failure to notice.


def test_the_article_date_reaches_the_prompt(session):
    """It never had. `extract_one`'s parameter defaulted to "unknown" and no
    caller ever passed it, while `discover` had been recording the date on
    `ingest_url` the whole time.
    """
    session.add(
        IngestUrl(
            url=URL, run_id="t", status="discovered", published_at=dt.datetime(2026, 1, 15, 9, 30)
        )
    )
    session.flush()

    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    crawl.run(session, [URL], fetcher=FakeFetcher({URL: fetched()}), extractor=llm, run_id="t1")

    _, user = llm.seen[0]
    assert "ARTICLE_DATE: 2026-01-15" in user


def test_a_url_with_no_recorded_date_says_unknown_rather_than_guessing(session):
    """A hand-supplied URL has no publication date anywhere.

    "unknown" is the honest answer and the prompt already knows what to do with
    it; inventing one — today's date, say — would turn every "next year" in the
    article into a fabricated schedule.
    """
    llm = FakeLLM([canned("llm_response_microsoft_wi.json")])
    crawl.run(session, [URL], fetcher=FakeFetcher({URL: fetched()}), extractor=llm, run_id="t1")

    _, user = llm.seen[0]
    assert "ARTICLE_DATE: unknown" in user
