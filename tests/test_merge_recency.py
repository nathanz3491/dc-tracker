"""A heavier source loses to one published more than a year later.

The 2026-10-21 loop read Aligned's whole newsroom. Its 2016 press releases (weight 3,
a company filing) then beat 2026 trade press (weight 2) on capacity, tenant and
date, because the merge ranks weight before recency: DFW-02 read 30 MW and Sabre, its
2017 tenant, instead of 46 MW and Lambda. `upsert.stale_claims` takes weight out of
the comparison once the heavier claim is a year older than a rival of the same
standing.
"""

from __future__ import annotations

import datetime as dt

from tracker.upsert import Policy, _Claim, resolve, stale_claims

FETCHED = dt.datetime(2026, 10, 21)


def _claim(value, weight, published, **kw) -> _Claim:
    return _Claim(
        value,
        weight,
        FETCHED,
        "trade_press",
        f"https://e.test/{value}",
        published_at=published,
        **kw,
    )


def _ranked(claims: list[_Claim]) -> list[_Claim]:
    stale = stale_claims(claims)
    return sorted(
        claims,
        key=lambda c: (
            not c.decided_against,
            c.confirmed,
            not c.part,
            not c.tertiary,
            id(c) not in stale,
            c.weight,
            c.recency(by_publication=False),
            c.url,
        ),
        reverse=True,
    )


def test_a_press_release_from_years_ago_loses_to_this_years_report():
    old = _claim(30.0, 3, dt.datetime(2016, 10, 14))
    new = _claim(46.0, 2, dt.datetime(2026, 8, 5))
    assert resolve(Policy.PREFER_WEIGHT, _ranked([old, new]), None) == 46.0


def test_within_a_year_weight_still_decides():
    """A plan restated within months is the same plan told twice."""
    heavy = _claim(72.0, 3, dt.datetime(2026, 1, 31))
    light = _claim(84.0, 2, dt.datetime(2026, 8, 5))
    assert resolve(Policy.PREFER_WEIGHT, _ranked([heavy, light]), None) == 72.0


def test_an_undated_claim_is_never_stale_and_dates_nothing():
    undated = _claim(30.0, 3, None)
    dated = _claim(46.0, 2, dt.datetime(2026, 8, 5))
    assert stale_claims([undated, dated]) == set()
    assert resolve(Policy.PREFER_WEIGHT, _ranked([undated, dated]), None) == 30.0


def test_standing_still_comes_first():
    """A newer unquoted claim does not make an older quoted one stale."""
    quoted = _claim(30.0, 3, dt.datetime(2016, 1, 1))
    unquoted = _claim(46.0, 2, dt.datetime(2026, 1, 1), confirmed=False)
    assert stale_claims([quoted, unquoted]) == set()
    assert resolve(Policy.PREFER_WEIGHT, _ranked([quoted, unquoted]), None) == 30.0


def test_the_claims_own_as_of_dates_it_ahead_of_its_article():
    """A 2026 article quoting a 2016 figure is a 2016 claim."""
    quoting = _claim(16.0, 2, dt.datetime(2026, 8, 5), as_of="2016-12-06")
    current = _claim(224.0, 2, dt.datetime(2025, 1, 1))
    assert stale_claims([quoting, current]) == {id(quoting)}


def test_the_merge_applies_it(session):
    """Through `claims_by_field`, which every write path and report reads."""
    import json

    from tracker.models import Project, Source
    from tracker.upsert import recompute_from_sources

    project = Project(
        name="DFW-02",
        company="Aligned",
        city="Plano",
        state="TX",
        dedup_key="aligned|city:plano|TX",
        phase="construction",
        confidence=1,
    )
    session.add(project)
    session.flush()
    for url, kind, published, mw, customer in (
        ("https://aligneddc.com/2016", "company_filing", dt.datetime(2016, 10, 14), 30.0, "Sabre"),
        ("https://dcd.test/2026", "trade_press", dt.datetime(2026, 8, 5), 46.0, "Lambda"),
    ):
        session.add(
            Source(
                project_id=project.id,
                url=url,
                source_type=kind,
                fetched_at=FETCHED,
                published_at=published,
                claims=json.dumps({"mw_planned": mw, "customer": customer}),
                quotes=json.dumps({"mw_planned": f"{mw:g} MW", "customer": customer}),
                fields="customer,mw_planned",
            )
        )
    session.flush()
    session.refresh(project)
    recompute_from_sources(session, project)
    assert (project.mw_planned, project.customer) == (46.0, "Lambda")
