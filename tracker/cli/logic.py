"""`tracker logic` — find values that contradict each other, and settle what can be.

Three commands and the three ways a finding can be settled: by a rule, by a model
reading the sources, or by a person at the keyboard. The pipeline these commands
drive is drawn in `docs/workflows/logic.md`; touching a function named in that
page's source map means updating the page in the same commit (`CLAUDE.md` §7).
"""

from __future__ import annotations

import atexit
from typing import Annotated

import typer
from rich.markup import escape
from sqlalchemy import select

from tracker.cli._shared import (
    _db_path,
    _explain_db_locks,
    _fail,
    _location,
    _print_report_rows,
    _read_engine,
    _use_llm,
    console,
    emit,
    err,
    json_mode,
    logic_app,
)
from tracker.config import get_settings
from tracker.db import AlreadyRunning, acquire_write_lock, init_db, session_scope
from tracker.models import Project


@logic_app.command("check")
def logic_check(
    read: Annotated[
        int,
        typer.Option(
            "--read",
            help="Also have a model read this many projects. 0 runs only the free checks.",
        ),
    ] = 0,
    audit: Annotated[
        int,
        typer.Option(
            "--audit",
            help=(
                "Also audit the evidence behind this many rows' values, costliest "
                "first. One LLM call per row; 0 skips it."
            ),
        ),
    ] = 0,
    project_id: Annotated[
        int | None,
        typer.Option("--id", help="Check one project.", show_default=False),
    ] = None,
    severity: Annotated[
        str | None,
        typer.Option("--severity", help="Show only `error` or only `warning`.", show_default=False),
    ] = None,
    code: Annotated[
        str | None,
        typer.Option("--code", help="Show only one kind of finding.", show_default=False),
    ] = None,
    collisions: Annotated[
        bool, typer.Option("--collisions/--no-collisions", help="Show source disagreements.")
    ] = True,
    limit: Annotated[int, typer.Option("--limit", help="Findings to print.")] = 40,
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Find values that contradict each other, and say which source wins.

    Every other check asks whether a value is supported. This one asks whether the
    supported values agree: a row can be perfectly cited and still be impossible.
    A campus marked `operational` whose construction track has reached nothing is
    either the wrong phase or a missing milestone, and both citations behind it
    can be sound.

    Three layers, and only the last one costs anything.

    **Rules** run always and are free. They state their reasoning, so you can
    disagree with one without reading any code: energised before operational,
    built above planned, online before announced, a milestone dated next year
    counted as already reached.

    **Collisions** are two sources claiming different values for one field. The
    winner is not decided here — it is read back from the same per-field policy
    the write path used, and the reason is printed with it. That reason is *not*
    always "the better source won": built capacity takes the largest figure,
    `first_announced` the earliest, and the identity fields are never overwritten
    at all. Only the rest are settled by credibility and then recency.

    **Judgement** is `--read N`, costs one LLM call per project, and is off by
    default. It catches what a rule cannot phrase — a blocker describing a problem
    the milestones say is solved. Every finding it returns must name two fields
    and quote its evidence, or it is dropped; it is never allowed to pick a
    collision winner, and nothing it says is written to the database.

    **The evidence audit** is `--audit N`, the same cost, and asks the prior
    question: does each value's own recorded sentence actually state it — or is
    it a programme-wide total quoted as one campus's money, a figure about a
    different building, an aspiration recorded as a schedule? Rows are read
    costliest first. A finding a person confirms is answered by demoting the
    value in `tracker review`; an unconfirmed investment figure already stays
    out of the capex sums, so the repair path exists before the audit runs.
    """
    _use_llm(llm_provider)
    from tracker import logic as logic_mod

    engine = _read_engine()
    extractor = None
    if read > 0 or audit > 0:
        from tracker.llm import LLMUnavailable, reasoning_extractor

        try:
            extractor = reasoning_extractor(get_settings())
        except LLMUnavailable as exc:
            _fail(str(exc))

    def announce(project) -> None:
        console.print(f"[dim]reading #{project.id} {project.company} — {project.name}[/dim]")

    def announce_audit(project) -> None:
        console.print(
            f"[dim]auditing evidence on #{project.id} {project.company} — {project.name}[/dim]"
        )

    # Progress lines share stdout with the payload, so in --json mode they would
    # corrupt it — the very first announce makes `json.load` fail at char 0.
    narrate = not json_mode()
    with session_scope(engine, commit=False) as session:
        report = logic_mod.review(
            session,
            extractor=extractor,
            read_limit=read or None,
            audit_limit=audit or None,
            only=project_id,
            on_examine=announce if read and narrate else None,
            on_audit=announce_audit if audit and narrate else None,
        )

    findings = report.findings
    if severity:
        findings = [f for f in findings if f.severity == severity.lower()]
    if code:
        findings = [f for f in findings if f.code == code]

    if json_mode():
        emit(
            {
                "projects": report.projects,
                "examined": report.examined,
                "audited": report.audited,
                "unreadable": report.unreadable,
                "findings": [f.as_json() for f in findings],
                "collisions": [c.as_json() for c in report.collisions],
            }
        )
        return

    _print_report_rows(report.as_rows(), title="logic")
    console.print()

    # Said out loud, not left in a table row. A truncated reply was paid for and
    # produced nothing, and the one reading it must not take that for "clean".
    if report.unreadable.get("truncated"):
        console.print(
            f"[yellow]{report.unreadable['truncated']} row(s) ran out of room while the "
            f"model was reasoning[/yellow] [dim]— those calls were paid for and returned "
            f"nothing. That is not the same as finding no contradictions; raise "
            f"MAX_TOKENS in tracker/logic.py (currently {logic_mod.MAX_TOKENS}).[/dim]"
        )
    if report.unreadable.get("unusable") or report.unreadable.get("error"):
        console.print(
            f"[yellow]{report.unreadable.get('unusable', 0) + report.unreadable.get('error', 0)} "
            "row(s) could not be read[/yellow] [dim]— run with -v for the reason[/dim]"
        )

    if not findings:
        console.print("[green]no contradictions[/green]")
    else:
        by_project: dict[int, list] = {}
        for finding in findings[:limit]:
            by_project.setdefault(finding.project_id, []).append(finding)
        for pid, group in by_project.items():
            console.print(f"[bold]#{pid}[/bold]")
            for finding in group:
                style = "red" if finding.severity == logic_mod.ERROR else "yellow"
                tag = "model" if finding.inferred else finding.code
                console.print(f"  [{style}]{tag}[/{style}] {escape(finding.summary)}")
                if finding.remedy:
                    console.print(f"    [dim]{escape(finding.remedy)}[/dim]")
        if len(findings) > limit:
            console.print(f"\n[dim]{len(findings) - limit} more; raise --limit[/dim]")

    if collisions and report.collisions:
        console.print(
            f"\n[bold]{len(report.collisions)} field(s) where sources disagree[/bold] "
            "[dim]— the kept value and why[/dim]"
        )
        for collision in report.collisions[:limit]:
            console.print(
                f"  #{collision.project_id} [bold]{collision.field}[/bold]: "
                f"{escape(str(collision.winner)[:44])} "
                f"[dim]over[/dim] {escape(str(collision.loser)[:44])}"
            )
            console.print(f"    [dim]{escape(collision.why)}[/dim]")
            if collision.stored_disagrees:
                console.print(
                    f"    [red]the row holds {escape(str(collision.stored)[:44])}[/red] "
                    "[dim]— run `tracker init` to recompute it from its sources[/dim]"
                )

    console.print(
        "\n[dim]nothing here is written. A contradiction is a question for a person: "
        "confirm the row in `tracker review`, or fold duplicates with `tracker merge`.[/dim]"
    )


@logic_app.command("conflicts")
def logic_conflicts(
    project_ids: Annotated[
        list[int] | None,
        typer.Argument(help="Only these projects. Default: every project.", show_default=False),
    ] = None,
    field: Annotated[
        str | None,
        typer.Option("--field", help="Settle one field only, e.g. investment_usd."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Fields to settle. 0 for all.")] = 20,
    apply: Annotated[
        bool, typer.Option("--apply", help="Write. Without it, proposes and writes nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation for a large run.")] = (
        False
    ),
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Let one model read every source about a contested field and settle it.

    **Costs one to two LLM calls per contested field.** Reasoning tier, because
    this is the hardest question the tool asks.

    Every other value in this database was extracted from one article in
    isolation, and the disagreements between articles are then settled by a sort:
    quote-backed first, then source weight, then date. That is the right default
    and it cannot tell a superseded figure from a rival one. Hyperion (#10) held
    Meta's 2024 $10B over its 2026 $50B because both come from the same publisher
    at the same weight, and crawl order decided it.

    So this is the one path where a model compares two contradicting sentences.
    It sees every quote-backed claim about one field at once — the value, the
    verbatim sentence, the publisher, and when they published — and answers with
    one key from a closed list.

    **It cannot type a value.** The options are figures publishers actually
    printed, already stored with their quotes; the model picks among them. A
    sentence nobody published has nowhere to enter by this path.

    **Refusing is a real answer.** Two credible publishers stating two figures
    with nothing to separate them is refused, not guessed at, and the
    disagreement stays disclosed in the row's notes. A second, adversarial call
    tries to knock every answer down before it is kept.

    **What `--apply` writes is not the field.** It marks the losing claims
    `superseded` on their own citations and re-derives the row, so the value still
    equals what its citations imply. Run `tracker backfill dates` first — the
    tiebreak, and this model, both reason from publication dates.
    """
    _use_llm(llm_provider)
    from typing import Any as _Any

    from tracker import conflicts as conflicts_mod

    engine, _ = init_db(_db_path()) if apply else (_read_engine(), None)

    with session_scope(engine, commit=False) as session:
        query = select(Project).order_by(Project.id.asc())
        if project_ids:
            query = query.where(Project.id.in_(project_ids))
        found: list[tuple[int, _Any]] = []
        for project in session.scalars(query).all():
            for dispute in conflicts_mod.disputes(project):
                if field and dispute.field != field:
                    continue
                found.append((project.id, dispute))

    chosen = found[:limit] if limit else found
    _print_report_rows(
        [
            ("contested fields", len(found)),
            ("will settle now", len(chosen)),
            ("LLM calls at most", len(chosen) * conflicts_mod.MAX_CALLS_PER_FIELD),
        ],
        title="logic conflicts",
    )
    if not chosen:
        console.print(
            "[green]nothing contested[/green] [dim]— no field has two quote-backed claims "
            "that genuinely disagree.[/dim]"
        )
        return

    if len(chosen) > 20 and not yes and not json_mode():
        console.print(
            f"\n[yellow]up to {len(chosen) * conflicts_mod.MAX_CALLS_PER_FIELD} LLM calls."
            "[/yellow] [dim]Re-run with --yes, or use --limit to go in tranches.[/dim]"
        )
        raise typer.Exit(1)

    from tracker.llm import LLMUnavailable, reasoning_extractor

    try:
        extractor = reasoning_extractor(get_settings())
    except LLMUnavailable as exc:
        _fail(str(exc))
        return

    if apply:
        try:
            release_lock = acquire_write_lock(_db_path(), command="logic conflicts")
        except AlreadyRunning as exc:
            _fail(str(exc))
            raise
        atexit.register(release_lock)

    report = conflicts_mod.SolveReport(disputes=len(chosen))
    console.rule("[bold]settle[/bold]", align="left")
    with _explain_db_locks(), session_scope(engine, commit=apply) as session:
        for project_id, dispute in chosen:
            outcome = conflicts_mod.solve(dispute, extractor=extractor)
            report.calls += outcome.calls
            report.outcomes.append(outcome)
            if outcome.verdict == "resolved":
                report.resolved += 1
                if apply:
                    project = session.get(Project, project_id)
                    report.written += conflicts_mod.apply_outcome(session, project, outcome)
                    # Per field, so a run that dies partway keeps what it settled.
                    session.commit()
            elif outcome.verdict == "refused":
                report.refused += 1
            else:
                report.errors += 1
            console.print(f"  {escape(outcome.render())}")

    if json_mode():
        emit({"rows": dict(report.as_rows()), "outcomes": [o.render() for o in report.outcomes]})
        return

    _print_report_rows(
        report.as_rows(),
        title="logic conflicts" + ("" if apply else " (proposal)"),
        warn={"errors"},
    )
    if not apply and report.resolved:
        console.print(
            f"\n[yellow]Nothing written.[/yellow] [dim]--apply supersedes the losing claims "
            f"on {report.resolved} field(s) and re-derives the rows.[/dim]"
        )
    if report.refused:
        console.print(
            f"[dim]{report.refused} refused. That is a recorded answer, not a failure — the "
            f"disagreement stays in the row's notes with both citations intact.[/dim]"
        )


@logic_app.command("resolve")
def logic_resolve(
    auto_only: Annotated[
        bool,
        typer.Option("--auto", help="Only the repairs needing no decision. No prompts."),
    ] = False,
    apply: Annotated[
        bool, typer.Option("--apply", help="With --auto: write. Otherwise implied.")
    ] = False,
    llm: Annotated[
        bool,
        typer.Option(
            "--llm",
            help="The old fixed-menu path: one call, one letter from a hand-written list.",
        ),
    ] = False,
    agent: Annotated[
        bool,
        typer.Option(
            "--agent/--no-agent",
            help=(
                "Default. A model reads the articles, searches if it must, and rules "
                "wrong claims out of the merge. Suppressed by --auto and --llm."
            ),
        ),
    ] = True,
    min_confidence: Annotated[
        float,
        typer.Option(
            "--min-confidence", help="With --agent: refuse a ruling below this. 0 allows any."
        ),
    ] = 0.75,
    code: Annotated[
        str | None,
        typer.Option("--code", help="Work through one kind of finding only.", show_default=False),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Findings to offer.")] = 30,
    again: Annotated[
        bool,
        typer.Option("--again", help="Re-offer findings already answered on their row."),
    ] = False,
    llm_provider: Annotated[
        str | None,
        typer.Option(
            "--llm-provider",
            help="Which model answers: 'deepseek' (the API) or 'ollama' (local).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Settle the collisions that can be settled, by re-running the merge policy.

    **Where the winner comes from.** Two sources disagreeing on one field is
    decided by how good each source is and how recently it was read — the more
    credible wins, and recency breaks the tie. That is already applied at write
    time, which is why a healthy database shows nothing to do here: the value on
    the row *is* the winner. This command exists for when it is not, which happens
    after a hand edit or when a source is attached by a path that did not
    re-derive. Re-running the policy is arithmetic, not a judgement.

    **Five fields do not use credibility, and that is deliberate.** Built capacity
    takes the largest cited figure, because energised megawatts only go up and a
    better source describing an earlier state should not walk it back.
    `first_announced` takes the earliest, because that is what "first" means.
    `phase` takes the furthest along unless a source says it stopped. Name,
    company and the location fields are never overwritten once set, because churn
    in an identity field is worse than staleness. `tracker logic check` prints
    which rule settled each collision, so the reason is always on screen.

    **Everything else needs a person and is left alone.** Whether 100 MW built
    against 32 MW planned means the plan was revised or the two figures describe
    different phases of one campus is not in the row; a tool that picked one would
    be inventing a fact. Measured on the live database, 0 of 149 findings were
    mechanically resolvable — which is the honest reason there is no button that
    fixes everything.

    **Then it hands you the rest, one at a time.** With the evidence on screen and
    the answers as single keys, because that is the difference between a report
    and a tool: a person looking at `100 MW built against 32 MW planned` with both
    quotes in front of them settles it in two seconds, and what they lacked was
    somewhere to put the answer. Every edit is written into the row's notes as
    plain prose, which is the one kind of note re-ingesting never erases.

    `--auto` does the no-decision repairs and stops, for scripts and for the
    console, which has no keyboard.

    **`--llm` lets a model choose instead of you**, one call per finding, and it
    skips whenever the evidence does not clearly favour one option. This is
    allowed where `tracker infer` bars a model from 关键数字, and the difference is
    worth understanding: `infer` asks a model to produce a number from general
    knowledge, whereas here every option was written by a person and operates only
    on figures already in the row with citations behind them. The model's whole
    output is one character from a closed set — it cannot type a value.

    Two things it may never do. It cannot mark a row verified, because that means
    "an operator says this is right" and feeds the confidence score. And its edits
    are recorded as `model resolved`, never `operator resolved`, so a reader six
    months later can tell that nobody looked.
    """
    _use_llm(llm_provider)
    import sys as _sys

    from tracker import logic as logic_mod

    path = _db_path()
    if not path.is_file():
        _fail(f"database not found: {path}\nRun `tracker init` first.")

    # `--agent` is the default and the other two paths are the opt-outs, because
    # the menu was the ceiling: `decide` declines 432 of 526 findings before it
    # calls a model at all. `--auto` (mechanical only) and `--llm` (the old menu)
    # both suppress it, so a script pinned to either keeps its behaviour.
    use_agent = agent and not auto_only and not llm

    extractor = None
    if llm or use_agent:
        from tracker.llm import LLMUnavailable, agent_extractor, reasoning_extractor

        try:
            # The agent pays its effort per turn across nine to twelve calls, so it
            # gets its own tier. `--llm` is one call and keeps `max`.
            build = agent_extractor if use_agent else reasoning_extractor
            extractor = build(get_settings())
        except LLMUnavailable as exc:
            # A bare `logic resolve` used to be the interactive walkthrough, and
            # making the agent the default must not take that away from somebody
            # with no key. Fall back where a person is watching; fail where one is
            # not, because a script that asked for the agent and silently got
            # nothing is worse than a script that stops.
            if not use_agent or not _sys.stdin.isatty() or json_mode():
                _fail(str(exc))
            err.print(f"[yellow]no model available, so answering by hand instead[/yellow]\n{exc}")
            use_agent = False

    interactive = (
        not auto_only and not llm and not use_agent and _sys.stdin.isatty() and not json_mode()
    )
    writing = interactive or apply or llm or use_agent

    if writing:
        engine, _ = init_db(path)
        try:
            release_lock = acquire_write_lock(path, command="logic resolve")
        except AlreadyRunning as exc:
            _fail(str(exc))
            raise
        atexit.register(release_lock)
    else:
        engine = _read_engine()

    with _explain_db_locks(), session_scope(engine, commit=writing) as session:
        repairs = logic_mod.resolve_drift(session, apply=writing)
        report = logic_mod.review(session)
        findings = [f for f in report.findings if not code or f.code == code]

        # Drop what has already been answered on this row. `audit resolve` has
        # skipped settled findings since it was written; `logic resolve` did not,
        # so it re-offered all 1,272 every run — including 384 of one code — and a
        # person or a model had to decline the same question every time. That is
        # also what makes "open findings" a number that can fall.
        if not again:
            from tracker.audit import settled_codes

            settled: dict[int, set[str]] = {}
            kept = []
            for finding in findings:
                if finding.project_id not in settled:
                    row = session.get(Project, finding.project_id)
                    settled[finding.project_id] = settled_codes(row) if row else set()
                if finding.code not in settled[finding.project_id]:
                    kept.append(finding)
            skipped = len(findings) - len(kept)
            findings = kept
            if skipped and not json_mode():
                console.print(
                    f"[dim]skipping {skipped} finding(s) already answered on their row; "
                    "--again to see them[/dim]"
                )
        # Findings something can be done about, first. The list was ordered by
        # project id, so `--limit 30` handed a person — or a model — thirty
        # questions whose only answers were "verify" and "skip", and the ones with
        # a real edit behind them sat at position 190.
        findings.sort(key=lambda f: (not logic_mod.resolvable(f), f.project_id))

        if json_mode():
            emit(
                {
                    "repaired": [
                        {
                            "project_id": r.project_id,
                            "changes": {f: {"was": w, "now": n} for f, (w, n) in r.changes.items()},
                        }
                        for r in repairs
                    ],
                    "left_for_a_person": [f.as_json() for f in findings],
                }
            )
            return

        for repair in repairs:
            console.print(
                f"[green]repaired[/green] #{repair.project_id} {escape(repair.label[:60])}"
            )
            for name, (was, now) in repair.changes.items():
                console.print(f"  {name}: {escape(str(was)[:36])} -> {escape(str(now)[:36])}")
        if repairs:
            console.print()

        # The findings a comparison answers, applied without asking anybody.
        #
        # Held to `audit.free_answer`'s bar: a read of data already stored, never a
        # judgement about which of two sourced figures to believe. Two codes clear
        # it and between them they were 448 of 536 resolvable findings — so this is
        # most of what makes a whole-database pass affordable instead of one model
        # call per finding.
        if writing:
            answered = 0
            for finding in list(findings):
                choice = logic_mod.free_answer(session.get(Project, finding.project_id), finding)
                if choice is None:
                    continue
                key, why = choice
                row = session.get(Project, finding.project_id)
                action = next(
                    (a for a in logic_mod.ACTIONS.get(finding.code, ()) if a.key == key), None
                )
                if action is None or row is None:
                    continue
                what = action.apply(session, row, finding)
                logic_mod.record_decision(row, finding.code, what, by="rule", detail=why)
                findings.remove(finding)
                answered += 1
            if answered:
                session.flush()
                console.print(
                    f"[green]{answered}[/green] finding(s) answered by comparison — "
                    "no model, no decision\n"
                )

        if not findings:
            console.print("[green]nothing left to decide[/green]")
            return

        if use_agent:
            # No `logic_mod.resolvable` filter, unlike the path below. That filter
            # exists because `decide` can only answer with a key from
            # `ACTIONS[code]`, and 11 of 16 codes have none — it is a property of
            # the menu, not of the finding. An agent rules claims out of the merge,
            # which is available on every code, so the 334 tranche findings the
            # menu could never touch reach a model here for the first time.
            _triage_by_agent(
                session,
                findings[:limit],
                extractor,
                min_confidence=min_confidence,
            )
            return

        if llm:
            # The whole list, not `[:limit]`: the limit is a budget for *calls*,
            # and slicing before the unanswerable ones are filtered out spent it
            # on findings the model can only decline. Ordered by project id, the
            # first thirty were almost all of that kind.
            _triage_by_model(session, findings, logic_mod, extractor, limit=limit)
            return

        if not interactive:
            console.print(
                f"[bold]{len(findings)}[/bold] contradiction(s) need a person.\n"
                "[dim]Run `tracker logic resolve` in a terminal to work through them; "
                "each one offers the edits that answer it.[/dim]"
            )
            return

        _triage(session, findings[:limit], logic_mod)


def _triage_by_agent(session, findings: list, extractor, *, min_confidence: float = 0.75) -> None:
    """Let a model read the sources and rule wrong claims out, one finding at a time.

    Prints what each run *looked at* as well as what it concluded. That is not
    decoration: the difference between "declined after reading three articles" and
    "declined after reading nothing" is the difference between a considered answer
    and a broken tool, and only the trail distinguishes them.

    Committed per finding, so a provider failure on row 40 keeps the first 39.
    """
    from tracker import triage as triage_mod
    from tracker.logic import record_decision
    from tracker.models import Project

    ruled = left = unusable = errored = 0
    spent = 0
    cache_hit = cache_miss = 0

    for index, finding in enumerate(findings, start=1):
        project = session.get(Project, finding.project_id)
        if project is None:
            continue

        head = f"[dim]{index}/{len(findings)}[/dim] #{project.id} {escape(project.name[:32])}"
        # `remedy` is included because it is where the codebase records what a
        # reader should look at, and withholding it makes the model rediscover
        # what a rule already knows. `subject` names what the finding is *about*
        # — the prompt was measurably blind without it.
        question = "\n".join(
            part
            for part in (
                f"Finding `{finding.code}`: {finding.summary}",
                f"About: {finding.subject}" if getattr(finding, "subject", "") else "",
                f"Where to look: {finding.remedy}" if finding.remedy else "",
                f"Fields involved: {', '.join(finding.fields)}" if finding.fields else "",
            )
            if part
        )

        # One finding must never end the batch. `triage` already turns a provider
        # failure into an outcome, but a database error raised while *applying* a
        # ruling escapes — and on the first overnight run an IntegrityError on
        # `phase` did exactly that, killing the logic phase of three rounds out of
        # five after a single bad finding. The session is rolled back so the next
        # finding starts from a clean transaction rather than a poisoned one.
        try:
            outcome = triage_mod.triage(
                session,
                project,
                question=question,
                extractor=extractor,
                min_confidence=min_confidence,
            )
        except Exception as exc:
            session.rollback()
            errored += 1
            console.print(f"{head}  [red]failed[/red] [dim]— {escape(str(exc)[:140])}[/dim]")
            continue
        spent += outcome.prompt_tokens + outcome.completion_tokens
        cache_hit += outcome.cache_hit_tokens
        cache_miss += outcome.cache_miss_tokens
        trail = " → ".join(outcome.steps) or "nothing"

        if outcome.verdict == "ruled":
            record_decision(
                project,
                finding.code,
                "; ".join(outcome.changes),
                by=f"agent ({outcome.confidence:.2f})",
                detail=outcome.note,
            )
            session.commit()
            ruled += 1
            console.print(f"{head}  [green]{escape(outcome.changes[0])}[/green]")
            console.print(f"      [dim]{escape(outcome.note[:150])}[/dim]")
        elif outcome.verdict == "left":
            # Recorded, so the next run does not pay to be told the same thing.
            record_decision(project, finding.code, "left alone", by="agent", detail=outcome.note)
            session.commit()
            left += 1
            console.print(f"{head}  [dim]left alone[/dim]")
            console.print(f"      [dim]{escape(outcome.note[:200])}[/dim]")
        elif outcome.verdict == "unusable":
            session.rollback()
            unusable += 1
            console.print(f"{head}  [yellow]unusable[/yellow] [dim]— {escape(outcome.note)}[/dim]")
        else:
            session.rollback()
            errored += 1
            console.print(f"{head}  [red]error[/red] [dim]— {escape(outcome.note[:120])}[/dim]")

        console.print(f"      [dim]looked at: {escape(trail)}[/dim]")

    console.print(
        f"\n[bold]{ruled}[/bold] ruled, [bold]{left}[/bold] left alone"
        + (f", [yellow]{unusable}[/yellow] unusable" if unusable else "")
        + (f", [red]{errored}[/red] errored" if errored else "")
        + f"  [dim]~{spent:,} tokens[/dim]"
    )
    if cache_hit or cache_miss:
        # The number that decides what a night costs. DeepSeek bills a cached
        # prompt token at a fraction of an uncached one, and an agent loop is the
        # ideal shape for it: every turn re-sends the previous turn's history
        # verbatim. A low rate here means the prefix is being disturbed.
        rate = cache_hit / (cache_hit + cache_miss)
        console.print(
            f"  [dim]prompt cache: {rate:.0%} hit ({cache_hit:,} cached, {cache_miss:,} not)[/dim]"
        )
    console.print(
        "\n[dim]A ruling supersedes a claim and re-derives the field, so it survives "
        "the next `backfill derive` — unlike the column assignments `--llm` makes. "
        "Recorded as `agent`, never as `operator`: nobody has read these sources.[/dim]"
    )


def _triage_by_model(session, findings: list, logic_mod, extractor, *, limit: int = 30) -> None:
    """Let a model answer each finding. One call per finding; it skips when unsure.

    Prints every decision *and* every skip with the reason, because a run that
    silently resolved 12 of 30 and said nothing about the other 18 would read as
    "the rest were fine". They were not — nobody has looked at them.

    **Findings no edit can answer never reach the model.** Eleven of the sixteen
    rules offer no action — a phase enum arguing with a campus that is half
    energised is a contradiction in the schema, not in the data — and `decide`
    can only ever answer "nothing to choose between" for them. On the live
    database that was 174 of 283 findings, and with the queue ordered by project
    id the first `--limit 30` was almost entirely made of them: twelve lines of
    "left alone" before a single decision. They are counted here and reported in
    one line, which is what they are worth.
    """
    from tracker.models import Project

    unanswerable = [f for f in findings if not logic_mod.resolvable(f)]
    findings = [f for f in findings if logic_mod.resolvable(f)]
    if unanswerable:
        by_code: dict[str, int] = {}
        for finding in unanswerable:
            by_code[finding.code] = by_code.get(finding.code, 0) + 1
        listed = ", ".join(
            f"{code} x{n}" for code, n in sorted(by_code.items(), key=lambda kv: -kv[1])
        )
        console.print(
            f"[dim]{len(unanswerable)} finding(s) have no edit that answers them and were "
            f"not sent to the model — {escape(listed)}.\n"
            "They are still worth reading: `tracker logic check`.[/dim]\n"
        )
    if not findings:
        console.print("[yellow]nothing here a model could act on[/yellow]")
        return

    over_budget = max(0, len(findings) - limit)
    findings = findings[:limit]

    applied = 0
    declined = 0
    rejected: dict[str, int] = {}
    for index, finding in enumerate(findings, start=1):
        project = session.get(Project, finding.project_id)
        if project is None:
            continue

        decision = logic_mod.decide(project, finding, extractor=extractor)
        head = f"[dim]{index}/{len(findings)}[/dim] #{project.id} {escape(project.name[:34])}"

        if not decision.acted:
            # A decline is the model doing what it was told; a rejection is its
            # answer being unusable. Counting them together would hide a broken
            # prompt inside a pile of sensible caution.
            if decision.outcome == "declined":
                declined += 1
                # On its own line and not truncated to 90 characters. A decline is
                # now a substantive reading of the obstacle and the milestone —
                # "the noise complaint was first seen after the permit was
                # approved" is the answer, and clipping it mid-word threw away the
                # most useful thing the run produced.
                console.print(f"{head}  [dim]left alone[/dim]")
                console.print(f"      [dim]{escape(decision.note[:220])}[/dim]")
            else:
                rejected[decision.note.split(":")[0]] = (
                    rejected.get(decision.note.split(":")[0], 0) + 1
                )
                console.print(
                    f"{head}  [yellow]unusable answer[/yellow] "
                    f"[dim]— {escape(decision.note[:70])}[/dim]"
                )
            continue

        action = next(a for a in logic_mod.ACTIONS[finding.code] if a.key == decision.key)
        changed = action.apply(session, project, finding)
        logic_mod.record_decision(
            project,
            finding.code,
            changed,
            by=f"model ({decision.confidence:.2f})",
            detail=decision.reason,
        )
        session.commit()
        applied += 1
        console.print(f"{head}  [green]{escape(changed)}[/green]")
        console.print(f"      [dim]{escape(decision.reason[:110])}[/dim]")

    from tracker.upsert import recompute_confidence

    recompute_confidence(session)
    session.commit()

    console.print(
        f"\n[bold]{applied}[/bold] resolved, [bold]{declined}[/bold] the model declined"
        + (f", [yellow]{sum(rejected.values())}[/yellow] unusable" if rejected else "")
        + (f", {over_budget} beyond --limit" if over_budget else "")
    )
    for reason, count in sorted(rejected.items(), key=lambda kv: -kv[1]):
        console.print(f"  [yellow]{count:>3}[/yellow]  [dim]{escape(reason)}[/dim]")
    console.print(
        "\n[dim]Each edit is recorded on the project as `model resolved`, never as "
        "`operator resolved` — nobody has read the sources for these. Run "
        "`tracker logic resolve` with no flags to go through the rest yourself.[/dim]"
    )


def _triage(session, findings: list, logic_mod) -> None:
    """Walk an operator through findings, applying the edit they choose.

    Committed after each answer rather than at the end. Somebody triaging forty
    rows will stop partway, and losing the first thirty decisions to a Ctrl-C is
    the kind of thing that stops a tool being used twice.
    """
    from tracker.models import Project

    done = skipped = 0
    total = len(findings)
    for index, finding in enumerate(findings, start=1):
        project = session.get(Project, finding.project_id)
        if project is None:
            continue

        console.rule(f"[dim]{index} of {total}[/dim]", align="left")
        console.print(
            f"[bold]#{project.id}[/bold] {escape(project.company)} — {escape(project.name)}"
            f"  [dim]{escape(_location(project))}[/dim]"
        )
        style = "red" if finding.severity == logic_mod.ERROR else "yellow"
        console.print(f"[{style}]{finding.code}[/{style}] {escape(finding.summary)}")
        if finding.remedy:
            console.print(f"[dim]{escape(finding.remedy)}[/dim]")

        # The values in play, so the decision does not need another command.
        for name in finding.fields or ():
            value = getattr(project, name, None)
            quote = _one_quote(project, name)
            console.print(f"  [bold]{name}[/bold] = {escape(str(value))}")
            if quote:
                console.print(f'    [dim]"{escape(quote[:150])}"[/dim]')

        actions = logic_mod.ACTIONS.get(finding.code, ())
        for action in actions:
            console.print(f"  [bold cyan]{action.key}[/bold cyan]  {action.label}")
        console.print("  [bold cyan]v[/bold cyan]  it is fine as it is — mark the row verified")
        console.print("  [bold cyan]s[/bold cyan]  skip    [bold cyan]q[/bold cyan]  stop here")

        valid = {a.key for a in actions} | {"v", "s", "q"}
        choice = ""
        while choice not in valid:
            choice = typer.prompt("  >", default="s", show_default=False).strip().lower()

        if choice == "q":
            break
        if choice == "s":
            skipped += 1
            continue
        if choice == "v":
            from tracker.models import utcnow

            project.last_verified_at = utcnow()
            logic_mod.record_decision(project, finding.code, "confirmed correct as it stands")
        else:
            action = next(a for a in actions if a.key == choice)
            changed = action.apply(session, project, finding)
            logic_mod.record_decision(project, finding.code, changed)
            console.print(f"  [green]{escape(changed)}[/green]")
        session.commit()
        done += 1

    from tracker.upsert import recompute_confidence

    rescored = recompute_confidence(session)
    session.commit()
    console.print(
        f"\n[bold]{done}[/bold] decided, [bold]{skipped}[/bold] skipped"
        + (f", {rescored} row(s) rescored" if rescored else "")
        + "\n[dim]every decision is written into the project's notes. "
        "Re-run `tracker logic check` to see what is left.[/dim]"
    )


def _one_quote(project, field: str) -> str | None:
    """The sentence behind a value, when there is one, for the triage screen."""
    from tracker.gaps import provenance

    try:
        prov = provenance(project, field)
    except Exception:
        return None
    return (prov.quote or "").strip() if prov else None
