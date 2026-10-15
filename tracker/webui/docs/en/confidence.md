# Confidence

Each campus has a **confidence** score from 0 to 3. It answers one question: how much should you trust this row without checking it yourself?

| Score | Meaning |
| --- | --- |
| **0** | No source at all. |
| **1** | Cited, but weakly: a single minor source, or a row with too few fields filled to judge. |
| **2** | Solidly backed by one source. |
| **3** | Backed by independent sources that agree, or checked and signed off by a person. |

**One source alone never reaches 3.** However good it is, a single article is one point of view; corroboration is what the top score means.

The score is worked out again from the sources every time a campus changes, so it rises and falls as evidence arrives.

## How it is worked out

1. **Set aside** what cannot vouch for anything: lookups such as Census geography, model inferences, and placeholders. These never raise the score.
2. **Start** from the weight of the strongest source — company filings and government documents weigh 3, trade press 2, general news 1 — but no higher than 2.
3. **Add 1** when two or more **independent publishers** back quoted values. Independence is counted by website: five articles on one site count once. Directories and wikis do not count, and nor do sources with only 待确认 values.
4. **Add 1** when independent sources **agree** on a key figure: planned or running capacity, investment, phase or tenant.
5. **Subtract 1** when sources **conflict** on a key figure. Two numbers more than 20% apart count as a conflict.
6. **Cap at 1** a campus with fewer than five of its twelve tracked fields filled: there is too little to judge.
7. A **person's sign-off** sets the score to 3.

A campus with any real source scores at least 1, and no campus scores more than 3.

## What it does not tell you

- **It is about the row, not each value.** A 3 means the campus as a whole is well backed; a single field on it can still be 待确认. Read the [labels](provenance) on the values you rely on.
- **Syndicated stories inflate it.** The same press release reprinted by several outlets looks like several independent publishers.
- **A sign-off does not expire.** A campus a person checked a year ago keeps its 3 even if the news has moved on.

See [Known limitations](limitations) for these and others.

## Where you see it

- On a [project's page](project-page), next to the phase: *confidence N*.
- In [Projects](projects), as a filter (*1 or better*, *2 or better*, *3 only*), as the default sort order, and as a column with **All fields** on.
- On the [Map](map), as one of the colourings.
