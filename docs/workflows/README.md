# Command workflows

Four commands in this tool are long, staged and expensive, and each one is really a
pipeline wearing a single name. Reading the code tells you what a stage does; these
files tell you **what order the stages run in, what each one is allowed to write,
and where a run stops.**

| | The command | What the poster answers |
| --- | --- | --- |
| [enrich](enrich.md) | `tracker enrich` | Which of six retrieval methods runs in which round, why a round stops, and when the expensive model rungs are reached |
| [sync](sync.md) | `tracker sync` — and the unrelated `scripts/sync_db.py` | The seven phases, which two are off by default, which cap bounds which spend, and the gate standing between the queue and a new row |
| [duplicates](duplicates.md) | `tracker duplicates`, `park`, `unpark`, `parked`, `resolve` | How a pair is *raised*, how its evidence is ranked, and every rail that refuses a merge |
| [logic](logic.md) | `tracker logic check`, `conflicts`, `resolve` | Which layer costs money, which one writes, and what "settled" means for each |

Each file leads with a full-page diagram and then carries the detail a diagram
cannot hold: the flags, the measured numbers, and a **source map** naming the
functions the diagram was drawn from.

## Why these four and not the rest

`gaps`, `sources`, `overview`, `export` and `capex` are reports: one query, one
table, nothing to sequence. `merge` and `backfill` are single operations. These
four are the ones where an operator has been surprised — a phase that was skipped,
a budget spent by the first project in a batch, a merge refused after the model
said "same site", a value that came back after being cleared. A picture is the
shortest answer to all four.

## Reading the diagrams

Colour is not decoration. It carries one distinction, the one this whole tool is
organised around — what a step costs:

| | Means |
| --- | --- |
| **teal** | free and deterministic. No model, and usually no network |
| **cool grey-blue** | one fetch or one query. Stored data and inputs |
| **orange** | costs a model call. These are the steps a budget bounds |
| **red** | a refusal, a rail, or a stop. Where a run declines to act |
| **dashed orange frame** | a loop or a group of steps that repeat together |
| **grey panel** | an annotation, not a step in the flow |

An arrow is a handover. A box with no arrow into it is an aside.

## Re-rendering them

The diagrams are generated, and the generator is the file you edit:

```bash
python scripts/render_workflow_diagrams.py            # all four
python scripts/render_workflow_diagrams.py logic      # just one
```

Standard library only — no Node, no Graphviz, nothing to install. Each diagram is
a declarative block at the foot of `scripts/render_workflow_diagrams.py`: boxes with
their text, arrows between named box edges. The `.svg` files are committed beside
the Markdown because GitHub renders `![](x.svg)` and does not run a build step.

The palette is measured out of `work/Extraction-Pipeline-Diagram.pdf`, which is the
house style for a diagram in this project.

**Why not Mermaid.** GitHub does render ```mermaid fences, so that was the cheaper
route. It lays itself out, though, and cannot express what these pages need —
sectioned bands, a dashed group around a sub-loop, annotation panels beside the
flow, and a palette where colour means something. A Mermaid version of `duplicates`
would be a monochrome tangle of 30 nodes; the value here is in what the eye can
skip.

## Keeping them true

A diagram that describes last month's ordering is worse than no diagram: it is read
as authority. `CLAUDE.md` §7 therefore makes updating the matching file part of the
same change as the code.

The source map at the foot of each page is the checklist — if you touched a function
it names, that page and its diagram are in scope. Line numbers are deliberately
absent from those maps: they are wrong within a week, and function names survive.
