# Plan — stop losing evidence at the gate

Written 2026-08-05, after a live `sync` run produced a wall of
`evidence quote for 'X' is not in the article; ignoring` and the question "are we
losing a lot of data?".

> **Status, 2026-08-06.** A2, A1, B and C are implemented. **D is deliberately
> not**, and the measurement is now on record rather than assumed: over 1,250
> stored quotes, **98.7% are exact substrings of their own article** and recovery
> is needed for 0.5%. The matching is not what refuses values, so relaxing it
> would be the guessing this plan's own ordering forbids. `scripts/
> measure_evidence_gate.py` re-runs that measurement and the mandatory negative
> control (0 of 3,064 crossings into another publisher's article), so when a real
> run's A2 logs say otherwise the experiment is repeatable rather than
> re-derived.
>
> Two findings below turned out to be wrong on measurement, and the corrections
> are recorded inline at **A1** and **D**.

## What was measured, before proposing anything

Three findings, and the first two are the reason this plan does **not** lead with
"loosen the matching".

**1. Values are not destroyed — but one path does destroy, and one cost got worse.**
A field whose quote fails is re-added to `claims` as 待确认
([crawl.py:1177–1181](../tracker/ingest/crawl.py)): stored, displayed,
resolvable. What is lost is the quote, the confidence uplift and the 9-of-12
count. Two exceptions matter:

- **A risk whose quote fails is dropped outright** —
  [crawl.py:1049–1064](../tracker/ingest/crawl.py) has no 待确认 path, unlike
  every field. That is real data loss.
- **An unconfirmed `investment_usd` is now excluded from the capex sums**, because
  the exclusion added this week reads exactly that flag. A rejected money quote
  used to mean "shown as 待确认"; it now means "silently out of the table".

**2. On real corpora the failure does not reproduce.** Instrumented replay of the
live extraction path over cached articles: **22 quotes / 6 trade-press articles →
100% exact substring; 5 quotes / 4 EDGAR filings → 100% exact.** `_verbatim_run`
was never needed. So "the model paraphrased slightly" is not the base-rate driver
— the existing recovery (40 chars / 50% of the quote, widened to sentence edges)
already covers ordinary articles.

A hypothesis worth recording as dead: in-sentence markdown decoration
(`**200 MW**`) breaking substring containment. Measured across 500 cached
articles — bold/emphasis 0%, markdown links 0%, nbsp 0%. Not the cause.

**3. The driver is that the model is being handed pages that are not articles.**
**6% of the cache is under 1,200 characters** and there is **no minimum body
length anywhere before extraction**:

```
    8 chars   "Document"
  552 chars   a Xiaohongshu page shell — copyright, address, phone
  598 chars   "Applied Digital has signed a 210 MW lease at Delta Forge 2... Read More >>"   (×8)
