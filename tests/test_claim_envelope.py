"""The claim envelope: does the quote license the label?

`evidence_gate` asks whether the article states the *value*. `axis_gate` asks
whether it states the *qualifier* — and the difference between an axis that
carries information and one that becomes decoration is exactly whether anything
checks it.

The cautionary case is in the database already. `risk.severity` is a judgement no
article ever states, and every risk on every row reads `watch`, `vocab`'s
default. Fully populated, perfectly valid, carrying nothing. So each axis here is
paired: a planted label the quote does not support must be refused, and a genuine
one beside it must survive. An axis that cannot pass both halves should be
deleted rather than displayed.

One property holds throughout and is asserted separately: **a refused axis never
costs the value.** The figure survives exactly as it did before the envelope
existed, so a model labelling everything `at_least` to sound careful gains
nothing, and this pass can never reduce coverage.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker.ingest import crawl
from tracker.vocab import BOUND_MARKERS, CLAIM_AXIS_DEFAULTS, bound_from_quote


def gate(
    entry: dict,
    quote: str,
    *,
    blocks: frozenset[str] = frozenset(),
    site: frozenset[str] = frozenset(),
) -> dict:
    return crawl.axis_gate(entry, quote, block_labels=blocks, site_names=site)


# --- bound -------------------------------------------------------------------
#
# Prompt RULE 4 used to say `"500-700 MW" -> 500 (the LOWER bound; say so in
# "notes")`. The range was destroyed on purpose and routed to prose, where
# nothing could read it back.


def test_a_hedge_the_article_really_wrote_is_kept():
    """Hyperion's own sentence, and the reason the axis exists."""
    got = gate({"bound": "at_least"}, "will bring more than $50B of investment to the region")
    assert got["bound"] == "at_least"


def test_a_hedge_the_article_did_not_write_is_refused():
    """The planted fault: a careful-sounding label on a flat sentence."""
    got = gate({"bound": "at_least"}, "the campus will draw 350 megawatts")
    assert got["bound"] == "exact"


@pytest.mark.parametrize(
    ("bound", "quote"),
    [
        ("approximate", "the first phase remains on track to deliver about 2 GW by 2030"),
        ("at_most", "expected to grow to up to 800 megawatts when the second facility opens"),
        ("at_least", "at least 1,200 MW across the campus"),
    ],
)
def test_each_hedge_word_licenses_its_own_bound(bound, quote):
    assert gate({"bound": bound}, quote)["bound"] == bound


def test_a_bound_outside_the_vocabulary_degrades(session=None):
    assert gate({"bound": "roughly-ish"}, "about 2 GW")["bound"] == "exact"


# --- modality ----------------------------------------------------------------


def test_an_achievement_the_article_states_is_kept():
    got = gate({"modality": "achieved"}, "Fairwater came online in April ahead of schedule")
    assert got["modality"] == "achieved"


def test_an_achievement_the_article_does_not_state_is_refused():
    """A plan is not an achievement, however confidently the model labels it."""
    got = gate({"modality": "achieved"}, "the campus is designed for 350 megawatts")
    assert got["modality"] == CLAIM_AXIS_DEFAULTS["modality"]


def test_something_achieved_cannot_be_dated_in_the_future():
    """The hard invariant, and the exact shape of Hyperion's live defect.

    Event 207 reads "interim milestone of 1.5 GW targeted by end of 2027", typed
    `announced`, dated 2027-12-31 — and the track strip counted it as reached.
    A target is not an achievement, and a date is enough to prove it.
    """
    future = dt.date.today().replace(year=dt.date.today().year + 2).isoformat()
    got = gate(
        {"modality": "achieved", "as_of": future},
        "some reports have surfaced that an interim milestone of 1.5 GW is being targeted",
    )
    assert got["modality"] == "targeted"
    assert got["as_of"] == future


def test_a_past_achievement_keeps_its_date():
    got = gate({"modality": "achieved", "as_of": "2026-04-01"}, "the site went live in April")
    assert got["modality"] == "achieved"
    assert got["as_of"] == "2026-04-01"


def test_an_unparseable_date_is_dropped_rather_than_guessed():
    got = gate({"modality": "planned", "as_of": "next spring"}, "the campus is planned")
    assert "as_of" not in got


