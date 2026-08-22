"""Operator coverage: who the roster says we should have, against who we do.

The load-bearing assertions here are the matching ones. Coverage is only useful if
"Nebius" finds a row stored as "Nebius Group N.V." and does *not* fold Cipher
Mining into a row filed as "Cipher"; the first failure hides a gap that exists and
the second invents one that does not.
"""

from __future__ import annotations

import pytest

from tracker import roster
from tracker.models import Project


def _project(session, company: str, name: str, *, mw: float | None = 300.0, state: str = "TX"):
    project = Project(
        name=name,
        company=company,
        city=f"Town {name.lower().replace(' ', '-')}",
        state=state,
        dedup_key=f"roster|{company}|{name}".lower(),
        phase="construction",
        confidence=2,
        mw_planned=mw,
    )
    session.add(project)
    session.flush()
    return project


def _roster(tmp_path, body: str):
    path = tmp_path / "operators.toml"
    path.write_text(body, encoding="utf-8")
    return path


# --- Loading ----------------------------------------------------------------


def test_the_shipped_roster_loads_and_is_all_valid_kinds():
    operators = roster.load()
    assert len(operators) > 40, "the shipped roster should cover the industry, not a sample"
    assert {op.kind for op in operators} <= set(roster.KINDS)
    assert any(op.name == "Nebius" for op in operators), (
        "Nebius is the operator this whole mechanism was built for"
    )


def test_every_edgar_company_is_also_rostered():
    """The two lists overlap on the public names, and must not drift.

    `edgar-companies.toml` is scoped by CIK and cannot hold private operators, so
    it is a subset — but a company worth reading filings for is a company worth
    measuring coverage of, and the failure mode this catches is exactly Nebius:
    present in one file, absent from the other, therefore never noticed.

    Utilities and contractors are excluded: they do not own campuses, so their
    absence from the roster is the design rather than a gap.
    """
    from tracker.ingest.edgar import load_companies

    companies, _, _ = load_companies()
    operators = roster.load()
    missing = [
        company.name
        for company in companies
        if company.kind not in {"utility", "contractor"}
        and not any(op.matches(company.name) for op in operators)
    ]
    assert missing == [], f"in edgar-companies.toml but not in operators.toml: {missing}"


def test_roster_order_is_the_files_order(tmp_path):
    path = _roster(
        tmp_path,
        '[[operator]]\nname = "Zeta"\nkind = "neocloud"\n'
        '[[operator]]\nname = "Alpha"\nkind = "neocloud"\n',
    )
    assert [op.name for op in roster.load(path)] == ["Zeta", "Alpha"]


def test_a_missing_roster_says_where_it_looked(tmp_path):
    with pytest.raises(roster.RosterError, match=r"operators\.toml"):
        roster.load(tmp_path / "absent.toml")


def test_invalid_toml_is_reported_as_such(tmp_path):
    with pytest.raises(roster.RosterError, match="not valid TOML"):
        roster.load(_roster(tmp_path, "[[operator]\nname = "))


def test_an_empty_roster_is_an_error(tmp_path):
    with pytest.raises(roster.RosterError, match="no \\[\\[operator\\]\\] entries"):
        roster.load(_roster(tmp_path, "# nothing here\n"))


def test_an_unknown_kind_fails_at_load(tmp_path):
    """A typo would otherwise make an operator invisible to every --kind report."""
    body = '[[operator]]\nname = "Somebody"\nkind = "hyperscalar"\n'
    with pytest.raises(roster.RosterError, match="hyperscalar"):
        roster.load(_roster(tmp_path, body))


def test_a_duplicate_operator_is_refused(tmp_path):
    body = (
        '[[operator]]\nname = "Nebius"\nkind = "neocloud"\n'
        '[[operator]]\nname = "Nebius Group Ltd"\nkind = "neocloud"\n'
    )
    with pytest.raises(roster.RosterError, match="alias"):
        roster.load(_roster(tmp_path, body))


def test_aliases_must_be_a_list(tmp_path):
    body = '[[operator]]\nname = "Somebody"\nkind = "neocloud"\naliases = "Someone"\n'
    with pytest.raises(roster.RosterError, match="list"):
        roster.load(_roster(tmp_path, body))


# --- Matching ---------------------------------------------------------------


def test_identity_drops_the_words_every_operator_shares():
    assert roster.identity("Aligned DataCenters") == frozenset({"aligned"})
    assert roster.identity("Compass Data Centers") == frozenset({"compass"})
    assert roster.identity("H5 Data Centers") == frozenset({"h5"})
    assert roster.identity("The Data Centers LLC") == frozenset(), (
        "a name made only of generic words identifies nobody"
    )


@pytest.mark.parametrize(
    ("stored", "how"),
    [
        ("Nebius", "exact"),
        ("Nebius Group N.V.", "loose"),
        ("NEBIUS GROUP", "exact"),  # via the alias, normalized
        ("Nebius AI Cloud", "loose"),
    ],
)
def test_one_operator_finds_its_own_spellings(stored, how):
    operator = roster.Operator(name="Nebius", kind="neocloud", aliases=("Nebius Group",))
    assert operator.matches(stored) == how


def test_matching_is_one_directional():
    """The long name finds the longer row; the short row does not claim the name.

    "Cipher Mining" must find "Cipher Mining Inc." and must not match a bare
    "Cipher", which could be anybody. A wrong fold is the mistake nothing
    downstream detects.
    """
    operator = roster.Operator(name="Cipher Mining", kind="neocloud")
    assert operator.matches("Cipher Mining Inc.") is not None
    assert operator.matches("Cipher") is None
    assert operator.matches("Cipher Digital Inc.") is None


