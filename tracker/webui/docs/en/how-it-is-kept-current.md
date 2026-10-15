# How the data is kept current

The tracker updates itself every evening, China time, and the morning email at 8:00 reports what that brought. This page describes what the update does, so you know what "current" means.

## The nightly update

It runs in rounds and stops when a round no longer improves anything. Each round:

1. **Re-works what it already has, for free.** Fields are worked out again from their sources, dates are filled from articles that state them, and safe repairs are applied.
2. **Settles figures that cannot be true** — two units mixed up, a programme's total stored as one site's.
3. **Re-checks obstacles** that were reported without a quote, against the article.
4. **Resolves contradictions** between sources, ruling out the figure that is about something else.
5. **Settles suspected duplicates** — rules different sites apart, and merges a pair only when a model is highly confident and none of the safety limits applies. See [Duplicates](duplicates).
6. **Fills gaps** on incomplete campuses: searches for and reads more articles about them, and asks targeted questions for the fields still missing.
7. **Re-reads older articles** read before the reading instructions were last tightened, so old rows meet today's checks.
8. **Finds new articles**, news first, and reads them — which is where new campuses come from.

A copy of the database is kept before anything is merged or removed, and every value the night changed is listed with the sentence now behind it, for a person to review.

## What updates when

| What | When |
| --- | --- |
| New articles found and read | Every evening |
| Gaps filled on incomplete campuses | Every evening, a few dozen campuses at a time |
| Cited articles re-read for changes | Every few weeks per article |
| Publishers that block the tracker re-checked | Weekly |
| Morning email | 8:00 China Standard Time |
| [Updates](updates) page | As soon as the evening update stores something |

**last crawl** on the Updates page shows when new articles were last fetched; it turns amber if that was more than two days ago.

## What it will not do

- **It does not change a value without a source.** Every change rests on a sentence from an article or on a decision recorded with its reasons.
- **It does not delete evidence.** A figure ruled out stays visible as a claim that was set aside.
- **It does not guess.** A field with nothing published stays empty — see [Why a field is empty](empty-fields).

## Why a figure changed

If a figure on a campus changed overnight, open the [project's page](project-page):

- **N other claims** on the field shows every value sources have given and which is kept now;
- the notes record any decision made about it — a figure ruled out, a contradiction resolved, a duplicate merged in — with the reason;
- **Citations** shows the sources, newest fetched included.

The usual reasons are a new article, a figure being ruled out as belonging to something else, or two rows being merged.
