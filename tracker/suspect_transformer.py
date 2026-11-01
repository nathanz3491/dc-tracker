"""The suspicion score's second model: a small transformer over a row's five fields.

Optional: it needs PyTorch, the `head` extra. Without it `train` and `check_row` say so
and do nothing, and the logistic regression (`tracker.suspect`) carries on alone.

**Shape.** One token per field (stage, planned MW, investment, online date, announced
date), each the backing of one candidate value: how many statements and publishers stand
behind it, whether a filing does, how many claim otherwise and what share agrees, how
old its support is, how far apart the competing figures are and whether it is the
largest or smallest, how many places the row's sources name, its size, and twelve flags
read from its quotes ("two campuses", "up to", "at least", "over 20 years", "power
plant", "critical IT", "has invested", bonds and incentives, no quote at all). Three
transformer blocks let the fields attend to each other; each field's output is P(wrong).

**What it learns from.** Never the answer key, which only measures it. The tracker's
own rejections: a value every statement of which a decision ruled out is "wrong", the
held value is "right". No token reads a tier, because a rejected claim's own tier would
give its label away. Measured 2026-11-01 on the key's 205 labels: AUC 0.75 to 0.78 without
ever seeing a key label, level with the logistic regression, and best on planned MW
(0.85). A refit replaces the saved model only if it ranks the key at least as well as
the saved one does on the same key.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

FIELDS = ("phase", "mw_planned", "investment_usd", "expected_online", "first_announced")
MODEL = "suspect-transformer.pt"
SEEDS = 3
FLAGS = (
    r"\b(two|three|four|five|several|multiple|both)\b[^.]{0,40}\b(campus(es)?|sites|data cent(er|re)s|locations|facilities)\b|\bcampuses\b",
    r"\b(statewide|nationwide|region(al)?|across (the )?(state|county|region)|portfolio|throughout)\b",
    r"\b(phase|first building|initial|first (two|data) (buildings|halls))\b",
    r"\b(up to|potential(ly)?|could|as much as|path to|ultimately|eventually|scale to|full build)",
    r"\b(at least|more than|minimum|exceed\w*|over \$?\d)",
    r"\b(estimat\w*|project(ed|ion)s?|anticipat\w*|roughly|approximately)\b",
    r"\b(over|within) (the next )?(\w+ )?(years|decades?)\b|\b\d+ years\b",
    r"\b(power plant|generation|gas[- ]fired|turbines?|nuclear|reactors?|solar|batter(y|ies)|transmission)\b",
    r"\b(critical it|it load|it capacity)\b",
    r"\b(has invested|already (invested|spent)|spent|to date)\b",
    r"\b(bonds?|notes|financing|loan|credit|abatement|incentive)\b",
)
_FLAG_RE = [re.compile(f, re.I) for f in FLAGS]
D = 12 + len(FLAGS) + 1 + 1


def available() -> bool:
    try:
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


def _number(c: dict) -> float | None:
    try:
        return float(c["number"]) if c.get("number") not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _same(a: dict, b: dict) -> bool:
    x, y = _number(a), _number(b)
    if x is not None and y is not None:
        return abs(x - y) <= 0.05 * max(abs(x), abs(y), 1e-9)
    return (
        str(a.get("value") or "").strip().lower()[:10]
        == str(b.get("value") or "").strip().lower()[:10]
    )


def groups(claims: list[dict]) -> list[list[dict]]:
    """Claims grouped by the value they state (figures within 5% are one value)."""
    out: list[list[dict]] = []
    for c in claims:
        for g in out:
            if _same(g[0], c):
                g.append(c)
                break
        else:
            out.append([c])
    return out


def token(entry: dict, group: list[dict], places: int, today: dt.date) -> list[float]:
    every = entry.get("claims") or []
    nums = [n for n in map(_number, every) if n is not None and n > 0]
    mine = _number(group[0])
    dates = []
    for c in group:
        try:
            dates.append(dt.date.fromisoformat(str(c.get("as_of"))[:10]))
        except ValueError:
            continue
    age = max((today - max(dates)).days, 0) if dates else 365
    above = sum(n > mine * 1.05 for n in nums) if mine else 0
    below = sum(n < mine * 0.95 for n in nums) if mine else 0
    text = " ".join(c.get("quote") or "" for c in group)
    return (
        [
            math.log1p(len(group)),
            float(len({urlparse(c.get("source_url") or "").netloc for c in group})),
            float(any(c.get("source_type") in ("company_filing", "government_doc") for c in group)),
            math.log1p(len(every) - len(group)),
            len(group) / max(len(every), 1),
            math.log1p(age),
            math.log1p((entry.get("row") or {}).get("citations", 0)),
            math.log(max(nums) / min(nums)) if nums else 0.0,
            float(mine is not None and above == 0 and below > 0),
            float(mine is not None and below == 0 and above > 0),
            math.log1p(places),
            math.log10(mine) if mine and mine > 0 else 0.0,
        ]
        + [float(bool(r.search(text))) for r in _FLAG_RE]
        + [float(not text.strip()), 1.0]
    )


def _by_row(entries: list[dict]) -> dict[int, dict[str, dict]]:
    out: dict[int, dict[str, dict]] = {}
    for e in entries:
        out.setdefault(e["project_id"], {})[e["field"]] = e
    return out


def _places(fields: dict[str, dict]) -> int:
    row = next(iter(fields.values())).get("row") or {}
    distinct = row.get("distinct") or {}
    return int(distinct.get("city", 0)) + int(distinct.get("county", 0))


def _held_tokens(fields: dict[str, dict], today: dt.date):
    """Five tokens for a row as it stands: each field's held value."""
    toks, present = [], []
    for f in FIELDS:
        e = fields.get(f)
        held = [c for c in (e or {}).get("claims", []) if c.get("held")]
        if e is None or not held:
            toks.append([0.0] * D)
            present.append(False)
            continue
        group = next((g for g in groups(e["claims"]) if any(c.get("held") for c in g)), held)
        toks.append(token(e, group, _places(fields), today))
        present.append(True)
    return toks, present