def test_a_generic_stored_name_matches_nothing():
    operator = roster.Operator(name="Prime Data Centers", kind="landlord")
    assert operator.matches("The Data Centers LLC") is None


def test_distinct_operators_sharing_a_word_stay_distinct():
    applied = roster.Operator(name="Applied Digital", kind="neocloud")
    galaxy = roster.Operator(name="Galaxy Digital", kind="neocloud")
    assert applied.matches("Galaxy Digital Inc.") is None
    assert galaxy.matches("Applied Digital Corporation") is None


# --- Measuring --------------------------------------------------------------


def test_absent_thin_and_covered(session, tmp_path):
    path = _roster(
        tmp_path,
        '[[operator]]\nname = "Nebius"\nkind = "neocloud"\n'
        '[[operator]]\nname = "Crusoe"\nkind = "neocloud"\n'
        '[[operator]]\nname = "Meta"\nkind = "hyperscaler"\naliases = ["Facebook"]\n',
    )
    _project(session, "Crusoe", "Abilene")
    _project(session, "Meta", "Hyperion")
    _project(session, "Facebook", "Prineville")

    report = roster.measure(session, roster.load(path))
    by_name = {row.name: row for row in report.rows}

    assert by_name["Nebius"].status == "absent"
    assert by_name["Nebius"].projects == 0
    assert by_name["Crusoe"].status == "thin", "one row is not coverage"
    assert by_name["Meta"].status == "covered"
    assert by_name["Meta"].projects == 2, "the alias folds Facebook into Meta"


def test_rows_without_a_capacity_figure_count_as_thin(session, tmp_path):
    path = _roster(tmp_path, '[[operator]]\nname = "Vultr"\nkind = "neocloud"\n')
    _project(session, "Vultr", "One", mw=None)
    _project(session, "Vultr", "Two", mw=None)

    row = roster.measure(session, roster.load(path)).rows[0]
    assert row.projects == 2
    assert row.with_capacity == 0
    assert row.status == "thin", "two rows nobody has sized is still not knowing anything"


def test_a_loose_match_is_reported_as_loose(session, tmp_path):
    path = _roster(tmp_path, '[[operator]]\nname = "EdgeConneX"\nkind = "landlord"\n')
    _project(session, "TA Realty / EdgeConneX", "Project Steamboat")

    row = roster.measure(session, roster.load(path)).rows[0]
    assert row.projects == 1
    assert row.loose_only == ("TA Realty / EdgeConneX",), (
        "a match no alias authorized has to be visible, so it can be corrected"
    )


def test_states_are_counted_across_every_spelling(session, tmp_path):
    path = _roster(
        tmp_path, '[[operator]]\nname = "Meta"\nkind = "hyperscaler"\naliases = ["Facebook"]\n'
    )
    _project(session, "Meta", "Hyperion", state="LA")
    _project(session, "Facebook", "Prineville", state="OR")

    row = roster.measure(session, roster.load(path)).rows[0]
    assert row.states == ("LA", "OR")


def test_unrostered_companies_are_reported_back(session, tmp_path):
    """The reverse gap: the roster is hand-written and the database is not."""
    path = _roster(tmp_path, '[[operator]]\nname = "Meta"\nkind = "hyperscaler"\n')
    _project(session, "Meta", "Hyperion")
    _project(session, "Entergy Mississippi", "Delta Blues")
    _project(session, "Entergy Mississippi", "Vicksburg")

    report = roster.measure(session, roster.load(path))
    assert report.unrostered == [("Entergy Mississippi", 2, 600.0)]
    assert report.projects_total == 3
    assert report.rostered_projects == 1


def test_a_joint_venture_can_belong_to_two_operators(session, tmp_path):
    """And the totals must not double-count it.

    "OpenAI, Oracle" is one project and legitimately both operators', so summing
    the per-operator counts would report more projects than exist. `projects_total`
    counts rows, not claims.
    """
    path = _roster(
        tmp_path,
        '[[operator]]\nname = "OpenAI"\nkind = "ai_lab"\n'
        '[[operator]]\nname = "Oracle"\nkind = "hyperscaler"\n',
    )
    _project(session, "OpenAI, Oracle", "Michigan")

    report = roster.measure(session, roster.load(path))
    assert [row.projects for row in report.rows] == [1, 1]
    assert report.projects_total == 1
    assert report.unrostered == []
    assert report.rostered_projects == 1


def test_measure_on_an_empty_database_reports_everything_absent(session, tmp_path):
    path = _roster(tmp_path, '[[operator]]\nname = "Nebius"\nkind = "neocloud"\n')
    report = roster.measure(session, roster.load(path))
    assert [row.status for row in report.rows] == ["absent"]
    assert report.projects_total == 0
    assert report.unrostered == []


# --- Ordering the hunt ------------------------------------------------------


def test_hunt_order_puts_the_absent_first_then_keeps_file_order(session, tmp_path):
    path = _roster(
        tmp_path,
        '[[operator]]\nname = "Crusoe"\nkind = "neocloud"\n'  # thin
        '[[operator]]\nname = "Nebius"\nkind = "neocloud"\n'  # absent
        '[[operator]]\nname = "Vultr"\nkind = "neocloud"\n'  # absent
        '[[operator]]\nname = "Meta"\nkind = "hyperscaler"\n',  # covered
    )
    _project(session, "Crusoe", "Abilene")
    _project(session, "Meta", "Hyperion")
    _project(session, "Meta", "Eagle Mountain")

    report = roster.measure(session, roster.load(path))
    assert [row.name for row in roster.hunt_order(report)] == ["Nebius", "Vultr", "Crusoe"]
    assert [row.name for row in roster.hunt_order(report, include_thin=False)] == [
        "Nebius",
        "Vultr",
    ]
