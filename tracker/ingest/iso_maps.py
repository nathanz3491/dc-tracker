"""Per-ISO column mappings for interconnection queue exports.

**Read this before using anything in here.** The public PJM / MISO / ERCOT /
CAISO queues are *generator* interconnection queues. They describe proposed
power plants — solar, gas, wind, storage — and every one of them has a `Fuel` or
`fuelType` column. **None of them has a data-center or load-type column.** The
PRD's "filter to Data Center applicants" is therefore not a filter but a keyword
heuristic over project and entity names.

What that means for every row this module produces:

* ``mw`` from a queue row is **generator nameplate capacity**, not data-center
  load. It is not written to ``project.mw_planned`` unless the operator passes
  ``--trust-gen-mw``; by default it goes to ``notes`` as a disclosure.
* ``company`` is at best a commercial name (often a single-purpose entity like
  "Nova Solar LLC") or the transmission owner (a utility). It is frequently
  **not** the data center operator.
* Location granularity is **County**, never city. That feeds
  ``project.county``, and `dedup.py` keeps county-level rows from merging into
  city-level ones.
* Confidence from this path caps at 1. See `confidence.SOURCE_WEIGHTS`.

Real large-load queues do exist (ERCOT's is enormous, PJM built a Large Load
Registry) but are published aggregated and anonymized, not per project. When a
genuine per-project large-load export becomes available, set
:attr:`IsoMap.load_type_col` and use ``--filter "column:<name>=<regex>"``, which
raises the confidence cap to 2 because the source then actually says "data
center" rather than us guessing.

Column names for PJM and MISO are taken from their real exports. **ERCOT and
CAISO names are unverified assumptions** — `--dry-run` prints unmatched required
columns and aborts, so a wrong guess fails loudly rather than ingesting nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Version stamp recorded in `source.extractor`, so a row can be traced back to
#: the mapping that produced it. Bump when a mapping changes meaningfully.
MAPS_VERSION = "iso_maps-v1"


@dataclass(frozen=True)
class ColSpec:
    """Where one logical field lives in a given ISO's export.

    ``src`` is a candidate list rather than a single name because these exports
    rename columns between quarters and because capacity is split across
    several columns that are populated inconsistently (PJM's `MW Capacity`,
    `MW Energy`, `MFO`). First present and non-empty wins.
    """

    src: tuple[str, ...]
    required: bool = False


@dataclass(frozen=True)
class IsoMap:
    iso: str
    #: Stable public page for the queue. Becomes the `source.url` base; the row's
    #: queue id is appended as a fragment so each row cites a distinct,
    #: human-checkable URL.
    provenance_url: str
    columns: dict[str, ColSpec]
    #: Columns searched for data-center keywords.
    dc_search_cols: tuple[str, ...]
    #: Raw queue status -> our `phase` vocabulary. Lowercased keys.
    status_map: dict[str, str] = field(default_factory=dict)
    source_type: str = "iso_queue"
    #: Worksheet name for xlsx exports.
    sheet: str | None = None
    #: Set only when an export genuinely identifies load type. None for every
    #: generator queue, which is all four of the public ones.
    load_type_col: str | None = None


PJM = IsoMap(
    iso="pjm",
    provenance_url="https://www.pjm.com/planning/services-requests/interconnection-queues.aspx",
    columns={
        "ext_id": ColSpec(("Project ID", "Queue Number"), required=True),
        # `Name` is the project name; `Commercial Name` is the developer's or
        # single-purpose entity's name. Prefer the former for display and the
        # latter for the company, then let _infer_company() try to do better.
        "name": ColSpec(("Name", "Commercial Name"), required=True),
        "company_raw": ColSpec(("Commercial Name", "Name")),
        "utility": ColSpec(("Transmission Owner",)),
        "county": ColSpec(("County",)),
        "state": ColSpec(("State",), required=True),
        "gen_mw": ColSpec(("MW Capacity", "MW Energy", "MFO")),
        "gen_mw_inservice": ColSpec(("MW In Service",)),
        "fuel": ColSpec(("Fuel",)),
        "raw_status": ColSpec(("Status",)),
        "first_announced": ColSpec(("Submitted Date",)),
        "expected_online": ColSpec(("Projected In Service Date",)),
        "actual_online": ColSpec(("Actual In Service Date",)),
        "withdrawn": ColSpec(("Withdrawal Date",)),
    },
    dc_search_cols=("Name", "Commercial Name"),
    status_map={
        "active": "permitting",
        "under study": "permitting",
        "engineering and procurement": "construction",
        "construction": "construction",
        "partially in service - under construction": "construction",
        "in service": "operational",
        "withdrawn": "cancelled",
        "retracted": "cancelled",
        "deactivated": "cancelled",
        "suspended": "paused",
    },
)

MISO = IsoMap(
    iso="miso",
    provenance_url="https://www.misoenergy.org/planning/resource-utilization/GI_Queue/",
    columns={
        "ext_id": ColSpec(("projectNumber", "Project Number"), required=True),
        "name": ColSpec(("projectName", "Project Name")),
        "county": ColSpec(("county", "County")),
        "state": ColSpec(("state", "State"), required=True),
        "gen_mw": ColSpec(("summerNetMW", "Summer Net MW", "winterNetMW")),
        "fuel": ColSpec(("fuelType", "Fuel Type")),
        "raw_status": ColSpec(("applicationStatus", "Application Status")),
        "first_announced": ColSpec(("queueDate", "Queue Date")),
        "expected_online": ColSpec(("negInService", "In Service Date")),
        "withdrawn": ColSpec(("withdrawnDate",)),
    },
    dc_search_cols=("projectName", "poiName", "Project Name"),
    status_map={
        "active": "permitting",
        "in progress": "permitting",
        "done": "operational",
        "withdrawn": "cancelled",
    },
)

ERCOT = IsoMap(
    iso="ercot",
    provenance_url="https://www.ercot.com/gridinfo/resource",
    sheet="Project Details - Large Gen",
    columns={
        "ext_id": ColSpec(("INR",), required=True),
        "name": ColSpec(("Project Name",), required=True),
        "company_raw": ColSpec(("Interconnecting Entity",)),
        "county": ColSpec(("County",)),
        "state": ColSpec(("State",)),
        "gen_mw": ColSpec(("Capacity (MW)",)),
        "fuel": ColSpec(("Fuel", "Technology")),
        "expected_online": ColSpec(("Projected COD",)),
        "raw_status": ColSpec(("IA Signed",)),
    },
    dc_search_cols=("Project Name", "Interconnecting Entity"),
    #: ERCOT does not publish a status word; presence of a signed interconnection
    #: agreement is the closest available signal.
    status_map={},
)

CAISO = IsoMap(
    iso="caiso",
    provenance_url=(
        "https://www.caiso.com/generation-transmission/generation/"
        "generator-interconnection/queue-management"
    ),
    sheet="Grid GenerationQueue",
    columns={
        "ext_id": ColSpec(("Queue Position", "Project ID"), required=True),
        "name": ColSpec(("Project Name",), required=True),
        "company_raw": ColSpec(("Interconnection Customer",)),
        "county": ColSpec(("County",)),
        "state": ColSpec(("State",)),
        "gen_mw": ColSpec(("Net MWs to Grid", "MW-1")),
        "fuel": ColSpec(("Fuel-1", "Type-1")),
        "expected_online": ColSpec(("Proposed On-line Date (as filed with IR)",)),
        "raw_status": ColSpec(("Application Status", "Interconnection Agreement Status")),
    },
    dc_search_cols=("Project Name", "Interconnection Customer"),
    status_map={},
)

ISO_MAPS: dict[str, IsoMap] = {m.iso: m for m in (PJM, MISO, ERCOT, CAISO)}

#: ISOs whose column names are confirmed against a real export. The loader warns
#: for the others so an operator knows a mismatch is expected rather than a bug.
VERIFIED_ISOS = frozenset({"pjm", "miso"})


def get_map(iso: str, overrides: dict[str, list[str]] | None = None) -> IsoMap:
    """Look up an ISO mapping, optionally overriding column names.

    ``overrides`` maps a logical field to a replacement candidate list, letting
    an operator absorb a column rename without waiting on a code change:
    ``--map-override '{"gen_mw": ["Max Summer MW"]}'``.
    """
    key = iso.strip().lower()
    if key not in ISO_MAPS:
        raise KeyError(f"unknown iso {iso!r}; expected one of {', '.join(sorted(ISO_MAPS))}")
    base = ISO_MAPS[key]
    if not overrides:
        return base

    columns = dict(base.columns)
    unknown = set(overrides) - set(columns)
    if unknown:
        raise KeyError(
            f"--map-override names unknown field(s) {sorted(unknown)}; "
            f"valid fields: {', '.join(sorted(columns))}"
        )
    for logical, names in overrides.items():
        columns[logical] = ColSpec(tuple(names), required=columns[logical].required)
    return IsoMap(
        iso=base.iso,
        provenance_url=base.provenance_url,
        columns=columns,
        dc_search_cols=base.dc_search_cols,
        status_map=base.status_map,
        source_type=base.source_type,
        sheet=base.sheet,
        load_type_col=base.load_type_col,
    )


__all__ = [
    "CAISO",
    "ERCOT",
    "ISO_MAPS",
    "MAPS_VERSION",
    "MISO",
    "PJM",
    "VERIFIED_ISOS",
    "ColSpec",
    "IsoMap",
    "get_map",
]