```

The model receives a teaser card, invents a plausible project from the title, and
then *every* quote fails — which is precisely the log signature reported:
`company / city / county / state / phase` failing together, alongside fetches of
`microsoft.com/en-mt/store/b/imprint` and `local.microsoft.com/communities/…`.
Worse, [crawl.py:1144–1148](../tracker/ingest/crawl.py) then restores the identity
fields from the *ungated* values, so **a row is created anyway** with zero
confirmed fields.

**In those cases the gate is correct.** Most of the visible "loss" is it refusing
unsourced claims. That is why the ordering below puts matching last.

---

## A. Refuse pages with nothing to quote, and make the warning actionable

The cheapest change and the largest share of the symptom. Two parts, neither
touching any evidence standard.

**A1. A minimum body length before extraction.** A fetch that returns 200 and 600
characters of navigation furniture is not an article. Add a `thin_content`
outcome status in `crawl.py` beside `no_project` / `llm_error`, refused *before*
the LLM call — it saves the call and prevents the phantom row.

- Threshold ~1,000 characters of prose, measured after stripping boilerplate, and
  named as a module constant with the measurement above in its docstring.
- Must not fire on legitimately short SEC 8-K excerpts: the corpus has a 590-char
  Meta 8-K that is real content. So the check belongs on the *news* fetch path,
  or the constant needs a per-source-type floor with `company_filing` exempt.
  Decide by measuring the length distribution per `source_type` first.

> **Corrected on measurement, 2026-08-06.** The per-source-type remedy proposed
> above is exactly backwards for this corpus, and measuring first is what caught
> it. Length by `source_type` over 544 cached articles: trade press has a floor of
> 3,927 raw characters and never comes near any threshold, while **every short
> page is `company_filing`** — 21 under 800 characters. Exempting that type, or
> putting the check only on the news path, would have exempted precisely the eight
> Applied Digital teasers that caused the symptom.
>
> Nor can raw length separate them at all: the real Meta 8-K is 590 characters and
> the teaser card is 598. The eight characters run the wrong way.
>
> What does separate them is **prose** — characters in lines long enough to be
> sentences (≥60). The 8-K scores 553; all fifteen cached teaser cards score 74,
> that being the single site-wide banner line. So: one floor, no per-type
> exemption, set at 200. It refuses 20 of 544 (3.7%), all read and confirmed, and
> only 1 of 115 SEC filings — a bare revenue table with no sentence in it.
>
> One further correction: measure prose in **characters, not words**. A word count
> scores every Chinese-language article at zero, so `thin_content` on a real
> 4,392-character article would be a false reason stored in the database. The
> accepted cost is recorded in the `MIN_PROSE_CHARS` docstring: a character floor
> is stricter for Chinese, where the same meaning fits in a third of the space.
- `ingest_url.status = 'thin_content'` is not `ok`, so `--retry-failed` can pick
  these up if a site later serves the full body; and `tracker queue --failed`
  shows them grouped by host, which is how the 8 identical Applied Digital
  teasers become one visible pattern instead of eight silent charges.

**A2. Log what was actually rejected.** Today the warning names the field and
nothing else, so it cannot be acted on — that is why answering this question
needed an instrumented replay. Change the one line in `evidence_gate` (and its
twin in `_risks`) to log the quote, truncated, plus the longest verbatim run and
its fraction:

```
evidence quote for 'investment_usd' is not in the article (best run 31 of 88 chars, 35%);
  offered: "Vantage will invest $2.5 billion in the Frontier campus over three phases"
