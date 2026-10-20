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
| `GET /v1/companies/{slug}` | One company's campuses and recent news. |
| `GET /v1/capex` | Capacity and investment by buyer. |
| `GET /v1/updates` | Recent news on your watchlist. |

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
| `sort` | A field to sort by: `confidence` (the default), `mw_planned`, `investment_usd`, `expected_online`, `updated_at`, `id`, or any other tracked field. |
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
      "links": {"self": "/v1/projects/42"}
    }
  ]
}
```

The values above are invented, for illustration.

## GET /v1/projects/{id}

One campus: everything a summary has, plus:

| Key | Meaning |
| --- | --- |
| `fields` | Each tracked field as `{value, label, quote, quote_is_exact, source_url}`, plus `scope`, `bound`, `modality`, `as_of`, `basis` or `date_precision` where they say something. `label` is `quoted`, `derived`, `unconfirmed`, `inferred`, `default`, `missing` or `not_applicable`. |
| `parties` | Companies involved: `name`, `role` (`developer`, `owner`, `operator`, `customer`, `utility`, `contractor`), `quote`, and `confirmed`. |
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

## GET /v1/projects/{id}/sources

Every citation: `url`, `type` (`company_filing`, `government_doc`, `trade_press`, `general_media`, `iso_queue` or `manual`), `published_at`, `fetched_at`, `excerpt`, and `quotes` — the sentence recorded for each field the source supports.

## GET /v1/companies/{slug}

A company's page as data: `name`, `website`, `totals` (campuses, as operator and as tenant, planned and running MW, open obstacles), `states`, `phases`, its `projects` (each with its role, phase, capacity and obstacles), and `updates` reported in the last two months. The slug is the company's name in lowercase with hyphens, as in the console's address for it: `compass-datacenters`.

`name` is the company's one name, the same on every page; each project also carries `company_name` and `customer_name` beside the source's own `company` and `customer`. The planned and running MW in `totals` are counted as on [Capex](capex) — a suspected duplicate once, paused and cancelled campuses not at all, a tenant credited with its own tranches — and `totals.counting` splits them into `capex_row` (the company's row in `/v1/capex`) and `no_named_buyer` (campuses it builds that name no buyer), with how many campuses were set aside as `duplicates`, `stopped` (paused or cancelled) or counted for another buyer (`elsewhere`). Each project's `counted` says what it adds: `status` (`counted`, `part`, `elsewhere`, `duplicate` or `stopped`), `mw_planned`, `mw_built`, and `for` (the other buyers) or `of` (the campus a duplicate is counted as).

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

## GET /v1/me

The key (`name`, `prefix`, `created_at`, `last_used_at`) and its account (`email`, `name`). Never the key itself.