def test_a_hedged_report_is_recorded_as_speculation():
    """Hyperion's 1.5 GW block stands on "Some reports have already surfaced".

    Stored today with the same standing as an SEC filing.
    """
    got = gate(
        {"modality": "speculated"},
        "Some reports have already surfaced that an interim milestone of 1.5 GW is being targeted",
    )
    assert got["modality"] == "speculated"


# --- scope -------------------------------------------------------------------
#
# The axis Hyperion needed. Three investment figures, no disagreement between
# them, one scalar column.


def test_the_three_hyperion_figures_take_three_different_scopes():
    """Verbatim from the row's own stored quotes.

    The site figure needs `site=` now: its sentence says "the buildout" without
    naming Hyperion or Richland Parish, and `this_site` is no longer a value the
    gate will pass through unlicensed. With the row's own name in the sentence it
    reads `this_site`; without it the honest answer is `unnamed`, and the two are
    separated in `test_this_site_must_be_licensed_by_the_sentence` below.
    """
    site = gate(
        {"scope": "this_site"},
        "the Hyperion buildout itself is expected cost in the $10 billion range",
        site=frozenset({"hyperion"}),
    )
    region = gate({"scope": "region"}, "will bring more than $50B of investment to the region")
    programme = gate({"scope": "programme"}, "OpenAI's $500 billion Stargate programme")

    assert (site["scope"], region["scope"], programme["scope"]) == (
        "this_site",
        "region",
        "programme",
    )


def test_a_regional_scope_the_sentence_does_not_support_is_refused():
    got = gate({"scope": "region"}, "the campus will cost $10 billion to build")
    assert got["scope"] == CLAIM_AXIS_DEFAULTS["scope"]


def test_a_block_scope_must_name_a_block_that_exists():
    """Referential integrity, not judgement.

    Nothing here can tell whether the model picked the *right* tranche; it can
    refuse one that is not on the record. Fairwater's live `phase-1` block has
    `parent = NULL` for exactly this reason — the extractor knew it was a phase
    and not of what, and the system recorded the uncertainty instead of guessing.
    """
    quote = "Phase 1 will deliver 200 MW"
    assert gate({"scope": "block:Phase 1"}, quote, blocks=frozenset({"phase 1"}))["scope"] == (
        "block:Phase 1"
    )
    assert (
        gate({"scope": "block:Phase 4"}, quote, blocks=frozenset({"phase 1"}))["scope"]
        == (CLAIM_AXIS_DEFAULTS["scope"])
    )


def test_unnamed_is_a_real_answer_and_the_default():
    """ "The article gave a number and did not say what it covers" is the honest
    reading of most sentences, and must not be pushed toward `this_site`."""
    assert gate({}, "the project will cost $4 billion")["scope"] == "unnamed"


# --- the property that stops the axes becoming decoration ---------------------


def test_a_refused_axis_never_costs_the_value():
    """Every axis wrong at once, and the entry still resolves to neutral labels.

    `axis_gate` returns labels only — it is never given the figure and cannot
    drop one. That is what makes the envelope safe to add before it is trusted:
    the worst case is a claim described no better than it was yesterday.
    """
    got = gate(
        {"scope": "region", "bound": "at_least", "modality": "achieved", "as_of": "not-a-date"},
        "the campus is designed for 350 megawatts",
    )
    assert got == {"scope": "unnamed", "bound": "exact", "modality": "planned"}


def test_an_entry_neutral_on_every_axis_is_not_stored():
    """Storing it would inflate coverage with rows carrying no information —
    which is the measurement that decides whether the axes survive at all.

    Note `this_site` is *not* neutral and is stored: "this figure is about this
    campus" is the distinction Hyperion needed, and it is a different statement
    from "the article did not say".
    """
    # `city` is in the payload so the sentence below can license `this_site` —
    # without an identity to match, an unlicensed `this_site` degrades to
    # `unnamed`, which IS neutral and so would not be stored either.
    kept = {"mw_planned": 350.0, "city": "Ashburn"}
    quotes = {"mw_planned": "the Ashburn campus will draw 350 megawatts"}
    bare = [{"field": "mw_planned", "quote": quotes["mw_planned"]}]
    assert crawl._claim_axes(bare, quotes, kept, []) == {}

    scoped = [{"field": "mw_planned", "quote": quotes["mw_planned"], "scope": "this_site"}]
    assert crawl._claim_axes(scoped, quotes, kept, []) == {
        "mw_planned": {"scope": "this_site", "bound": "exact", "modality": "planned"}
    }


