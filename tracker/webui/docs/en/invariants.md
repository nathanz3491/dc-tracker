# How the figures relate

A company owns campuses; a campus is built in buildings and phases (its *tranches*); each has a planned capacity and a running one. These are the relationships the tracker holds its figures to — the 勾稽关系 — checked across the whole database every time `tracker invariants` runs.

Each relationship is one of two kinds:

- **Held** — the tracker enforces it when it writes a row. Every surface (the project page, the company pages, [Capex](capex), the map, the API) reads figures that keep it. A break would be a fault in the tracker, not in the data, and the test suite runs these before any change ships.
- **Reported** — the published sources do not yet close it, and the tracker says where. Most of these come from one gap: a campus described once as a whole and again building by building, with nothing saying which building is part of which. Since 22 October 2026 the tranches are summed as the tree they form: what the labels settle on their own — one building under several names, a parent its source names, a campus-wide tranche whose parts fit inside it — is settled on every read, and the rest is ruled on by a model reading the quotes each night, or by a person. Each ruling moves a campus off this list.

## Held

| Relationship | 关系 |
| --- | --- |
| A campus's running capacity is never more than its planned capacity. When the two disagree, the better-supported figure decides which describes something else. See [Numbers and dates](numbers-and-dates). | 园区运行容量不超过规划容量。 |
| A campus's capacity lines — cited per tranche, unconfirmed, not tied to a facility, not yet itemised, less what is counted twice — add up to its total. A utility's plant serving the campus is named beside the total and never inside it. | 园区各分项容量之和等于园区总容量；为园区供电的设施单列，不计入总数。 |
| What a campus delivers is never more than its total. | 园区正在交付的容量不超过园区总容量。 |
| A company's capacity is the sum of the campuses credited to it: each campus counted once, and in full, across its buyers. When a campus's tranches name buyers but describe the campus more than once, one buyer named across them gets the campus and no more; several are not told apart, and the campus is credited whole. | 公司容量等于其名下各园区之和：每个园区只计一次，且在各买方之间完整分配。 |

## Reported

| Relationship | 关系 | What a break means |
| --- | --- | --- |
| No tranche is larger than its campus. | 任何分期都不大于其所属园区。 | Almost always kilowatts read as megawatts. Already left out of every figure. |
| A campus's tranches describe it once: its buildings and phases add up to the campus, not past it. | 园区的楼栋与分期只描述园区一次，其容量之和不超过园区总容量。 | The campus and its buildings listed side by side, or one building under two names, that no rule or ruling has yet placed. Shown on a [project's page](project-page) as *counted twice over*. |
| Every tranche belongs to a named facility of its campus. | 每个分期都属于园区内一个具名设施。 | "Phase 2" without saying of which building. |
| The tranches marked live add up to no more than the campus's running capacity. | 已投运分期的容量之和不超过园区运行容量。 | The same double description, seen in the running figure. |
| A campus is not at an earlier phase than its own running tranches. | 园区所处阶段不早于其已投运的分期。 | A phase the tranches outran. |
| A campus is announced before it is expected online. | 园区宣布时间早于预计上线时间。 | Usually the earliest article found is later than the real announcement — an existing site first reported when it expanded. |

A break never changes a figure by itself. The figures on every page already follow the rules above — a tranche out of scale is excluded, a double description is counted once — and the report is the list of what is left to settle.
