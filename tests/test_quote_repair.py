"""What a quote may be, and what the evidence gate reads in one.

Two kinds of loss this guards against, both measured on a replay of 40 articles:

* **Short exact quotes refused.** The run floors were written for *edited* quotes
  and applied to exact ones too, so a dateline or a company's legal name was
  refused everywhere except the field gate, which happened to test containment
  first. 46 of 461 quotes in the replay were exact and under 40 characters.
* **Sentences that state the value read as not stating it.** "A 60-megawatt data
  center", "in 2017", "announced in 2021" against a date the model wrote to the
  day: 24 of 51 "wrong sentence" refusals in the replay were the checker's.

And the one thing loosening must not buy: a quote that does not belong to the
article, or a figure the model changed, getting through.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker.ingest import crawl
from tracker.normalize import norm_money_detail, norm_mw_detail

ARTICLE = (
    "Denver, CO - November 28, 2018\n\n"
    "Core Scientific is a leader in digital infrastructure for high-density colocation, "
    "operating dedicated, purpose-built facilities designed to support artificial "
    "intelligence workloads across the United States.\n\n"
    "Facility | Status\n"
    "Dalton, GA ~175 MW Est. completion: early 2027\n\n"
    "Construction crews broke ground on the site in 2023, the county said.\n"
    "The seller was Broderick Acquisition Company, LLC.\n"
)

UNRELATED = (
    "Residents packed the county board meeting on Tuesday to oppose a proposed "
    "warehouse near the river. The developer said traffic studies were complete and "
    "that construction could begin next spring if the board approves the rezoning.\n"
)


# --- exact quotes ------------------------------------------------------------------


@pytest.mark.parametrize(
    "quote",
    ["Denver, CO - November 28, 2018", "Broderick Acquisition Company, LLC", "~175 MW"],
)
def test_a_short_exact_quote_is_accepted_as_its_whole_line(quote):
    run = crawl._verbatim_run(quote, ARTICLE)
    assert run.text is not None
    assert quote in run.text


def test_a_bare_token_is_not():
    assert crawl._verbatim_run("2023", ARTICLE).text is None


def test_a_milestone_with_a_short_exact_quote_is_confirmed():
    """`_events` goes straight to `_verbatim_run`; this used to come back unverified."""
    [event] = crawl._events(
        {
            "events": [
                {
                    "event_date": "2018-11-28",
                    "event_type": "announced",
                    "description": "Announced.",
                    "quote": "Denver, CO - November 28, 2018",
                }
            ]
        },
        ARTICLE,
        "https://news.example.com/a",
    )
    assert event.unconfirmed is None
    assert event.quote == "Denver, CO - November 28, 2018"


# --- edited quotes -----------------------------------------------------------------


def test_one_changed_word_in_a_long_quote_is_repaired_to_the_articles_words():
    """106 of 226 characters in one unbroken stretch: refused by the run floor."""
    edited = (
        "Core Scientific is a leader in digital infrastructure for high-density colocation, "
        "running dedicated, purpose-built facilities designed to support artificial "
        "intelligence workloads across the United States."
    )
    run = crawl._verbatim_run(edited, ARTICLE)
    assert run.text is not None
    assert "operating dedicated" in run.text
    assert "running" not in run.text


def test_an_edited_table_line_is_repaired():
    """35 of 45 characters: a short line could never reach the 40-character floor."""
    run = crawl._verbatim_run("Dalton, GA ~175 MW Est. completion early 2027", ARTICLE)
    assert run.text == "Dalton, GA ~175 MW Est. completion: early 2027"


def test_a_changed_figure_is_not_repaired_into_the_articles_figure():
    """97% coverage, and the one character that differs is the fact."""
    assert crawl._verbatim_run("broke ground on the site in 2024", ARTICLE).text is None


@pytest.mark.parametrize(
    "quote",
    [
        "Residents packed the county board meeting on Tuesday to oppose a data center.",
        "The developer said construction could begin next spring.",
        "Construction crews broke ground on the warehouse in 2023, the board said.",
    ],
)
def test_a_quote_from_another_article_is_refused(quote):
    """The negative control: a near-copy of a sentence that is not in this article."""
    assert crawl._verbatim_run(quote, ARTICLE).text is None


def test_a_quote_is_not_matched_into_an_unrelated_article():
    edited = "Core Scientific is a leader in digital infrastructure for colocation."
    assert crawl._verbatim_run(edited, UNRELATED).text is None


# --- reading the value -------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "sentence"),
    [
        ("mw_planned", 60.0, "Nautilus introduced the planned 60-megawatt data center."),
        ("mw_planned", 2000.0, "what would be Meta's largest data center: the 2-gigawatt campus"),
        ("mw_planned", 300.0, "a 300-MW building"),
        ("investment_usd", 2_000_000_000, "a $2-billion project"),
        ("first_announced", dt.date(2018, 11, 28), "Denver, CO - November 28, 2018"),
        (
            "first_announced",
            dt.date(2017, 1, 1),
            "Since breaking ground in 2017, Meta has invested.",
        ),
        (
            "expected_online",
            dt.date(2019, 10, 1),
            "construction is projected to be completed in Q4 2019",
        ),
    ],
)
def test_the_checker_reads_what_news_prose_writes(field, value, sentence):
    assert crawl._stated_in(field, value, sentence)


def test_a_hyphen_between_two_numbers_is_still_a_range():
    parsed = norm_mw_detail("100-200 MW")
    assert parsed.value == 100.0
    assert "range" in (parsed.note or "")
    assert norm_money_detail("$2-billion").value == 2_000_000_000


def test_a_date_stated_by_year_is_kept_as_the_year():
    """ "Announced in 2021" against 2021-06-01: the month was never published."""
    sentence = "Project announced in 2021, cancelled in April 2025."
    precisions = {"first_announced": "day"}
    kept, quotes, dropped = crawl.evidence_gate(
        {"first_announced": dt.date(2021, 6, 1)},
        [{"field": "first_announced", "quote": sentence}],
        sentence,
        precisions,
    )
    assert kept == {"first_announced": dt.date(2021, 1, 1)}
    assert quotes["first_announced"] == sentence
    assert precisions["first_announced"] == "year"
    assert dropped == {}


def test_a_date_outside_what_the_sentence_states_is_still_refused():
    sentence = "Project announced in 2021, cancelled in April 2025."
    kept, _, dropped = crawl.evidence_gate(
        {"first_announced": dt.date(2022, 6, 1)},
        [{"field": "first_announced", "quote": sentence}],
        sentence,
    )
    assert kept == {}
    assert dropped == {"first_announced": "quote_off_target"}


def test_a_half_year_keeps_its_own_start():
    sentence = "The campus is expected online in the second half of 2027."
    kept, _, _ = crawl.evidence_gate(
        {"expected_online": dt.date(2027, 9, 1)},
        [{"field": "expected_online", "quote": sentence}],
        sentence,
    )
    assert kept == {"expected_online": dt.date(2027, 7, 1)}
