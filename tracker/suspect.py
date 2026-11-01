"""The suspicion score: how likely each stored value is to be wrong, to aim re-checks.

**What it is for.** A value's quote check says a sentence exists; it cannot say the
sentence is about this campus, this phase or this kind of figure. The answer key found
41% of the values it judged wrong, every one with a real sentence behind it. Re-reading
everything is not affordable, so this ranks the values and the nightly loop re-reads
the top of the list (plus a random fifth, so the record does not only confirm the
ranking). The score never writes a value. It aims a pass that does — `logic resolve
--suspects N`, the agent that re-reads sources and rules — and that pass's answers go
into the verdict log (`tracker.labels`), which is what the next model learns from.

**Two models, one in charge.** The logistic regression ranks, at the start of the
night: nine readable signals about how a value is backed (how many statements and
publishers stand behind it, whether only directories do, whether a filing does, how
many sources claim something else, how much of the row's evidence was ruled out, how
old the newest support is, how cited the row is) plus the field. Measured on 205
answer-key labels from 60 campuses, scored on campuses it never saw: AUC 0.76, against
0.64 for the field alone; bigger models overfit (forest 0.74, boosting 0.69). Written
in plain Python because the host has no numpy, and 15 weights do not need it.
The transformer (`tracker.suspect_transformer`, the optional `head` extra) is retrained
beside it each night and is there for an agent to consult on one row; it ranks nothing
until it clearly beats the logistic on the key.

**Which fields.** Planned MW, investment and the online date, where the score finds
wrong values two times in three or better. Not the stage (its errors are an unrecorded
stop, which a date check finds better) and not the first-announced date (its errors
are about which facility a date belongs to, which no backing signal shows: AUC 0.54).

**Retraining.** `train` refits at the end of each night on the answer key's verdicts on
the held values. The key is the only clean label set, so the logistic's held-out AUC is
measured by refitting with whole campuses left out, and a refit is kept only if that
measurement clears `FLOOR`; otherwise the previous weights stay.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

FIELDS = ("phase", "mw_planned", "investment_usd", "expected_online", "first_announced")
#: The fields the score ranks for re-checking. See the module docstring for the others.
SCORED = ("mw_planned", "investment_usd", "expected_online")
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
FEATURES = SIGNALS + tuple(f"field_{f}" for f in FIELDS)
PRIMARY = {"company_filing", "government_doc"}
#: A refit whose held-out AUC falls below this keeps the previous weights.
FLOOR = 0.65
#: The share of each night's suspects drawn at random rather than from the top.
RANDOM_SHARE = 0.2
MODEL = "suspect-logistic.json"
RANKED = "suspicion.jsonl"
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


@dataclass
class Logistic:
    """Standardised features, L2-penalised logistic regression. 15 weights."""

    weights: list[float]
    intercept: float
    mean: list[float]
    scale: list[float]
    labels: int = 0
    heldout_auc: float | None = None
    trained_at: str = ""

    def z(self, x: Sequence[float]) -> list[float]:
        return [(v - m) / s for v, m, s in zip(x, self.mean, self.scale, strict=True)]

    def prob(self, x: Sequence[float]) -> float:
        t = self.intercept + sum(w * v for w, v in zip(self.weights, self.z(x), strict=True))
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, t))))

    def contributions(self, x: Sequence[float]) -> list[tuple[str, float]]:
        """Each feature's push on the log-odds, largest first."""
        parts = [(n, w * v) for n, w, v in zip(FEATURES, self.weights, self.z(x), strict=True)]
        return sorted(parts, key=lambda p: -abs(p[1]))

    def as_json(self) -> dict[str, Any]:
        return {
            "features": list(FEATURES),
            "weights": self.weights,
            "intercept": self.intercept,
            "mean": self.mean,
            "scale": self.scale,
            "labels": self.labels,
            "heldout_auc": self.heldout_auc,
            "trained_at": self.trained_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Logistic:
        if list(data.get("features") or []) != list(FEATURES):
            raise ValueError("the saved model was trained on other features")
        return cls(
            data["weights"],
            data["intercept"],
            data["mean"],
            data["scale"],
            data.get("labels", 0),
            data.get("heldout_auc"),
            data.get("trained_at", ""),
        )


def fit(X: list[list[float]], y: list[int], *, l2: float = 2.0, steps: int = 3000) -> Logistic:
    """Gradient descent on the mean log-loss plus `l2`/n times the squared weights."""
    n, k = len(X), len(X[0])
    mean = [sum(r[j] for r in X) / n for j in range(k)]
    scale = [max(math.sqrt(sum((r[j] - mean[j]) ** 2 for r in X) / n), 1e-9) for j in range(k)]
    Z = [[(r[j] - mean[j]) / scale[j] for j in range(k)] for r in X]
    w, b, lr = [0.0] * k, 0.0, 0.5
    for _ in range(steps):
        gw, gb = [l2 * wj / n for wj in w], 0.0
        for row, target in zip(Z, y, strict=True):
            t = b + sum(wj * v for wj, v in zip(w, row, strict=True))
            err = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, t)))) - target
            gb += err / n
            for j in range(k):
                gw[j] += err * row[j] / n
        b -= lr * gb
        w = [wj - lr * g for wj, g in zip(w, gw, strict=True)]
    return Logistic(w, b, mean, scale, labels=n)