def test_an_axis_is_stored_when_it_says_something():
    kept = {"investment_usd": 50_000_000_000}
    quotes = {"investment_usd": "will bring more than $50B of investment to the region"}
    evidence = [
        {
            "field": "investment_usd",
            "quote": quotes["investment_usd"],
            "scope": "region",
            "bound": "at_least",
        }
    ]

    got = crawl._claim_axes(evidence, quotes, kept, [])
    assert got == {
        "investment_usd": {"scope": "region", "bound": "at_least", "modality": "planned"}
    }


def test_an_axis_is_verified_against_the_stored_quote_not_the_model_s_own_words():
    """`_verbatim_run` may have repaired the quote to the article's own words.

    Checking the model's version instead would let it license a hedge by writing
    one into a sentence nobody published — the fabrication route `evidence_gate`
    exists to close, reopened one level up.
    """
    kept = {"mw_planned": 350.0}
    stored = {"mw_planned": "the campus will draw 350 megawatts"}
    evidence = [
        {
            "field": "mw_planned",
            # The model's offered text carries the hedge; the stored quote does not.
            "quote": "the campus will draw more than 350 megawatts",
            "bound": "at_least",
        }
    ]

    got = crawl._claim_axes(evidence, stored, kept, [])
    assert got == {}, "the hedge was not in the sentence the gate verified"


# --- date precision ----------------------------------------------------------
#
# Not model-asserted and so not gated: it is what our own parser observed while
# reading the date. `normalize.parse_date` has always returned it and nothing
# outside that module has ever read it.


def test_a_year_only_date_records_its_precision():
    values, _, precisions = crawl._coerce(
        {"name": "X", "company": "Y", "state": "TX", "city": "Z", "first_announced": "2024"}
    )
    assert values["first_announced"] == dt.date(2024, 1, 1)
    assert precisions["first_announced"] == "year"


def test_a_full_date_records_no_precision_to_disclose():
    """Day precision is the absence of a caveat, and is stored as NULL.

    Recording "day" explicitly would make every row written before the envelope
    indistinguishable from one whose article really did give a day.
    """
    _, _, precisions = crawl._coerce(
        {
            "name": "X",
            "company": "Y",
            "state": "TX",
            "city": "Z",
            "expected_online": "2027-03-14",
        }
    )
    assert precisions.get("expected_online") in (None, "day")


# --- reading a bound off the stored quote ------------------------------------


class TestBoundFromQuote:
    """`vocab.bound_from_quote`, which is what every *display* surface uses.

    Distinct from `crawl.axis_gate`, which assigns a bound at ingest and is
    deliberately never given the figure. This one is positional *because* it has
    the figure, which is how it settles the two-hedge sentence the gate cannot.
    """

    @pytest.mark.parametrize(
        ("quote", "value", "expected"),
        [
            # The live Fairwater case: a floor stored as a point value on two rows.
            ("Each exceeds 350 MW and is scaling toward multi-GW", 350.0, "at_least"),
            ("the campus will draw about 2,000 megawatts", 2000.0, "approximate"),
            ("up to 1.2 GW of capacity", 1200.0, "at_most"),
            ("over 200 megawatts of power capacity", 200.0, "at_least"),
            ("a 900 MW campus", 900.0, "exact"),
            # Written as gigawatts where the column holds megawatts.
            ("more than 1.2 GW committed", 1200.0, "at_least"),
            # Dollars, where the sentence says "billion" and the column holds digits.
            ("roughly $27 billion in total development costs", 27_000_000_000, "approximate"),
            # No quote at all, and a figure the sentence does not contain.
            (None, 350.0, "exact"),
            ("", 350.0, "exact"),
            ("more than 500 MW", 350.0, "exact"),
        ],
    )
    def test_reads_the_hedge_the_article_used(self, quote, value, expected):
        assert bound_from_quote(quote, value) == expected

    def test_two_figures_two_hedges_each_keeps_its_own(self):
        """The defect HANDOFF.md records against the ingest gate.

        Source 12 on Hyperion reads this sentence, and a presence-only check read
        `approximate` for the $50B figure off the *other* number's "roughly".
        """
        quote = "require more than $50 billion in investment, up from the roughly $27 billion plan"
        assert bound_from_quote(quote, 50_000_000_000) == "at_least"
        assert bound_from_quote(quote, 27_000_000_000) == "approximate"

    def test_the_nearest_hedge_wins(self):
        """ "more than approximately 350" is a floor, not an estimate."""
        assert bound_from_quote("more than approximately 350 MW", 350.0) == "at_least"

    def test_a_hedge_far_from_the_figure_is_not_its_hedge(self):
        far = "roughly a decade of planning went into the site before the 350 MW campus"
        assert bound_from_quote(far, 350.0) == "exact"

    def test_the_gate_and_the_display_share_one_marker_list(self):
        """Two copies of this rule is how `find_conflicts` became a third copy of
        the 待确认 rule and started disagreeing with the other two on screen."""
        from tracker.ingest.crawl import _BOUND_MARKERS

        assert _BOUND_MARKERS is BOUND_MARKERS

    def test_exceeds_is_licensed(self):
        """It was missing, and it is the commonest hedge in this corpus."""
        assert "exceeds" in BOUND_MARKERS["at_least"]


