# Where the data comes from

Everything in dc-tracker comes from public sources. Nothing is bought, leaked or private, and no figure is the tracker's own estimate presented as reported: where the tracker works something out, it is labelled [derived or inferred](provenance).

## The sources

| Source | What it gives | Weight |
| --- | --- | --- |
| **Company filings and announcements** — operators' newsrooms, press releases, SEC filings | First-hand statements of plans, capacity, investment and milestones | 3 |
| **Government documents** — permits, zoning and planning records, economic-development announcements | Approvals, filings, official figures | 3 |
| **Trade press** — data-center and industry publications | Detailed reporting, often first | 2 |
| **General media** — national and local news | Local reaction, opposition, permits, utility news | 1 |
| **Grid interconnection queues** | Large loads asking to connect; matched to campuses by keyword only, so weak evidence | 1 |
| **Directories, wikis and digests** — facility directories, project trackers, encyclopedias | Fill gaps only (see below) | — |
| **US Census reference data** | County and town-centre coordinates for a place | reference only |

The weight is how much a source counts in [confidence](confidence) and when sources disagree. Weights are set by hand, by kind of source, not by publisher.

### Directories and wikis only fill gaps

Facility directories, project trackers and wikis are useful but second-hand: they are often undated, and they often mix one building's figures with the whole campus's. So they are kept as citations, but they **only fill a field no first-hand source states**, never replace one, and never count as corroboration.

### Publishers that block automated reading

Some publishers refuse every automated visitor. The tracker does not try to get past that. For two of them in particular — DatacenterDynamics and Data Center Frontier — it learns their headlines from a news-search service and reads the same story from another outlet or the operator's own release. Their pages themselves are never fetched.

## How articles are found

- **News feeds and sitemaps** of selected publishers and operators' newsrooms, checked every day.
- **Web searches** for places and events — a county name with "data center", a permit vote — to find coverage feeds miss.
- **SEC filings** of cloud companies, data-center landlords and utilities.
- **Targeted searches** for one campus's missing facts, when the tracker is filling gaps.

New articles go into a queue, with recent news read first.

## How a figure is read and checked

1. **Read.** A language model reads the article and extracts every campus it describes: who, where, how big, how much, when, what stage, what stands in the way, and the sentence behind each value.
2. **Quote-checked.** Each value is kept as *quoted* only if its sentence really is in the article and really states it. See [Where a value came from](provenance).
3. **Scale-checked.** A figure too large to be one site's — more than $50 million per megawatt, say — is held back as *not this site's figure*. A building's figure is kept out of the campus total. A forecast is kept apart from a milestone reached.
4. **Matched.** The campus is matched to an existing row or, if new, checked against similar rows before one is created. See [Duplicates](duplicates).
5. **Combined.** The campus's fields are worked out again from all its sources together. See [Field dictionary](fields).
6. **Cross-checked.** Rules look for contradictions — more capacity running than planned, a date before the campus was announced, an implausible cost per megawatt. Where two quoted figures disagree, a model may choose between the published figures or decline; it can never type in a new value, and the losing figure is kept, marked as set aside.

## How the crawler behaves

The tracker identifies itself to every site it visits, asks each site's `robots.txt` first and obeys it, waits between requests to the same site, and backs off on errors. It does not disguise itself as a browser to get past a block. It keeps a private copy of an article's text so it can be re-read and checked; it does not republish articles. Publishers can ask for it to slow down or stop.

## What is not used

- No figure is guessed. An estimate the tracker cannot source is not stored as a value.
- No private or paid data.
- No content is used to train any model.