def _model(torch):
    nn = torch.nn

    class Encoder(nn.Module):
        def __init__(self, d: int = 16, blocks: int = 3):
            super().__init__()
            self.inp = nn.Linear(D, d)
            self.field = nn.Embedding(5, d)
            layer = nn.TransformerEncoderLayer(
                d, nhead=2, dim_feedforward=32, dropout=0.1, batch_first=True
            )
            self.blocks = nn.TransformerEncoder(layer, blocks, enable_nested_tensor=False)
            self.out = nn.Linear(d, 1)

        def forward(self, x):
            return self.out(self.blocks(self.inp(x) + self.field.weight[None])).squeeze(-1)

    return Encoder


def _weak_samples(entries: list[dict], exclude: set[int], today: dt.date, rng) -> tuple[list, list]:
    """Rows outside the key: one candidate per field, labelled by the tracker's own rulings."""
    X, Y = [], []
    for pid, fields in _by_row(entries).items():
        if pid in exclude:
            continue
        per_field = []
        for f in FIELDS:
            e = fields.get(f)
            cands = []
            for g in groups((e or {}).get("claims", [])):
                if any(c.get("held") for c in g):
                    cands.append((g, 0))
                elif all(c.get("tier") == "ruled_out" for c in g):
                    cands.append((g, 1))
            per_field.append((e, cands))
        if not any(c for _, c in per_field):
            continue
        has_wrong = any(lab for _, c in per_field for _, lab in c)
        for _ in range(4 if has_wrong else 1):
            toks, labs = [], []
            for e, cands in per_field:
                if not cands:
                    toks.append([0.0] * D)
                    labs.append(-1.0)
                    continue
                wrong = [c for c in cands if c[1] == 1]
                g, lab = (
                    wrong[rng.randrange(len(wrong))]
                    if wrong and rng.random() < 0.5
                    else cands[rng.randrange(len(cands))]
                )
                toks.append(token(e, g, _places(fields), today))
                labs.append(float(lab))
            X.append(toks)
            Y.append(labs)
    return X, Y


def _key_set(entries: list[dict], today: dt.date):
    X, Y, P = [], [], []
    for pid, fields in _by_row(entries).items():
        labs = []
        for f in FIELDS:
            ans = (fields.get(f) or {}).get("answer") or {}
            labs.append(
                float(ans["verdict"] == "wrong")
                if ans.get("verdict") in ("right", "wrong", "imprecise")
                else -1.0
            )
        if all(v < 0 for v in labs):
            continue
        toks, present = _held_tokens(fields, today)
        X.append(toks)
        Y.append([v if ok else -1.0 for v, ok in zip(labs, present, strict=True)])
        P.append(pid)
    return X, Y, P


