"""The suspicion score: how likely each stored value is to be wrong, to aim re-checks.

**What it is for.** A value's quote check says a sentence exists; it cannot say the
sentence is about this campus, this phase or this kind of figure. The answer key found
41% of the values it judged wrong, every one with a real sentence behind it. Re-reading
everything is not affordable, so this ranks the values and the nightly loop re-reads
the top of the list (plus a random fifth, so the record does not only confirm the
ranking). The score never writes a value. It aims a pass that does — `logic resolve
--suspects N`, the agent that re-reads sources and rules — and what that pass decides
is filed against what the ranking knew (`record_outcomes`, below).

**Two models, one in charge.** The logistic regression ranks, at the start of the
night: seven readable signals about how a value is backed (how many statements and
publishers stand behind it, whether only directories do, whether only unconfirmed
statements do, whether a filing does, how many sources claim something else, how much
of the row's evidence was ruled out) plus the field. Written in plain Python, and
dependency-free on purpose: 12 weights do not need numpy.

**How it was chosen (2026-11-11).** Two label sets that do not share a campus: the
answer key's frozen verdicts (204 values, 60 campuses — the campuses carrying the most
money) and 105 values the nightly agent passes judged elsewhere (changed = wrong;
kept, or left alone with a reason = right), each read from the snapshot taken before
its pass. A change had to help on both. Dropping two signals did — the age of the
newest support and how cited the row is, which describe the campus and the calendar
rather than the value — from AUC 0.79 to 0.80 on the key's ranked fields (campuses
held out) and 0.76 to 0.77 on the agent's labels. Everything else that helped on the
key failed the second set: the figure's size and its dollars per megawatt (key 0.84,
agent 0.65 — the key's big campuses are wrong by being too big; elsewhere size says
nothing), hedge, power-plant, purchase and other sentence flags (no better than chance
on either), agent labels as extra training, per-field models, boosting, stronger or
weaker penalties, and ranking with the transformer (key +0.01, agent -0.02).

The transformer (`tracker.suspect_transformer`, the optional `head` extra) is retrained
beside it each night and is there for an agent to consult on one row. It ranks nothing:
fresh retrains that never saw the agent's labels score 0.60 to 0.67 on them.

**Which fields.** Planned MW, investment and the online date, where the score finds
wrong values two times in three or better. Not the stage (its errors are an unrecorded
stop, which a date check finds better) and not the first-announced date (its errors
are about which facility a date belongs to, which no backing signal shows: AUC 0.54).

**Retraining.** `train` refits at the end of each night on the answer key's verdicts on
the held values. The key is the only clean label set, so a refit is measured by
refitting with whole campuses left out, over five different splits, on the ranked
fields. The saved model's own signal set is measured the same way on the same labels,
and the better of the two is refitted; it is kept only if it clears `FLOOR`, otherwise
the previous weights stay.

**What the night's passes found is kept** (`record_outcomes`): every ranked value a
pass then changed, kept or left alone with a reason, with the signals and the score it
had when it was ranked. That is the honest test of the ranking — features from before
the decision, a label from after — and it calibrates the probability shown to the
agent, which the key alone sets far too high: values the score put at 86% were changed
54% of the time, values at 60% one time in ten.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

FIELDS = ("phase", "mw_planned", "investment_usd", "expected_online", "first_announced")
#: The fields the score ranks for re-checking. See the module docstring for the others.
SCORED = ("mw_planned", "investment_usd", "expected_online")
#: Every signal `signals` reads. A saved model may use any of these.
SIGNALS = (
    "log_backers",
    "publishers",
    "directory_only",
    "unconfirmed_only",
    "primary_backed",
    "rival_claims",
    "ruled_out",
    "log_age_days",
    "log_row_citations",
)
FIELD_FLAGS = tuple(f"field_{f}" for f in FIELDS)
KNOWN = SIGNALS + FIELD_FLAGS
#: What a new fit uses. Not the age of the newest support nor how cited the row is:
#: both describe the campus and the calendar rather than the value, and leaving them
#: out ranked better on the answer key and on the agent's own verdicts alike.
FEATURES = tuple(s for s in SIGNALS if s not in ("log_age_days", "log_row_citations")) + FIELD_FLAGS
PRIMARY = {"company_filing", "government_doc"}
#: A refit whose held-out AUC falls below this keeps the previous weights.
FLOOR = 0.65
#: Campus splits a refit is measured on; one split moves the AUC by ±0.015.
REPEATS = 5
#: Outcomes needed, of both kinds together, before they calibrate the probability.
CALIBRATE_MIN = 50
#: The share of each night's suspects drawn at random rather than from the top.
RANDOM_SHARE = 0.2
MODEL = "suspect-logistic.json"
RANKED = "suspicion.jsonl"
OUTCOMES = "suspect-outcomes.jsonl"
CODE_PREFIX = "suspect_"


def models_dir(db_path: Path | str) -> Path:
    return Path(db_path).parent / "models"


def _number(claim: dict) -> float | None:
    try:
        return float(claim["number"]) if claim.get("number") not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _same(a: dict, held: Any) -> bool:
    n = _number(a)
    try:
        h = float(held)
    except (TypeError, ValueError):
        h = None
    if n is not None and h is not None:
        return abs(n - h) <= 0.05 * max(abs(n), abs(h), 1e-9)
    return str(a.get("value") or "").strip().lower()[:10] == str(held or "").strip().lower()[:10]


def signals(entry: dict, today: dt.date | None = None) -> dict[str, float] | None:
    """The features for one `tracker.training` entry's held value, or None if it holds none."""
    if entry.get("held") in (None, ""):
        return None
    claims = entry.get("claims") or []
    backing = [c for c in claims if c.get("held")] or [c for c in claims if _same(c, entry["held"])]
    if not backing:
        return None
    today = today or dt.date.today()
    tiers = {c.get("tier") for c in backing}
    live = [c for c in claims if not c.get("held") and c.get("tier") != "ruled_out"]
    rivals = [c for c in live if not _same(c, entry["held"])]
    dates = []
    for c in backing:
        try:
            dates.append(dt.date.fromisoformat(str(c.get("as_of"))[:10]))
        except ValueError:
            continue
    age = max((today - max(dates)).days, 0) if dates else 365
    citations = (entry.get("row") or {}).get("citations", 0)
    out = {
        "log_backers": math.log1p(len(backing)),
        "publishers": float(len({urlparse(c.get("source_url") or "").netloc for c in backing})),
        "directory_only": float("confirmed" not in tiers and "directory" in tiers),
        "unconfirmed_only": float("confirmed" not in tiers and "directory" not in tiers),
        "primary_backed": float(any(c.get("source_type") in PRIMARY for c in backing)),
        "rival_claims": math.log1p(len(rivals)),
        "ruled_out": math.log1p(sum(c.get("tier") == "ruled_out" for c in claims)),
        "log_age_days": math.log1p(age),
        "log_row_citations": math.log1p(citations),
    }
    out.update({f"field_{f}": float(entry["field"] == f) for f in FIELDS})
    return out


