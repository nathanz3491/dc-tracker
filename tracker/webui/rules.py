"""Rules that apply to a place, as the console reads them (`tracker.conditions`).

Three readers: a project's page ("Exposed to"), the Rules view, and one rule's list
of the projects it reaches. Nothing here decides anything. Which projects a rule
reaches, how sure the match is and why, is `conditions.reach`'s, stored on the
exposure; what would clear one is `watchfor.clears`, the sentence the Monitor page
prints. This module only shapes rows for the page.

**Kept apart from obstacles on purpose.** A rule reaches a project because of where
the project is, not because an article about that campus reported a problem. So
nothing here touches the project payload (`dataset.project_payload`): the project
route sends exposures beside the project, not inside it, and no obstacle count,
blocker, confidence or completeness reads them.

**A rule that eases building is not a threat.** Every rule says whether it restricts
(a ban, a pause, a new cost) or eases (an exemption, by-right zoning). Both reach
projects the same way; the pages draw the easing ones under "Helped by", with
nothing to clear, and every count here keeps the two apart.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from tracker import conditions
from tracker.models import Condition, ConditionSource, Exposure, Project
from tracker.normalize import state_name
from tracker.sources import host_of
from tracker.watchfor import COVERS, EASES, EFFECT_LABEL, clears, need_label, rule_status_label

#: What each verified sentence of an article was kept for, in the order a reader
#: wants them: the rule itself, where, where it stands, its size threshold.
QUOTE_ROLES: tuple[tuple[str, str], ...] = (
    ("rule", "the rule"),
    ("place", "where"),
    ("status", "where it stands"),
    ("threshold", "the size threshold"),
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.date().isoformat() if hasattr(value, "date") else value.isoformat()


def _link(url: str | None) -> str | None:
    """`url` if a browser may follow it from an `href`, else None.

    Every stored article URL is http or https, because the news step fetched it. A
    link is still the one place a stored string becomes something a click runs, so
    the page is never handed any other scheme.
    """
    return url if url and url.lower().startswith(("http://", "https://")) else None


def quotes_json(source: ConditionSource) -> list[dict[str, Any]]:
    """An article's verified sentences, each once, with what it was kept for.

    The model is asked for one sentence per thing it says and told to repeat one
    that says several, so "voters approved a ban on data centers in Piqua" can be
    the rule, the place and the status at once. Printed three times it reads as
    three findings; it is one sentence that bears out three things.
    """
    stored = conditions.quotes_of(source)
    out: list[dict[str, Any]] = []
    by_text: dict[str, dict[str, Any]] = {}
    for role, label in QUOTE_ROLES:
        text = stored.get(role)
        if not isinstance(text, str) or not text.strip():
            continue
        if text in by_text:
            by_text[text]["for"].append(label)
            continue
        entry = {"text": text, "for": [label]}
        by_text[text] = entry
        out.append(entry)
    return out


def source_json(source: ConditionSource) -> dict[str, Any]:
    """One article behind a rule: where it is, what it said the rule was, its sentences."""
    url = _link(source.url)
    return {
        "title": source.title or None,
        "url": url,
        "publisher": host_of(source.url) if source.url else None,
        "published": _iso(source.published_at),
        "read": _iso(source.read_at),
        "status": source.status,
        "status_label": rule_status_label(source.status),
        "summary": source.summary,
        "quotes": quotes_json(source),
    }


def rule_json(
    condition: Condition, *, reach: int | None = None, sources: bool = True
) -> dict[str, Any]:
    """One rule as the console draws it: `conditions.as_json`, plus the words for it.

    Articles newest first, because the newest is the one that set the status.
    """
    out = conditions.as_json(condition, reach=reach)
    covers = COVERS.get(condition.applies_to, condition.applies_to)
    if condition.min_mw is not None:
        covers += f" of {condition.min_mw:g} MW and up"
    out.update(
        {
            "where": conditions.where_words(condition),
            "status_label": rule_status_label(condition.status),
            "effect_label": EFFECT_LABEL.get(condition.effect, condition.effect),
            "need_label": need_label(condition.channel),
            "kind_label": condition.kind.replace("_", " "),
            "covers": covers,
            "clears": clears(condition),
            # National and whole-industry rules reach no project (see `market`).
            "market": condition.scope not in conditions.PLACED,
        }
    )
    if sources:
        newest_first = sorted(
            condition.sources,
            key=lambda s: (s.published_at or s.read_at, s.id or 0),
            reverse=True,
        )
        out["sources"] = [source_json(s) for s in newest_first]
    return out


def exposures(session: Session, project_id: int) -> list[dict[str, Any]]:
    """The rules for a project's place that reach it, newest report first.

    `conditions.for_project`, less any rule no longer standing: the exposures are
    rebuilt by `tracker conditions match`, so a rule read as lifted keeps its old
    exposures until the next rebuild, and a lifted rule reaches nothing.
    """
    out = []
    for condition, exposure in conditions.for_project(session, project_id):
        if condition.status not in conditions.EXPOSING:
            continue
        row = rule_json(condition)
        row.update(
            {
                "fit": exposure.fit,
                "why": exposure.why,
                "first_seen": _iso(exposure.first_seen),
            }
        )
        out.append(row)
    return out


def _states_of(condition: Condition) -> set[str]:
    """The states a placed rule reaches into: its own, or a grid region's."""
    if condition.scope == "grid_region":
        return {s for s, r in conditions.STATE_REGIONS.items() if r == condition.region}
    return {condition.state} if condition.state else set()


