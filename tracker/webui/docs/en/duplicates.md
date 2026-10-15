# Duplicates

The same campus often appears in the news under different names: one article names the developer, another the tenant; one names the town, another the county; one calls it "Project Blue", another by its address. When the tracker cannot tell that two articles describe one site, it ends up with two rows for it.

A duplicate is not harmless. It counts a campus's megawatts twice, against whoever its buyer is.

## How a suspected duplicate is found

Rows are compared within the same town or county, and across town and county for the same company and state. A pair is raised when the rows share something that suggests one site, strongest first:

- the **same name** and company;
- the **same named building or phase**;
- the **same company involved** in a role on both;
- one filed by **town**, the other by **county**;
- **overlapping names**.

Pairs that overlap are gathered into one group per campus.

## Counted once until settled

While a group is unsettled, [Capex](capex) counts **one row** from it — the one with a named tenant, then the largest, then the oldest — and sets the others aside, showing their megawatts as *skipped*. Nothing is added twice, and nothing is merged automatically by this. The **N campuses are stored more than once** banner on Capex lists the groups.

## How a pair is settled

A pair is settled one of two ways:

- **Ruled out** — judged to be two different sites. The pair stops being raised, and Capex stops setting a row aside for it. The decision is recorded with who made it.
- **Merged** — judged to be one site. Every source, milestone and obstacle moves onto one row, the other row is deleted, and every field is worked out again from the combined sources. Which row survives is decided by a rule (the most sources, then the most fields filled, then the lowest number), not by preference.

Merges are made by whoever maintains the tracker, and by the nightly update under strict limits: a model must judge the pair to be one site with high confidence, and the merge is refused when the only evidence is a shared word in the name, a town-versus-county match, or two rows whose locations are far apart. Pairs it cannot settle are left for a person.

> [!WARNING]
> A merge cannot be undone, and the merged-away row's number stops working. If a link to `/projects/<number>` says *No project #N*, the campus was most likely merged into another row.

Nobody can merge or rule out pairs from the console; it only shows them.

## One campus, several buildings

Separate buildings or phases of one campus are not duplicates. They are **tranches** of a single row, each with its own capacity, state and tenant — see [A project's page](project-page). The tracker also watches for one building being described twice under different names ("Phase I", "first phase", "Building A"), and reports it rather than adding its megawatts twice.