# --- the logistic regression, in plain Python ----------------------------------------


def _sigmoid(t: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, t))))


@dataclass
class Logistic:
    """Standardised features, L2-penalised logistic regression, one weight per feature.

    `features` names what it was fitted on, so a model saved with another signal set
    (an older one, or one a refit judged better) keeps scoring after the default
    changes. `calibration` maps its log-odds onto what the night's passes then found
    (`record_outcomes`); without it the probability is on the answer key's scale.
    """

    weights: list[float]
    intercept: float
    mean: list[float]
    scale: list[float]
    labels: int = 0
    heldout_auc: float | None = None
    trained_at: str = ""
    features: list[str] = field(default_factory=lambda: list(FEATURES))
    calibration: dict[str, float] | None = None

    def vector(self, sig: dict[str, float]) -> list[float]:
        return [sig[f] for f in self.features]

    def z(self, x: Sequence[float]) -> list[float]:
        return [(v - m) / s for v, m, s in zip(x, self.mean, self.scale, strict=True)]

    def logit(self, x: Sequence[float]) -> float:
        return self.intercept + sum(w * v for w, v in zip(self.weights, self.z(x), strict=True))

    def prob(self, x: Sequence[float]) -> float:
        return _sigmoid(self.logit(x))

    def calibrated(self, x: Sequence[float]) -> float:
        """P(wrong) on the scale of what the passes found, once enough is known."""
        if not self.calibration:
            return self.prob(x)
        t = max(-30.0, min(30.0, self.logit(x)))
        return _sigmoid(self.calibration["slope"] * t + self.calibration["intercept"])

    def contributions(self, x: Sequence[float]) -> list[tuple[str, float]]:
        """Each feature's push on the log-odds, largest first."""
        parts = [(n, w * v) for n, w, v in zip(self.features, self.weights, self.z(x), strict=True)]
        return sorted(parts, key=lambda p: -abs(p[1]))

    def as_json(self) -> dict[str, Any]:
        return {
            "features": list(self.features),
            "weights": self.weights,
            "intercept": self.intercept,
            "mean": self.mean,
            "scale": self.scale,
            "labels": self.labels,
            "heldout_auc": self.heldout_auc,
            "trained_at": self.trained_at,
            "calibration": self.calibration,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Logistic:
        features = list(data.get("features") or [])
        if not features or not set(features) <= set(KNOWN):
            raise ValueError("the saved model was trained on other features")
        if not len(data["weights"]) == len(data["mean"]) == len(data["scale"]) == len(features):
            raise ValueError("the saved model's weights do not match its features")
        return cls(
            data["weights"],
            data["intercept"],
            data["mean"],
            data["scale"],
            data.get("labels", 0),
            data.get("heldout_auc"),
            data.get("trained_at", ""),
            features,
            data.get("calibration"),
        )


def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    """A x = b by Gaussian elimination with partial pivoting (A is small and symmetric)."""
    n = len(b)
    M = [[*row, b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            if f:
                M[r] = [a - f * v for a, v in zip(M[r], M[c], strict=True)]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = M[r][n] - sum(M[r][j] * x[j] for j in range(r + 1, n))
        x[r] = s / M[r][r] if abs(M[r][r]) >= 1e-12 else 0.0
    return x


def fit(
    X: list[list[float]],
    y: list[int],
    *,
    l2: float = 2.0,
    steps: int = 50,
    features: Sequence[str] | None = None,
) -> Logistic:
    """Minimise the summed log-loss plus `l2`/2 times the squared weights, by Newton steps.

    The intercept is not penalised. This is the optimum the gradient descent it replaced
    approached in 3,000 steps; Newton's method reaches it in about ten, which is what
    makes measuring a refit on several campus splits affordable in plain Python.
    """
    n, k = len(X), len(X[0])
    mean = [sum(r[j] for r in X) / n for j in range(k)]
    scale = [max(math.sqrt(sum((r[j] - mean[j]) ** 2 for r in X) / n), 1e-9) for j in range(k)]
    Z = [[1.0] + [(r[j] - mean[j]) / scale[j] for j in range(k)] for r in X]
    beta = [0.0] * (k + 1)

    def objective(b: list[float]) -> float:
        total = l2 / 2 * sum(v * v for v in b[1:])
        for row, target in zip(Z, y, strict=True):
            t = max(-30.0, min(30.0, sum(c * v for c, v in zip(b, row, strict=True))))
            total += math.log1p(math.exp(-t)) if target else math.log1p(math.exp(t))
        return total

    current = objective(beta)
    for _ in range(steps):
        grad = [0.0] + [l2 * v for v in beta[1:]]
        hess = [
            [(l2 + 1e-9) if (i == j and i) else 0.0 for j in range(k + 1)] for i in range(k + 1)
        ]
        for row, target in zip(Z, y, strict=True):
            p = _sigmoid(sum(c * v for c, v in zip(beta, row, strict=True)))
            err, w = p - target, p * (1.0 - p)
            for i in range(k + 1):
                grad[i] += err * row[i]
                wi = w * row[i]
                if wi:
                    h = hess[i]
                    for j in range(i, k + 1):
                        h[j] += wi * row[j]
        for i in range(k + 1):
            for j in range(i):
                hess[i][j] = hess[j][i]
        step = _solve(hess, grad)
        # Halve a step that does not lower the objective; Newton rarely needs it here.
        scale_step, nxt, value = 1.0, beta, current
        for _ in range(20):
            nxt = [b - scale_step * s for b, s in zip(beta, step, strict=True)]
            value = objective(nxt)
            if value <= current + 1e-12:
                break
            scale_step /= 2
        moved = max(abs(a - b) for a, b in zip(nxt, beta, strict=True))
        beta, current = nxt, value
        if moved < 1e-9:
            break
    names = list(features) if features is not None else None
    if names is None:
        names = list(FEATURES) if k == len(FEATURES) else [f"x{j}" for j in range(k)]
    return Logistic(beta[1:], beta[0], mean, scale, labels=n, features=names)


def auc(y: Sequence[int], p: Sequence[float]) -> float | None:
    """Area under the ROC curve: the chance a wrong value outranks a right one."""
    pos = [s for s, t in zip(p, y, strict=True) if t]
    neg = [s for s, t in zip(p, y, strict=True) if not t]
    if not pos or not neg:
        return None
    wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def heldout_auc(
    X,
    y,
    groups,
    folds: int = 5,
    *,
    repeats: int = 1,
    measure: Sequence[bool] | None = None,
) -> float | None:
    """AUC with every campus scored by a model fitted without it.

    Averaged over `repeats` different splits of the campuses (split `r` shuffles them
    with seed `r`, so split 0 is the one this always used). `measure` picks the rows the
    AUC is taken on — the ranked fields — while every row of the other campuses trains.
    """
    measured = []
    for r in range(repeats):
        campuses = sorted(set(groups))
        random.Random(r).shuffle(campuses)
        fold_of = {c: i % folds for i, c in enumerate(campuses)}
        scores = [0.0] * len(y)
        for f in range(folds):
            train = [i for i, g in enumerate(groups) if fold_of[g] != f]
            test = [i for i, g in enumerate(groups) if fold_of[g] == f]
            if not test or len({y[i] for i in train}) < 2:
                continue
            model = fit([X[i] for i in train], [y[i] for i in train])
            for i in test:
                scores[i] = model.logit(X[i])
        keep = [i for i in range(len(y)) if measure is None or measure[i]]
        value = auc([y[i] for i in keep], [scores[i] for i in keep])
        if value is not None:
            measured.append(value)
    return sum(measured) / len(measured) if measured else None


def load(db_path: Path | str) -> Logistic | None:
    path = models_dir(db_path) / MODEL
    if not path.is_file():
        return None
    try:
        return Logistic.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, KeyError) as exc:
        import logging

        logging.getLogger(__name__).warning("ignoring the saved suspicion model: %s", exc)
        return None


# --- what the commands do --------------------------------------------------------------


def entries(
    session: Any,
    db_path: Path | None,
    project_ids: Iterable[int] | None = None,
    snapshots: Path | None = None,
) -> list[dict]:
    """`tracker.training` rows, with each row's citation count added for the signals."""
    from sqlalchemy import func, select

    from tracker import training
    from tracker.models import Project, Source

    ids = (
        list(project_ids) if project_ids is not None else list(session.scalars(select(Project.id)))
    )
    counts = (
        dict(
            session.execute(
                select(Source.project_id, func.count(func.distinct(Source.url)))
                .where(Source.project_id.in_(ids))
                .group_by(Source.project_id)
            ).all()
        )
        if ids
        else {}
    )
    out = training.rows(session, ids, db_path=db_path, snapshots=snapshots)
    for e in out:
        e["row"] = {**e["row"], "citations": int(counts.get(e["project_id"], 0))}
    return out


FROZEN = "key-labels-frozen.jsonl"


def frozen(db_path: Path | str) -> dict[tuple[int, str], dict]:
    """The answer key's labels as they stood before the key corrected anything.

    `answers apply` copies the key's values onto the rows, after which every corrected
    row reads "right" and the key has nothing left to teach or to measure the score
    by. So before it corrects a campus it saves that campus's training entries (the
    claims, the row, the key's verdict on the value then held) here, and training reads
    these in place of the live rows. Keyed by (project, field).
    """
    path = models_dir(db_path) / FROZEN
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            e = json.loads(line)
            out[(int(e["project_id"]), e["field"])] = e
    return out


def with_frozen(rows: list[dict], db_path: Path | str) -> list[dict]:
    """`rows` with every frozen (project, field) replaced by its frozen entry."""
    saved = frozen(db_path)
    return [saved.get((e["project_id"], e["field"]), e) for e in rows]


def freeze(session: Any, db_path: Path | str) -> int:
    """Save the entry of every answered field not frozen yet. Returns how many.

    Answered fields only: a field frozen before it had an answer would hide the answer
    a later batch gives it.
    """
    from tracker import answer_key

    saved = frozen(db_path)
    pending = {(a.project_id, a.field) for a in answer_key.answers(session)} - set(saved)
    if not pending:
        return 0
    campuses = sorted({p for p, _ in pending})
    new = [
        e
        for e in entries(session, Path(db_path), campuses)
        if (e["project_id"], e["field"]) in pending
    ]
    path = models_dir(db_path) / FROZEN
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for e in new:
            fh.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
    return len(new)


def compare(
    sigs: list[dict[str, float]],
    y: list[int],
    groups: list[int],
    measure: list[bool],
    candidates: Sequence[Sequence[str]],
) -> list[float | None]:
    """Each candidate signal set's held-out AUC on the same labels, splits and rows."""
    return [
        heldout_auc(
            [[s[f] for f in names] for s in sigs], y, groups, repeats=REPEATS, measure=measure
        )
        for names in candidates
    ]


def calibrate(model: Logistic, lines: list[dict]) -> dict[str, Any]:
    """Map the model's log-odds onto what the passes found, from `record_outcomes`' log.

    A one-input logistic regression of each outcome on the model's log-odds for the
    signals the value had when it was ranked. Applied only when it keeps the order (a
    positive slope): it changes what the agent is told, never who is asked.
    """
    t, y = [], []
    for line in lines:
        sig = line.get("signals") or {}
        if line.get("wrong") not in (0, 1) or not all(f in sig for f in model.features):
            continue
        t.append([max(-30.0, min(30.0, model.logit(model.vector(sig))))])
        y.append(int(line["wrong"]))
    if len(y) < CALIBRATE_MIN or len(set(y)) < 2:
        model.calibration = None
        return {"calibrated": f"not yet: {len(y)} outcome(s) of {CALIBRATE_MIN}"}
    one = fit(t, y, l2=1e-6, features=["logit"])
    slope = one.weights[0] / one.scale[0]
    if slope <= 0:
        model.calibration = None
        return {"calibrated": f"no: on {len(y)} outcomes the ranking runs backwards"}
    model.calibration = {
        "slope": slope,
        "intercept": one.intercept - one.weights[0] * one.mean[0] / one.scale[0],
        "outcomes": len(y),
    }
    return {"calibrated": f"on {len(y)} outcomes", "outcome_auc": auc(y, [v[0] for v in t])}


def train_logistic(session: Any, db_path: Path, *, today: dt.date | None = None) -> dict[str, Any]:
    """Refit on the answer key's verdicts; keep it if it measures at least as well.

    The verdicts are the frozen ones where the key has already corrected the row
    (`frozen`): a corrected row reads "right" and would teach nothing. Every field's
    verdicts train it — the stage and announced date carry what being badly backed
    looks like too — and the measure is taken on the ranked fields alone. The saved
    model's signal set and the default are measured on the same labels and splits; the
    better one is refitted (the saved one on a tie) and kept if it clears `FLOOR`.
    """
    from tracker import answer_key

    key = {(a.project_id, a.field) for a in answer_key.answers(session)}
    rows = with_frozen(entries(session, db_path, sorted({p for p, _ in key})), db_path)
    sigs, y, groups, measure = [], [], [], []
    for e in rows:
        ans = e.get("answer") or {}
        if (e["project_id"], e["field"]) not in key or ans.get("verdict") not in (
            "right",
            "wrong",
            "imprecise",
        ):
            continue
        sig = signals(e, today)
        if sig is None:
            continue
        sigs.append(sig)
        y.append(int(ans["verdict"] == "wrong"))
        groups.append(e["project_id"])
        measure.append(e["field"] in SCORED)
    report: dict[str, Any] = {"labels": len(y), "campuses": len(set(groups))}
    if len(set(y)) < 2 or len(set(groups)) < 10:
        report["kept"] = "too few labels to fit; the previous model stays"
        return report
    previous = load(db_path)
    candidates = [list(FEATURES)]
    if previous is not None and list(previous.features) != list(FEATURES):
        candidates.insert(0, list(previous.features))
    scores = compare(sigs, y, groups, measure, candidates)
    best = max(range(len(candidates)), key=lambda i: -1.0 if scores[i] is None else scores[i])
    measured = scores[best]
    report["heldout_auc"] = measured
    if len(candidates) > 1:
        report["previous_signals_auc"] = scores[0]
        report["default_signals_auc"] = scores[1]
    if measured is None or measured < FLOOR:
        report["kept"] = f"held-out AUC {measured} below {FLOOR}; the previous model stays"
        return report
    names = candidates[best]
    model = fit([[s[f] for f in names] for s in sigs], y, features=names)
    model.heldout_auc = measured
    model.trained_at = (today or dt.date.today()).isoformat()
    report.update(calibrate(model, outcomes(db_path)))
    path = models_dir(db_path) / MODEL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model.as_json(), indent=1), encoding="utf-8")
    report["kept"] = "new" if best == len(candidates) - 1 else "new, on the saved model's signals"
    return report


