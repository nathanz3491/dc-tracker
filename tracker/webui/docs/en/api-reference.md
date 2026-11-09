# API reference

Every endpoint of the API, version 1. All are `GET`, answer JSON, and live under `{{api}}`. All but the first two need a key — see [The API](api).

## Overview

| Endpoint | Answers |
| --- | --- |
| `GET /v1` | What is here, and where it is documented. No key. |
| `GET /v1/health` | Whether the API is up, and the tracker's version. No key. |
| `GET /v1/me` | The key you are using, and the account it reads as. |
| `GET /v1/projects` | Campuses, filtered, sorted and paged. |
| `GET /v1/projects/{id}` | One campus in full, each field with its source. |
| `GET /v1/projects/{id}/sources` | Every citation behind one campus. |
| `GET /v1/projects/{id}/facts` | Every statement its sources make about one campus, one row each. |
| `GET /v1/companies/{slug}` | One company: what it is, its campuses and recent news. |
| `GET /v1/capex` | Capacity and investment by buyer. |
| `GET /v1/updates` | Recent news on your watchlist. |
| `GET /v1/conditions` | Rules that apply to a place — a state's permit pause, a town's ban — filtered and paged. |
| `GET /v1/conditions/{id}` | One rule: the articles that state it, and every campus it reaches. |

## GET /v1/projects

Campuses, as the [Projects](projects) table shows them, with the same filters.

| Parameter | Meaning |
| --- | --- |
| `q` | Free text. Every word must appear in the campus's name, company, tenant, city, county, state or biggest obstacle. `#42` means campus 42. |
| `state` | Two-letter state, such as `TX`. |
| `phase` | `announced`, `permitting`, `construction`, `operational`, `paused` or `cancelled`. |
| `min_confidence` | `1`, `2` or `3`. See [Confidence](confidence). |
| `obstacle` | Campuses with an open obstacle of this kind, such as `grid_capacity`. |
| `severity` | Campuses with an open obstacle at least this severe: `watch`, `material`, `blocking`. |
| `quoted_only` | `true` keeps campuses with no 待确认, inferred or default value. |
| `suspected_delay` | `true` keeps campuses that are a suspected delay (see below). |
| `sort` | A field to sort by: `confidence` (the default), `mw_planned`, `investment_usd`, `expected_online`, `updated_at`, `id`, or any other tracked field. `days_late` sorts suspected delays by how late they are, most overdue first with `desc`, every other campus after them. |
| `order` | `desc` (the default) or `asc`. Empty values always come last. |
| `limit` | Rows per page, 1–200. Default 30. |
| `offset` | Rows to skip. Default 0. |

An unknown parameter, or an unknown sort field, is a `400` rather than being ignored.

```json
{
  "total": 118,
  "offset": 0,
  "limit": 30,
  "next_offset": 30,
  "data": [
    {
      "id": 42,
      "name": "North Campus",
      "company": "Acme Data Centers",
      "customer": null,
      "city": "Abilene",
      "county": "Taylor County",
      "state": "TX",
      "phase": "construction",
      "mw_planned": 1200.0,
      "mw_built": null,
      "investment_usd": 15000000000,
      "first_announced": "2025-03-01",
      "expected_online": "2027-01-01",
      "blocker": "The utility has not yet approved the substation upgrade.",
      "h200_equivalent": 920000,
      "confidence": 2,
      "lat": 32.45,
      "lon": -99.73,
      "updated_at": "2026-10-14T12:31:07",
      "suspected_delay": null,
      "links": {"self": "/v1/projects/42"}
    }
  ]
}
```

The values above are invented, for illustration.

### suspected_delay

`null`, unless the campus is a suspected delay: its own online date has passed — the whole period the date names — and nothing reported since says it is running. The rule is the one the console uses, given in full in [Numbers and dates](numbers-and-dates). Then it is an object:

| Key | Meaning |
| --- | --- |
| `expected_online` | The stored date, such as `2025-07-01`. |
| `precision` | The precision its source recorded: `month`, `quarter`, `half`, `year`, or `null` for a day or for nothing recorded. |
| `read_as` | The precision the rule read the date at: `precision` where one was recorded; otherwise `day`, or `month` for a first of the month, or `year` for 1 January. |
| `stated` | The date as a reader would write it: `Q3 2025`, `H2 2026`, `2026`, `Jul 2026`, `15 Jul 2026`. |
| `due` | The last day of that period. The campus became late the day after. |
| `days_late` | Days from `due` to `as_of`. |
| `as_of` | The day this was worked out. It changes daily, so `days_late` is only meaningful beside it. |
| `reason` | One sentence saying why, in plain words. |