```

Two lines of code. It turns every subsequent real run into the dataset this
question deserved, and it is the prerequisite for tuning anything in section D
against evidence rather than intuition.

## B. Give a risk the same 待confirm dignity as a field

A risk with an unverifiable quote is currently deleted. Every *field* in the same
position is kept and flagged. Align them: keep the risk, mark it unconfirmed,
and let it be visible and resolvable.

- `RiskRecord` needs an `unconfirmed` flag (or reuse the existing severity floor:
  keep at `watch`, which the code already does for an unrecognised severity, and
  add the flag for provenance).
- `tracker risks` already prints `uncited — confirm in tracker review` for risks
  with no quote, so the display path exists; this makes the *stored* row match
  that vocabulary instead of vanishing.
- `exposure` and the capex `mw_at_risk` column must decide whether an unconfirmed
  risk counts. Recommendation: it counts, and the footer discloses how many are
  unconfirmed — same floor-with-disclosure discipline as everywhere else.

## C. Split "no quote at all" from "quote failed a check"

Both land in the same `unconfirmed` bucket today, so a figure that is perfectly
quoted but whose label did not match reads identically to a programme-wide total
caught by the `$/MW` ceiling. The capex exclusion cannot tell them apart either,
which means it over-excludes.

- Record the *reason* alongside the flag. `webui.dataset._unconfirmed_because`
  already reconstructs one reason (the scale note) by string-matching a note
  marker — that is the seam to formalise.
- Then `capex.unconfirmed_investment_ids` can exclude only the scale-demoted
  figures and keep the merely-unquoted ones, disclosed separately.
- This is also what makes `logic check --audit`'s findings actionable: the audit
  says *why* a quote is wrong (unsupported / misattributed / hedged), and there
  is currently nowhere to store that verdict.

## D. Only then, relax the matching — on axes that cannot admit fabrication

Do this last, and tune it against the logs from A2 rather than guesses. Three
levers, in increasing order of how much they change:

1. **Verify sentence by sentence.** Split the offered quote into sentences and
   verify each independently, keeping the real ones. A two-sentence quote where
   the model edited one sentence currently fails wholesale on
   `MIN_RUN_FRACTION`, even when the other sentence is verbatim and carries the
   figure.
2. **A lower character floor for quantity fields.** `_stated_in` is a second,
   independent gate: if the recovered run contains the exact normalised figure
   *and* the field is numeric, a 25-character run is stronger evidence than 40
   characters of prose. `MIN_RUN_CHARS = 40` exists to stop coincidental matches,
   and a matching number is not a coincidence.
3. **Ordered token containment** (≈90% of the quote's tokens appearing in one
   article window, in order) in place of exact substring — still storing *the
   article's* words, never the model's.

Every one of these keeps the two invariants that make the gate worth having: the
stored quote is text somebody published, and `_stated_in` runs against that
stored text rather than against the model's edit.

**Negative control is mandatory.** The existing thresholds were tuned by testing
every quote against an *unrelated* article and confirming none crossed
([crawl.py, `MIN_RUN_CHARS` docstring](../tracker/ingest/crawl.py)). Any change
here repeats that experiment, or it is not a change we can defend.

> **Not implemented, and the measurement says why, 2026-08-06.** `scripts/
> measure_evidence_gate.py` now runs both experiments this section depends on.
> Over 1,250 stored quotes tested against their own articles: **98.7% exact
> substring, 0.5% needed recovery, 0.8% no longer match because the article was
> edited after ingest.** That is finding #2 above reproduced at 46x the original
> sample. The matching is not what refuses values, so all three levers here would
> be tuned against nothing — which is what the ordering was written to prevent.
> A2 is now in place, so the next real `sync` produces the evidence this needs.
>
> The negative control holds at the current thresholds: **0 crossings in 3,064
> (quote, unrelated-publisher article) pairs.**
>
> **Building it corrected the experiment's own design.** "Unrelated" has to mean a
> different *publisher*. Pairing naively across the whole cache reported three
> crossings, and every one was a single company's boilerplate recurring in its own
> documents — two filings under one SEC CIK, two pages on one domain. Counting
> those as failures would have sent somebody to raise a threshold that is not
> broken. They are reported on their own line instead, because they name something
> real that no substring test can ever catch: boilerplate like "H5 Data Centers, a
> national colocation and wholesale data center provider" is verbatim on every
> page the company publishes, so quoting it proves publication and proves nothing
> about *which site* it describes. That is the misattribution class
> `logic check --audit` exists for, and it is an argument for section C's verdict
> storage rather than for anything in this section.

---

## Order, and why

1. **A2** (log the rejects) — two lines, and everything after it is better
   informed for having it.
2. **A1** (thin-content refusal) — largest share of the symptom, saves money,
   prevents phantom rows. Measure length-by-source_type before picking a floor.
3. **B** (risks stop vanishing) — the only place with real, silent deletion.
4. **C** (split the reasons) — unblocks precise capex exclusion and gives the
   audit's verdicts somewhere to live.
5. **D** (matching) — last, against A2's data, with the negative control.

## Verification

- `pytest` throughout; the gate has dense coverage in `tests/test_crawl.py`
  including the recovery thresholds — new behaviour needs cases beside them.
- Re-run the instrumented replay used for this diagnosis (6 trade-press + 4
  EDGAR cached articles) after D and confirm exact-match share does not fall and
  no quote from an unrelated article is accepted.
- After A1, re-run `tracker sync --dry-run` over the queue and confirm the
  thin-content pages are refused before the LLM call, by count.
- After B, `tracker risks` should show more rows, with the new ones marked
  unconfirmed, and `tracker capex`'s footer should disclose how many.