def auc(y: Sequence[int], p: Sequence[float]) -> float | None:
    """Area under the ROC curve: the chance a wrong value outranks a right one."""
    pos = [s for s, t in zip(p, y, strict=True) if t]
    neg = [s for s, t in zip(p, y, strict=True) if not t]
    if not pos or not neg:
        return None
    wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def heldout_auc(X, y, groups, folds: int = 5) -> float | None:
    """AUC with every campus scored by a model fitted without it."""
    campuses = sorted(set(groups))
    random.Random(0).shuffle(campuses)
    fold_of = {c: i % folds for i, c in enumerate(campuses)}
    scores = [0.0] * len(y)
    for f in range(folds):
        train = [i for i, g in enumerate(groups) if fold_of[g] != f]
        test = [i for i, g in enumerate(groups) if fold_of[g] == f]
        if not test or len({y[i] for i in train}) < 2:
            continue
        model = fit([X[i] for i in train], [y[i] for i in train])
        for i in test:
            scores[i] = model.prob(X[i])
    return auc(y, scores)


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


def train_logistic(session: Any, db_path: Path, *, today: dt.date | None = None) -> dict[str, Any]:
    """Refit on the answer key's verdicts; keep it if its held-out AUC clears `FLOOR`.

    The verdicts are the frozen ones where the key has already corrected the row
    (`frozen`): a corrected row reads "right" and would teach nothing.
    """
    from tracker import answer_key

    key = {(a.project_id, a.field) for a in answer_key.answers(session)}
    rows = with_frozen(entries(session, db_path, sorted({p for p, _ in key})), db_path)
    X, y, groups = [], [], []
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
        X.append([sig[f] for f in FEATURES])
        y.append(int(ans["verdict"] == "wrong"))
        groups.append(e["project_id"])
    report: dict[str, Any] = {"labels": len(y), "campuses": len(set(groups))}
    if len(set(y)) < 2 or len(set(groups)) < 10:
        report["kept"] = "too few labels to fit; the previous model stays"
        return report
    measured = heldout_auc(X, y, groups)
    report["heldout_auc"] = measured
    if measured is None or measured < FLOOR:
        report["kept"] = f"held-out AUC {measured} below {FLOOR}; the previous model stays"
        return report
    model = fit(X, y)
    model.heldout_auc = measured
    model.trained_at = (today or dt.date.today()).isoformat()
    path = models_dir(db_path) / MODEL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model.as_json(), indent=1), encoding="utf-8")
    report["kept"] = "new"
    return report


def score(session: Any, db_path: Path, *, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Rank every scored field's held value, most suspicious first, and save the list."""
    model = load(db_path)
    if model is None:
        return []
    ranked = []
    for e in entries(session, db_path):
        if e["field"] not in SCORED:
            continue
        sig = signals(e, today)
        if sig is None:
            continue
        x = [sig[f] for f in FEATURES]
        ranked.append(
            {
                "project_id": e["project_id"],
                "field": e["field"],
                "held": e["held"],
                "p_wrong": round(model.prob(x), 4),
                "why": [n for n, c in model.contributions(x)[:3] if c > 0],
            }
        )
    ranked.sort(key=lambda r: (-r["p_wrong"], r["project_id"], r["field"]))
    night = (today or dt.date.today()).isoformat()
    path = Path(db_path).parent / "runs" / RANKED
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps({"night": night, "rank": i + 1, **r}) + "\n" for i, r in enumerate(ranked)
        ),
        encoding="utf-8",
    )
    return ranked


def ranked(db_path: Path | str) -> list[dict[str, Any]]:
    path = Path(db_path).parent / "runs" / RANKED
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def suspects(db_path: Path | str, n: int, *, seed: str = "") -> list[Any]:
    """Tonight's suspects as logic findings: the top of the ranking plus a random fifth.

    The random share is drawn from the rest of the list with a seed of the night, so a
    rerun the same night offers the same values and the decline memory holds.
    """
    from tracker.logic import Finding

    rows = ranked(db_path)
    if not rows or n <= 0:
        return []
    top = max(n - round(n * RANDOM_SHARE), 0)
    chosen = rows[:top]
    rest = rows[top:]
    rng = random.Random(seed or rows[0].get("night", ""))
    chosen += rng.sample(rest, min(n - len(chosen), len(rest)))
    out = []
    for r in chosen:
        drivers = ", ".join(r.get("why") or []) or "no single signal"
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
                    f"a stated plan or a potential."
                ),
                fields=(r["field"],),
                remedy="keep it if the sources state it for this campus; otherwise rule out the "
                "claims behind it",
            )
        )
    return out


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
            x = [sig[f] for f in FEATURES]
            view["logistic_p_wrong"] = round(model.prob(x), 3)
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
    "SCORED",
    "Logistic",
    "auc",
    "check_row",
    "entries",
    "fit",
    "heldout_auc",
    "load",
    "ranked",
    "score",
    "signals",
    "suspects",
    "train_logistic",
]