```json
"suspected_delay": {
  "expected_online": "2025-07-01",
  "precision": "quarter",
  "read_as": "quarter",
  "stated": "Q3 2025",
  "due": "2025-09-30",
  "days_late": 385,
  "as_of": "2026-10-20",
  "reason": "Expected online Q3 2025, a quarter that ended 385 days ago; the phase is still construction and nothing reported since says it is running."
}
```

## GET /v1/projects/{id}

One campus: everything a summary has, plus:

| Key | Meaning |
| --- | --- |
| `fields` | Each tracked field as `{value, label, quote, quote_is_exact, source_url}`, plus `scope`, `bound`, `modality`, `as_of`, `basis` or `date_precision` where they say something. `label` is `quoted`, `derived`, `unconfirmed`, `inferred`, `default`, `missing` or `not_applicable`. |
| `parties` | Companies involved: `name`, `role` (`developer`, `owner`, `operator`, `customer`, `utility`, `contractor`), `quote`, `confirmed`, and `ended_on` and `ended_reason` — null, or when and why the role ended (a former tenant, a sold site). A customer whose role has ended is not the campus's buyer: [Capex](capex) and the company pages credit it nothing. The `customer` field keeps what the sources say. |
| `tranches` | Buildings and phases: `label`, `status`, `mw`, `mw_counted` (whether that capacity is in the campus total), `customer`, `expected_online`, `energized_on`, `investment_usd`. |
| `obstacles` | `category`, `severity`, `status`, `summary`, `quote`, `first_seen`, `resolved_at`. |
| `milestones` | `date`, `type`, `description`, `quote`, and `confirmed`. |
| `source_count` | How many citations; `links.sources` lists them. |

```json
{
  "data": {
    "id": 42,
    "name": "North Campus",
    "fields": {
      "mw_planned": {
        "value": 1200.0,
        "label": "quoted",
        "quote": "The campus will draw 1.2 GW at full build-out.",
        "quote_is_exact": true,
        "source_url": "https://news.example.com/north-campus",
        "basis": "facility"
      }
    }
  }
}
```

A `quote_is_exact` of `false` means the text is the source's excerpt rather than a sentence recorded for this field. A campus merged into another answers `404`, saying it may have been merged.

`fields` is read from the same statements `/v1/projects/{id}/facts` returns. Where `quote_is_exact` is `true`, `quote` is the `quote` of the campus statement for that field from that `source_url`, so the two endpoints cannot disagree.

## GET /v1/projects/{id}/sources

Every citation: `url`, `type` (`company_filing`, `government_doc`, `trade_press`, `general_media`, `iso_queue` or `manual`), `published_at`, `fetched_at`, `excerpt`, and `quotes` — the sentence recorded for each field the source supports.

## GET /v1/projects/{id}/facts

Every statement the campus's sources make, one row each, whatever it is about — the master data behind every figure on the page, including the ones the page does not show. See [Where a value came from](provenance).