def listing(session: Session, *, state: str | None = None, cleared: bool = False) -> dict[str, Any]:
    """The Rules view: every rule for a place, the market apart, and the states to filter by.

    **A state's filter takes in its grid region's rules.** `conditions.listing`
    filters on the rule's own state, and a grid-region rule has none — so Texas
    would leave out the ERCOT rule that reaches every Texas project. Here a state
    keeps the rules filed under it and those of the region it is mostly in
    (`conditions.STATE_REGIONS`, the same inference the match makes).

    `cleared` adds lifted and voted-down rules, which reach nothing: history, kept
    out unless asked for.

    **Restricting and easing rules are counted apart.** "Projects reached" is the
    projects a rule that makes building harder reaches; the ones only an exemption
    or by-right zoning reaches are `helped`, so help is never added to a threat.
    """
    wanted = state.upper() if state else None
    held = conditions.listing(session, cleared=cleared)
    reach = conditions.reach_counts(session)
    placed = [c for c in held if c.scope in conditions.PLACED]
    by_state: Counter[str] = Counter()
    for condition in placed:
        by_state.update(_states_of(condition))
    shown = [c for c in placed if wanted is None or wanted in _states_of(c)]
    standing = [c for c in shown if c.status in conditions.EXPOSING]

    def projects_reached(ids: list[int]) -> int:
        if not ids:
            return 0
        return (
            session.scalar(
                select(func.count(func.distinct(Exposure.project_id))).where(
                    Exposure.condition_id.in_(ids)
                )
            )
            or 0
        )

    restricting = [c for c in standing if c.effect != EASES]
    market = conditions.market(session)
    return {
        "state": wanted,
        "cleared": cleared,
        "rules": [
            rule_json(c, reach=reach.get(c.id, 0) if c.status in conditions.EXPOSING else 0)
            for c in shown
        ],
        "market": [rule_json(c) for c in market],
        "states": [
            {"state": code, "name": state_name(code) or code, "rules": n}
            for code, n in sorted(by_state.items())
        ],
        "counts": {
            "rules": len(shown),
            "standing": len(standing),
            "restricts": sum(1 for c in shown if c.effect != EASES),
            "eases": sum(1 for c in shown if c.effect == EASES),
            # Restrictions in effect now; an exemption in force is good news.
            "in_force": sum(1 for c in restricting if c.status == "in_force"),
            "projects": projects_reached([c.id for c in restricting]),
            "helped": projects_reached([c.id for c in standing if c.effect == EASES]),
            "market": len(market),
        },
    }


def one(session: Session, condition_id: int) -> dict[str, Any] | None:
    """One rule, its articles, and every project it reaches with the fit and why.

    The Rules view asks for this when a rule is opened: Texas's pause reaches some
    two hundred projects, and the list of every rule should not carry all of them.
    Exact matches first, then by state and name.
    """
    from tracker.company import display_names

    condition = session.scalars(
        select(Condition)
        .where(Condition.id == condition_id)
        .options(selectinload(Condition.sources))
    ).first()
    if condition is None:
        return None
    rows = session.execute(
        select(Exposure, Project)
        .join(Project, Project.id == Exposure.project_id)
        .where(Exposure.condition_id == condition_id)
        .order_by(Exposure.fit, Project.state, Project.name, Project.id)
    ).all()
    standing = condition.status in conditions.EXPOSING
    names = display_names(session) if rows and standing else None
    projects = (
        [
            {
                "project_id": project.id,
                "project": project.name,
                "company": names.company(project.company) if names else project.company,
                "location": ", ".join(p for p in (project.city, project.state) if p),
                "phase": project.phase,
                "fit": exposure.fit,
                "why": exposure.why,
                "first_seen": _iso(exposure.first_seen),
            }
            for exposure, project in rows
        ]
        if standing
        else []
    )
    return {"rule": rule_json(condition, reach=len(projects)), "projects": projects}


__all__ = ["QUOTE_ROLES", "exposures", "listing", "one", "quotes_json", "rule_json", "source_json"]