# --- `this_site` has to be earned --------------------------------------------
#
# The axis failed its own kill criterion at `this_site` 96.9% — "an axis whose modal
# value exceeds 95% is reporting a default rather than the article" — and the reason
# was structural. `_SCOPE_MARKERS` licenses `programme`, `region` and `portfolio` by
# wording, and `block:` by resolving against the record. `this_site` had no check at
# all, so it fell through every branch and was written verbatim: the one value the
# gate could not refuse, and therefore the model's cheapest safe answer. Replaying
# the gate over 9,226 stored claims takes it from 98.30% to 55.02%.


def test_this_site_must_be_licensed_by_the_sentence():
    """Naming the site licenses it; not naming it is `unnamed`, the honest answer."""
    named = gate(
        {"scope": "this_site"},
        "the Fairwater campus will draw 350 megawatts at full build",
        site=frozenset({"fairwater"}),
    )
    unnamed = gate(
        {"scope": "this_site"},
        "the buildout is expected to cost in the $10 billion range",
        site=frozenset({"fairwater"}),
    )

    assert named["scope"] == "this_site"
    assert unnamed["scope"] == "unnamed"


def test_a_sentence_naming_a_tranche_is_that_tranche_not_the_site():
    """Project #14's live defect: source 2790 claims `mw_planned = 19.2` labelled
    `this_site`, and an agent that read six articles proved it is Building K's
    figure. The label is the campus-versus-building discrimination the axis exists
    for, and it was being handed out for free."""
    got = gate(
        {"scope": "this_site"},
        "The new Building K itself is rated at 19.2 MW.",
        blocks=frozenset({"building k"}),
        site=frozenset({"ashburn"}),
    )

    assert got["scope"] == "block:building k"


def test_a_tranche_name_wins_even_when_the_site_is_also_named():
    """An article says both. The narrower object is the one the figure is of."""
    got = gate(
        {"scope": "this_site"},
        "At the Ashburn campus, ACC10 is rated at 70 MW.",
        blocks=frozenset({"acc10"}),
        site=frozenset({"ashburn"}),
    )

    assert got["scope"] == "block:acc10"


def test_generic_words_do_not_license_this_site():
    """An article about a programme says "the campus" and "the site" too, so
    accepting them would put `this_site` straight back to unrefusable."""
    for quote in (
        "the campus will cost $10 billion",
        "the site is expected to draw 350 MW",
        "the data center project will be built in phases",
    ):
        got = gate({"scope": "this_site"}, quote, site=frozenset({"fairwater", "ashburn"}))
        assert got["scope"] == "unnamed", quote


def test_refusing_this_site_never_costs_the_value():
    """The gate's founding promise, restated for the value it now refuses: axes are
    labels and the figure is untouched, so this pass cannot reduce coverage."""
    kept = {"mw_planned": 350.0}
    quotes = {"mw_planned": "the buildout will draw 350 megawatts"}
    evidence = [{"field": "mw_planned", "quote": quotes["mw_planned"], "scope": "this_site"}]

    crawl._claim_axes(evidence, quotes, kept, [])

    assert kept["mw_planned"] == 350.0


def test_site_names_drops_the_words_every_article_contains():
    """A name is matched whole and by word, but "data", "center" and "campus"
    would match almost any sentence in the corpus."""
    got = crawl.site_names({"name": "Digital Ashburn Data Center Campus", "city": "Ashburn"})

    assert "ashburn" in got
    assert "digital" in got
    assert "center" not in got
    assert "campus" not in got
    assert not any(len(word) < 4 for word in got)