def score(session: Any, db_path: Path, *, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Rank every scored field's held value, most suspicious first, and save the list.

    Each line keeps the value's signals and when it was scored, so what the night's
    passes then decide can be filed against what the ranking knew (`record_outcomes`).
    """
    from tracker.models import utcnow

    model = load(db_path)
    if model is None:
        return []
    scored_at = utcnow().isoformat()
    ranked = []
    for e in entries(session, db_path):
        if e["field"] not in SCORED:
            continue
        sig = signals(e, today)
        if sig is None:
            continue
        x = model.vector(sig)
        ranked.append(
            {
                "project_id": e["project_id"],
                "field": e["field"],
                "held": e["held"],
                "p_wrong": round(model.calibrated(x), 4),
                "order": model.logit(x),
                "why": [n for n, c in model.contributions(x)[:3] if c > 0],
                "signals": {k: round(v, 5) for k, v in sig.items()},
            }
        )
    # Ordered by the model's own log-odds: calibration may squeeze the probabilities
    # together but never reorders them.
    ranked.sort(key=lambda r: (-r["order"], r["project_id"], r["field"]))
    for r in ranked:
        del r["order"]
    night = (today or dt.date.today()).isoformat()
    path = Path(db_path).parent / "runs" / RANKED
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps({"night": night, "scored_at": scored_at, "rank": i + 1, **r}, default=str)
            + "\n"
            for i, r in enumerate(ranked)
        ),
        encoding="utf-8",
    )
    return ranked


def outcomes(db_path: Path | str) -> list[dict[str, Any]]:
    """Every outcome `record_outcomes` has filed, oldest first."""
    from tracker import labels

    return labels.read(db_path, OUTCOMES)


def record_outcomes(session: Any, db_path: Path | str) -> dict[str, Any]:
    """File what the night's passes decided about the values tonight's ranking scored.

    A value a pass changed is wrong; one it kept, or a suspect the agent left alone
    with its reason, is right; one it could not decide is not filed. Each line carries
    the signals and the probability the value had when it was ranked, before anything
    was decided — so the log is a test of the ranking with nothing leaking into it,
    which the answer key, built from the campuses carrying the most money, is not for
    the rest of the database. Run at the end of the night, before `train`; a rerun
    files nothing twice.
    """
    from sqlalchemy import select

    from tracker import labels
    from tracker.models import ModelDecline

    rows = ranked(db_path)
    if not rows or not rows[0].get("scored_at"):
        return {"recorded": 0, "outcomes": len(outcomes(db_path))}
    scored_at = str(rows[0]["scored_at"])
    night = rows[0]["night"]
    by_value = {(int(r["project_id"]), r["field"]): r for r in rows}
    filed = {(o["night"], int(o["project_id"]), o["field"]) for o in outcomes(db_path)}
    found: dict[tuple[int, str], dict[str, Any]] = {}
    for v in labels.read(db_path, labels.VERDICTS):
        if str(v.get("at", "")) < scored_at or v.get("by") not in ("logic", "audit"):
            continue
        where = (int(v["project_id"]), v.get("field"))
        r = by_value.get(where)
        if (
            r is None
            or (night, *where) in filed
            or not _same({"value": v.get("old"), "number": v.get("old")}, r["held"])
        ):
            continue
        wrong = {"changed": 1, "kept": 0}.get(v.get("verdict"))
        if where not in found or (found[where]["wrong"] is None and wrong is not None):
            found[where] = {"wrong": wrong, "how": v.get("verdict"), "by": v.get("by")}
    since = dt.datetime.fromisoformat(scored_at)
    for (pid, f), seen in found.items():
        if seen["wrong"] is not None:
            continue
        decline = session.scalars(
            select(ModelDecline).where(
                ModelDecline.kind == "logic", ModelDecline.subject == f"{pid}:{CODE_PREFIX}{f}"
            )
        ).first()
        if decline is not None and decline.outcome == "left alone" and decline.decided_at >= since:
            seen.update(wrong=0, how="left alone")
    lines = [
        {
            "night": night,
            "project_id": pid,
            "field": f,
            "held": by_value[(pid, f)]["held"],
            "rank": by_value[(pid, f)]["rank"],
            "p_wrong": by_value[(pid, f)]["p_wrong"],
            "signals": by_value[(pid, f)].get("signals") or {},
            **seen,
        }
        for (pid, f), seen in sorted(found.items())
        if seen["wrong"] is not None
    ]
    if lines:
        path = labels.path_of(db_path, OUTCOMES)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
    every = outcomes(db_path)
    report: dict[str, Any] = {"recorded": len(lines), "outcomes": len(every)}
    ranking_auc = auc([int(o["wrong"]) for o in every], [float(o["p_wrong"]) for o in every])
    if ranking_auc is not None:
        report["ranking_auc"] = round(ranking_auc, 3)
    return report


def ranked(db_path: Path | str) -> list[dict[str, Any]]:
    path = Path(db_path).parent / "runs" / RANKED
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def suspects(
    db_path: Path | str,
    n: int,
    *,
    seed: str = "",
    session: Any = None,
    again: bool = False,
) -> list[Any]:
    """Tonight's suspects as logic findings: the top of the ranking plus a random fifth.

    The random share is drawn from the rest of the list with a seed of the night, so a
    rerun the same night offers the same values and the decline memory holds.

    **With a `session`, every slot buys a fresh question.** A value already put to the
    agent on the same evidence (`declines.value_evidence`), one the row's notes record
    as answered, one no live claim states any more, or a field now empty is passed over
    and the next one taken — from the top for the top share, from the shuffled rest for
    the random share. On 2026-11-11 the selection did none of this: 30 rows had taken
    172 of 241 re-reads, because a value the agent left alone came back the next night
    as a new question. Each finding also names the citations that state the held value,
    so the agent starts from them instead of looking for them in a long list.
    """
    from tracker.logic import Finding

    rows = ranked(db_path)
    if not rows or n <= 0:
        return []
    open_ = _still_open(session, again)
    top = max(n - round(n * RANDOM_SHARE), 0)
    chosen: list[dict[str, Any]] = []
    walked = 0
    while walked < len(rows) and len(chosen) < top:
        if open_(rows[walked]):
            chosen.append(rows[walked])
        walked += 1
    rest = rows[walked:]
    rng = random.Random(seed or rows[0].get("night", ""))
    for r in rng.sample(rest, len(rest)):
        if len(chosen) >= n:
            break
        if open_(r):
            chosen.append(r)
    out = []
    for r in chosen:
        drivers = ", ".join(r.get("why") or []) or "no single signal"
        carriers = _carriers(session, r) if session is not None else []
        stated_by = (
            f" The citation(s) stating it: {', '.join(str(i) for i in carriers)} — "
            f"`list_sources` with field={r['field']} shows each one's sentence."
            if carriers
            else ""
        )
        out.append(
            Finding(
                project_id=int(r["project_id"]),
                code=CODE_PREFIX + r["field"],
                severity="warning",
                summary=(
                    f"the suspicion score puts this row's {r['field']} ({r['held']}) at "
                    f"{r['p_wrong']:.0%} likely wrong (rank {r['rank']} of {len(rows)}; "
                    f"driven by {drivers}). Re-read what the figure is of: this campus or "
                    f"another, the whole plan or one phase, the data center or its power plant, "
                    f"a stated plan or a potential.{stated_by}"
                ),
                fields=(r["field"],),
                remedy="keep it if the sources state it for this campus; otherwise rule out the "
                "claims behind it",
            )
        )
    return out


def _still_open(session: Any, again: bool) -> Any:
    """`row -> bool`: whether a ranked value is still worth a re-read tonight.

    Without a session every value is (the ranking alone, as a dry listing reads it).
    """
    if session is None or again:
        return lambda r: True

    from tracker import declines
    from tracker.audit import settled_codes
    from tracker.models import Project
    from tracker.triage import has_live_claim

    known = declines.load(session, "logic")
    settled: dict[int, set[str]] = {}

    def open_(r: dict[str, Any]) -> bool:
        project = session.get(Project, int(r["project_id"]))
        field = str(r["field"])
        if project is None or getattr(project, field, None) is None:
            return False
        code = CODE_PREFIX + field
        if project.id not in settled:
            settled[project.id] = settled_codes(project)
        if code in settled[project.id]:
            return False
        probe = _Probe(project.id, code, (field,))
        if not has_live_claim(project, probe):
            return False
        return not declines.holds(
            known,
            f"{project.id}:{code}",
            lambda: declines.value_evidence(project, field),
        )

    return open_


@dataclass(frozen=True)
class _Probe:
    """The parts of a finding the selection's checks read, before the finding exists."""

    project_id: int
    code: str
    fields: tuple[str, ...]


def _carriers(session: Any, r: dict[str, Any]) -> list[int]:
    """Ids of the citations whose live claim is the value the row holds."""
    from tracker.models import Project
    from tracker.upsert import DECIDED_REASONS, _same, claim_value

    project = session.get(Project, int(r["project_id"]))
    field = str(r["field"])
    held = getattr(project, field, None) if project is not None else None
    if held is None:
        return []
    out = []
    for source in project.sources or ():
        try:
            claims = json.loads(source.claims or "{}")
            reasons = json.loads(source.unconfirmed_reasons or "{}") or {}
        except (TypeError, ValueError):
            continue
        if not isinstance(claims, dict) or claims.get(field) is None:
            continue
        if isinstance(reasons, dict) and reasons.get(field) in DECIDED_REASONS:
            continue
        if _same(claims[field], claim_value(held)):
            out.append(source.id)
    return sorted(out)


def check_row(session: Any, db_path: Path, project_id: int) -> dict[str, Any]:
    """Both models' view of one row, field by field: what an agent sees when it asks."""
    from tracker import suspect_transformer

    model = load(db_path)
    rows = entries(session, db_path, [project_id])
    transformer = suspect_transformer.check_row(rows, db_path)
    out: dict[str, Any] = {"project_id": project_id, "fields": {}}
    for e in rows:
        sig = signals(e)
        view: dict[str, Any] = {"held": e["held"]}
        if model is not None and sig is not None:
            x = model.vector(sig)
            view["logistic_p_wrong"] = round(model.calibrated(x), 3)
            view["logistic_why"] = [(n, round(c, 2)) for n, c in model.contributions(x)[:3]]
        if e["field"] in transformer:
            view["transformer_p_wrong"] = transformer[e["field"]]
        view["trusted_field"] = e["field"] in SCORED
        out["fields"][e["field"]] = view
    out["note"] = (
        "p_wrong ranks values for re-checking; it is not a verdict. It is reliable "
        "only for mw_planned, investment_usd and expected_online."
    )
    return out


__all__ = [
    "FEATURES",
    "FLOOR",
    "KNOWN",
    "SCORED",
    "SIGNALS",
    "Logistic",
    "auc",
    "calibrate",
    "check_row",
    "compare",
    "entries",
    "fit",
    "heldout_auc",
    "load",
    "outcomes",
    "ranked",
    "record_outcomes",
    "score",
    "signals",
    "suspects",
    "train_logistic",
]