def _auc(torch, scores, Y) -> float | None:
    from tracker.suspect import auc

    y = Y.flatten()
    s = scores.flatten()
    mask = y >= 0
    return auc([int(v) for v in y[mask].tolist()], s[mask].tolist())


def train(entries: list[dict], db_path: Path, *, today: dt.date | None = None) -> dict[str, Any]:
    """Pretrain on the tracker's rulings; keep it if it ranks the key at least as well."""
    if not available():
        return {"kept": "skipped: PyTorch is not installed (the `head` extra)"}
    import random

    import torch

    today = today or dt.date.today()
    torch.set_num_threads(max(1, (torch.get_num_threads() or 2) - 1))
    kx, ky, kp = _key_set(entries, today)
    X, Y = _weak_samples(entries, set(kp), today, random.Random(0))
    if len(X) < 200 or not kx:
        return {"kept": f"too little to train on ({len(X)} weak samples, {len(kx)} key rows)"}
    X, Y = torch.tensor(X), torch.tensor(Y)
    flat = X.reshape(-1, D)
    live = flat[flat[:, -1] > 0]
    mu, sd = live.mean(0), live.std(0) + 1e-6
    mu[-1], sd[-1] = 0.0, 1.0

    def norm(t):
        return (t - mu) / sd * (t[..., -1:] > 0)

    Encoder = _model(torch)
    models = []
    for seed in range(SEEDS):
        torch.manual_seed(seed)
        m = Encoder()
        opt = torch.optim.AdamW(m.parameters(), lr=3e-3, weight_decay=1e-2)
        x = norm(X)
        for _ in range(40):
            m.train()
            for idx in torch.randperm(len(x)).split(256):
                logits = m(x[idx])
                mask = Y[idx] >= 0
                loss = torch.nn.functional.binary_cross_entropy_with_logits(
                    logits[mask], Y[idx][mask]
                )
                opt.zero_grad()
                loss.backward()
                opt.step()
        models.append(m.eval())
    KX, KY = torch.tensor(kx), torch.tensor(ky)
    with torch.no_grad():
        new_scores = torch.stack([torch.sigmoid(m(norm(KX))) for m in models]).mean(0)
    new_auc = _auc(torch, new_scores, KY)
    report: dict[str, Any] = {"weak_samples": len(X), "key_rows": len(kx), "key_auc": new_auc}
    path = Path(db_path).parent / "models" / MODEL
    old = _load(torch, path)
    if old is not None:
        with torch.no_grad():
            old_auc = _auc(torch, _predict(torch, old, KX), KY)
        report["previous_key_auc"] = old_auc
        if new_auc is None or (old_auc is not None and new_auc < old_auc):
            report["kept"] = "the previous model ranks the key better; it stays"
            return report
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "states": [m.state_dict() for m in models],
            "mu": mu,
            "sd": sd,
            "key_auc": new_auc,
            "trained_at": today.isoformat(),
        },
        path,
    )
    report["kept"] = "new"
    return report


def _load(torch, path: Path):
    if not path.is_file():
        return None
    try:
        saved = torch.load(path, weights_only=False)
        Encoder = _model(torch)
        models = []
        for state in saved["states"]:
            m = Encoder()
            m.load_state_dict(state)
            models.append(m.eval())
        return {"models": models, "mu": saved["mu"], "sd": saved["sd"]}
    except Exception:
        return None


def _predict(torch, loaded, X):
    x = (X - loaded["mu"]) / loaded["sd"] * (X[..., -1:] > 0)
    return torch.stack([torch.sigmoid(m(x)) for m in loaded["models"]]).mean(0)


def check_row(entries: list[dict], db_path: Path | str) -> dict[str, float]:
    """{field: P(wrong)} for one row's held values, or {} without PyTorch or a model."""
    if not entries or not available():
        return {}
    import torch

    loaded = _load(torch, Path(db_path).parent / "models" / MODEL)
    if loaded is None:
        return {}
    toks, present = _held_tokens(_by_row(entries)[entries[0]["project_id"]], dt.date.today())
    with torch.no_grad():
        p = _predict(torch, loaded, torch.tensor([toks]))[0].tolist()
    return {f: round(v, 3) for f, v, ok in zip(FIELDS, p, present, strict=True) if ok}


__all__ = ["available", "check_row", "groups", "token", "train"]
