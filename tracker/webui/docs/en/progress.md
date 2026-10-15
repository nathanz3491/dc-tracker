# Phases and progress

The tracker describes how far along a campus is in two ways: a single **phase**, and five independent **tracks**. The phase is the short answer; the tracks are the honest one.

## Phase

| Phase | Meaning |
| --- | --- |
| **announced** | Made public; nothing formal filed yet. |
| **permitting** | Seeking zoning, permits or approvals. |
| **construction** | Being built. |
| **operational** | At least part of it is running. |
| **paused** | Work has stopped, by the developer's own account or a source's. |
| **cancelled** | Abandoned. |

The first four are a ladder. *Paused* and *cancelled* stand outside it: when a credible source says a campus has stopped, that overrides how far it had got.

When sources disagree on the phase, the furthest-along phase that a quoted source supports wins — unless a source says the campus stopped. Named buildings can also move a campus forward: if a source says a named building is running, the campus is at least operational. A campus is only cancelled through its buildings when *every* one of them is stopped.

If no source states a phase at all, the row shows **announced** as a [default](provenance), not as a reported fact.

[Capex](capex) leaves paused and cancelled campuses out of its totals.

## Five tracks

A single phase hides most of what matters. A campus can own its land and have its permits, and still wait years for a grid connection. So progress is also tracked on five ladders that move independently:

| Track | Milestones, in order |
| --- | --- |
| **site control** | announced → land acquired |
| **permits** | permit filed → permit approved |
| **power** | interconnection agreement → energized |
| **construction** | site work → groundbreaking → equipment installed |
| **customer & finance** | first customer |

Each track's status is the latest milestone reached on it. The next milestone not yet reached is the **signal to watch for**: the news that would show the campus moving.

### Implied milestones

Some milestones imply earlier ones: you cannot break ground without the land. When a later milestone is reported, the earlier ones it implies are shown as **implied** — faint in [Projects](projects), hollow on a project's timeline — because a deduction is not a source.

**Power is never implied by construction.** Building ahead of a grid connection is routine, and a finished building waiting on a substation is one of the most useful things the tracker can show you. Power is reached only when a source says so.

### Obstacles and tracks

Each open obstacle holds up one track:

| Obstacle | Track it holds up |
| --- | --- |
| grid capacity, transmission | power |
| permitting, community opposition, environmental, water | permits |
| equipment supply, chip supply | construction |
| financing, no committed customer | customer & finance |

A track with an open obstacle shows as **blocked**, in red.

## Milestones that have not happened

A milestone counts as reached only if its date has passed and the source reported it as done. Wording such as "expected to begin operations in 2027" is a **forecast**: it is kept, labelled *expected, not reached*, and never counted. On [Updates](updates) such news is flagged **expected, not reached**.

## Obstacles

| Field | Values |
| --- | --- |
| **Kind** | grid capacity, transmission, permitting, environmental, equipment supply, chip supply, financing, no committed customer (offtake), community opposition, water |
| **Severity** | *watch* — raised, no stated effect; *material* — a source says the schedule or scope moved; *blocking* — work has stopped |
| **Status** | open, resolved, superseded |

Most obstacles are *watch*: sources rarely say outright that something moved the schedule. The **biggest obstacle** on a row is the most serious open one, preferring those backed by a quote; when two are equally serious the choice between them is arbitrary, and the project page says so.