| Key | Meaning |
| --- | --- |
| `entity`, `entity_key` | What the statement is about: `campus`; a `tranche` (its key); a `party` (the company's key, with the role in `field`); a `milestone` (its type); an `obstacle` (its category). |
| `field`, `value`, `number`, `unit` | The field, the value as stated, the value as a number where it is one, and its unit (`MW`, `USD`, `date`…). |
| `tier`, `reason` | How far it can be trusted: `confirmed`, `directory`, `derived`, `inferred`, `unconfirmed` or `ruled_out`, and why when it is not confirmed. |
| `held` | Whether this is the value the campus shows. |
| `as_of`, `source_url`, `source_type`, `quote`, `axes` | When it was true, the citation, the sentence, and the scope, bound and measure it was read with. |

```json
{
  "data": [
    {
      "entity": "campus", "entity_key": "", "field": "mw_planned",
      "value": "900", "number": 900.0, "unit": "MW",
      "tier": "confirmed", "reason": null, "held": true,
      "as_of": "2026-09-01", "source_url": "https://news.example.com/north/",
      "source_type": "company_filing", "quote": "will draw 900 MW", "axes": null
    }
  ]
}
```

Several rows with the same `entity`, `entity_key` and `field` are sources disagreeing; the one with `held: true` is the value the merge kept, and [Fields and evidence](project-page) on the campus's page says why.

## GET /v1/companies/{slug}

A company's page as data: `name`, `website`, `totals` (campuses, as operator and as tenant, planned and running MW, open obstacles), `states`, `phases`, its `projects` (each with its role, phase, capacity, obstacles and `suspected_delay`), `updates` reported in the last two months, and `suspected_delays` — `count`, the `mw_planned` those campuses hold, and their `project_ids`, most overdue first. The slug is the one in the console's address for the company: `compass` for Compass Datacenters, `amazon` for Amazon and AWS alike.

What the tracker records about the company itself: `kind` (`hyperscaler`, `landlord`, `neocloud`, `ai_lab`, `utility`, `contractor`, or null), `parent` and `subsidiaries` (each `{name, slug}`), and `also_written` — the other spellings its campuses' sources use. A parent's figures do not include its subsidiaries': each company's capacity is its own campuses, as on [Capex](capex).

`name` is the company's one name, the same on every page; each project also carries `company_name` and `customer_name` beside the source's own `company` and `customer`. The planned and running MW in `totals` are counted as on [Capex](capex) — a suspected duplicate once, paused and cancelled campuses not at all, a tenant credited with its own tranches — and `totals.counting` splits them into `capex_row` (the company's row in `/v1/capex`) and `no_named_buyer` (campuses it builds that name no buyer), with how many campuses were set aside as `duplicates`, `stopped` (paused or cancelled) or counted for another buyer (`elsewhere`). Each project's `counted` says what it adds: `status` (`counted`, `part`, `elsewhere`, `duplicate` or `stopped`), `mw_planned`, `mw_built`, and `for` (the other buyers) or `of` (the campus a duplicate is counted as).

`builds` is the company as the builder of its campuses: `campuses`, `mw_planned` and `mw_built` over every campus it builds, whole, whoever buys it — paused, cancelled and suspected-duplicate rows left out. `totals` credits each campus to its buyer, as the Capex page does, so a builder that leases out what it builds has small `totals` and large `builds`.

`new_parties` is the page's *Newly mentioned suppliers*: the companies a source named on one of these campuses for the first time in the last `days` (30) days, since `since`, the company itself left out. `parties` holds one entry per company — suppliers (contractors and utilities) first, then newest first — with `key`, `name` (its one name), `roles`, `supplier`, `confirmed` (true when a sentence supports its role on at least one campus), `latest` (the most recent date it was first named on a campus) and `campuses`. Each campus has `id`, `name`, `company` (its operator), `place`, `state`, `role` and `roles` (the company's roles there), `first_cited`, `quote`, `unconfirmed` (null, `role_inferred`, or why the role is 待确认), `url` (the article) and `new_campus` (true when the campus itself was first reported in the window). A role that has ended is never listed.

## GET /v1/capex

[Capex](capex) as data. `buyers` holds one row per buyer, including `(no named customer)`:

| Key | Meaning |
| --- | --- |
| `customer`, `projects` | The buyer, under its one name, and how many campuses. |
| `self_built`, `undisclosed` | Campuses attributed from ownership, and tenants described without a name. |
| `mw_planned`, `mw_built` | Planned and running megawatts. |
| `investment_usd` | Confirmed investment. `investment_unquoted_usd`, `investment_excluded_usd` and `investment_out_of_scope_usd` are the money reported but treated differently, each disclosed apart. |
| `mw_by_year` | Megawatts due online, by year. |
| `mw_at_risk`, `projects_at_risk`, `slipped` | Capacity on campuses with an open obstacle, how many, and how many have slipped. |
| `duplicate_rows_skipped`, `mw_duplicate_skipped` | Suspected duplicate rows set aside rather than counted twice. |
| `project_ids` | The campuses behind the row. |

Every figure is a floor, and paused and cancelled campuses are left out.

## GET /v1/updates

What was reported recently about your watchlist — the same as your [Updates](updates) page.

| Parameter | Meaning |
| --- | --- |
| `days` | How far back, 1–60. Default 7. |

`data` holds the confirmed updates, most material first; `unconfirmed` those nobody could quote. Each has `kind`, `sign` (`good`, `bad`, `neutral`), `project_id`, `company` (the operator's one name), `project`, `headline`, `detail`, `happened`, `reported`, `expected`, `quote`, `source_url` and `publisher`. `last_crawl` says when articles were last fetched. With an empty watchlist, `data` is empty.

## GET /v1/conditions

Rules that apply to a place rather than to one campus — a state's pause on new permits, a town's ban on large data centers, a grid operator's rule for large loads, a tariff — newest report first. A rule is kept only when the article's own sentences state it, name where it applies and say where it stands; which campuses it reaches is then worked out by the tracker from each campus's place, stage and size, never by a model.

| Parameter | Meaning |
| --- | --- |
| `state` | Two-letter state, such as `TX`. |
| `status` | One status, or several separated by commas: `proposed`, `pledged` (promised by an official or a candidate), `passed` (adopted, not yet in effect), `in_force`, `blocked` (held up by a court), `lifted`, `rejected` (voted or turned down). Every status by default, so a program keeping its own copy hears when a rule ends. |
| `scope` | One scope, or several separated by commas: `state`, `county`, `place` (a city, town, village or township), `grid_region`, `national`, `input` (one input to every campus, such as large transformers). |
| `limit` | Rows per page, 1–200. Default 30. |
| `offset` | Rows to skip. Default 0. |

Paging works as on `/v1/projects`. An unknown parameter or value is a `400`.

| Key | Meaning |
| --- | --- |
| `id`, `title`, `summary` | The rule's number, a short name with its place, and one plain sentence. |
| `kind` | `moratorium`, `ban`, `permit_pause`, `zoning`, `law`, `executive_order`, `ballot_measure`, `tax`, `tariff`, `grid_rule`, `pledge` or `other`. |
| `need` | What it bears on, as an obstacle kind such as `permitting` or `grid_capacity`. |
| `effect` | `restricts` when it makes building harder (a ban, a pause, a new cost) or `eases` when it makes building easier (a tax exemption, by-right zoning, a law letting a large load generate its own power). A rule that eases is never an obstacle. |
| `scope`, `where` | Where it applies, and that place in words: `Texas`, `Piqua, OH`, `ERCOT`. `state`, `county`, `place`, `region` and `input` hold the parts; the ones the scope does not use are null. |
| `applies_to` | `new` (campuses announced or permitting with no approved permit on record), `unbuilt` (not yet running) or `all`. |
| `min_mw` | The size threshold in megawatts, or null when the rule has none. |
| `status` | Where it stands, as its newest article says. |
| `effective_on`, `ends_on` | When it takes effect and ends, where an article says. |
| `first_reported`, `last_reported` | The dates of its oldest and newest article. |
| `projects` | How many campuses it reaches now. A lifted or rejected rule reaches none. |
| `market_context` | `true` for a national rule or one on an input to every campus. These reach no campus by design — flagging every row says nothing — so `projects` is 0. |

```json
{
  "total": 14,
  "offset": 0,
  "limit": 30,
  "next_offset": null,
  "data": [
    {
      "id": 12,
      "title": "Texas pause on new data center permits",
      "kind": "pledge",
      "need": "permitting",
      "effect": "restricts",
      "scope": "state",
      "state": "TX",
      "county": null,
      "place": null,
      "region": null,
      "input": null,
      "applies_to": "new",
      "min_mw": null,
      "status": "pledged",
      "summary": "The governor-elect wants new data center permits paused until water and power rules are written.",
      "effective_on": null,
      "ends_on": null,
      "first_reported": "2026-11-05",
      "last_reported": "2026-11-08",
      "projects": 31,
      "where": "Texas",
      "market_context": false,
      "links": {"self": "/v1/conditions/12"}
    }
  ]
}
```

The values above are invented, for illustration.

A rule's reach is kept apart from a campus's own obstacles: it is not counted in `blocker`, `obstacles` or `confidence`, so every value on a campus still traces to an article about that campus.

## GET /v1/conditions/{id}

One rule: everything its summary has, plus:

| Key | Meaning |
| --- | --- |
| `articles` | Each article that stated it, newest first: `url`, `title`, `published_at`, `status` (where that article said it stood), `summary`, and `quotes` — the article's own sentences, by what each is cited for: `rule`, `place`, `status` and, where there is one, `threshold`. |
| `affected_projects` | Every campus it reaches: `project_id`, `name`, `company`, `city`, `county`, `state`, `phase`, `fit`, `why`, `first_seen` (when the campus first fell under it) and `links.self`. |

`fit` is `exact` when the campus's place and stage are on record, and `likely` when part of the match is inferred: a town's rule is matched to the campuses filed under that town, because no campus has a surveyed site and so the town limits cannot be checked; a grid region's rule from the state, where the state is mostly in that operator's region; and a size threshold against a campus whose size no source states. `why` says which, in words — for example `TX · announced · no permit on record`.

```json
{
  "data": {
    "id": 12,
    "title": "Texas pause on new data center permits",
    "status": "pledged",
    "articles": [
      {
        "url": "https://news.example.com/texas-pause",
        "title": "Governor-elect calls for data center permit pause",
        "published_at": "2026-11-08T00:00:00",
        "status": "pledged",
        "summary": "The governor-elect wants new data center permits paused.",
        "quotes": {
          "rule": "The governor-elect wants a statewide pause on new data center permits until the legislature writes water and power rules.",
          "status": "She campaigned on freezing new data center construction across Texas."
        }
      }
    ],
    "affected_projects": [
      {
        "project_id": 42, "name": "North Campus", "company": "Acme Data Centers",
        "city": "Abilene", "county": "Taylor County", "state": "TX", "phase": "announced",
        "fit": "exact", "why": "TX · announced · no permit on record",
        "first_seen": "2026-11-08T18:40:12", "links": {"self": "/v1/projects/42"}
      }
    ]
  }
}
```

The values above are invented, for illustration. A rule found to have been read wrongly is dropped, and its id then answers `404`.

## GET /v1/me

The key (`name`, `prefix`, `created_at`, `last_used_at`) and its account (`email`, `name`). Never the key itself.
