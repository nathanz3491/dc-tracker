# The API

The API lets a program read what you read in the console: campuses, their figures and the sources behind them, companies, capex by buyer, and the news on your watchlist. It is read-only — nothing a program does through it changes any data — and it answers in JSON.

| | |
| --- | --- |
| **Address** | `{{api}}` |
| **Authentication** | an API key, sent as `Authorization: Bearer <key>` |
| **Limit** | 60 requests a minute per key |
| **Format** | JSON, UTF-8 |

The [API reference](api-reference) lists every endpoint.

## Get a key

1. Sign in to the console and open [Your account](account) (your name, top right).
2. Under **API keys**, give the key a name you will recognise — "analysis notebook", "reporting server" — and press **Create key**.
3. **Copy the key now.** It starts with `dct_` and is shown only this once: the tracker keeps only a fingerprint of it, so it cannot show it to you again. If you lose it, revoke it and make another.

An account can hold five active keys. The list on the Account page shows each key's name, its first characters, when it was made and when it was last used, and a **Revoke** button.

> [!WARNING]
> A key reads as you. Keep it out of code you share, out of web pages, and out of URLs; store it the way you would a password. If one leaks, revoke it — it stops working at once.

A key works only while your account does. If an administrator disables the account, every key on it stops working; if the account is deleted, its keys go with it.

## Your first request

```bash
curl -H "Authorization: Bearer dct_your_key_here" \
  "{{api}}/projects?state=TX&limit=5"
```

In Python:

```python
import requests

API = "{{api}}"
HEADERS = {"Authorization": "Bearer dct_your_key_here"}

page = requests.get(f"{API}/projects", headers=HEADERS, params={"state": "TX", "limit": 5}).json()
for campus in page["data"]:
    print(campus["id"], campus["name"], campus["mw_planned"])
```

`X-API-Key: <key>` works as well as the `Authorization` header. A key in the address (`?key=`) is never accepted: addresses end up in logs and browser history.

## Reading the answers

- **Every value says where it came from.** A campus's detail gives each field as `value`, `label` and the `quote` and `source_url` behind it. The labels are the console's: `quoted`, `derived`, `unconfirmed` (待确认), `inferred`, `default`, `missing`, `not_applicable`. See [Where a value came from](provenance).
- **Numbers are floors, dates are as precise as their source.** A figure's `bound` says if it was "about" or "at least", its `basis` which kind of megawatt it is, and a date's `date_precision` whether the source gave a day, a month, a quarter, a half or a year. See [Numbers and dates](numbers-and-dates).
- **Empty is often correct.** `null` means no source states the value; see [Why a field is empty](empty-fields).
- **Rows can be merged.** A campus found to duplicate another is merged into it and its id stops working: the API answers 404 and says so. Look it up again by name.

## Paging

`/v1/projects` returns at most 200 campuses at a time (30 by default). Each answer carries `total`, `offset`, `limit` and `next_offset`; ask again with `offset=<next_offset>` until `next_offset` is `null`.

## Errors

Every error is JSON with a stable code and a sentence:

```json
{"error": {"code": "key_invalid", "message": "that API key is not valid or was revoked"}}
```

| Status | Code | Meaning |
| --- | --- | --- |
| 400 | `bad_parameter`, `unknown_parameter`, `bad_id`, `bad_slug` | The request asked for something the API does not understand. The message says what. |
| 401 | `key_required`, `key_invalid` | No key, or a key that is wrong, revoked, or on an account that is disabled. |
| 404 | `not_found` | No such endpoint, campus or company. |
| 405 | `read_only` | Something other than GET. The API never changes data. |
| 429 | `rate_limited` | More than 60 requests in a minute. Wait the number of seconds in `Retry-After`. |
| 500 | — | A fault on our side. Try again later. |

Each answer carries `X-RateLimit-Limit` and `X-RateLimit-Remaining`, so a program can slow down before it is refused.

## How current it is

The API reads the same data as the console, updated every evening (China time). Nothing is cached between requests, so an answer reflects the data at the moment it is asked. See [How the data is kept current](how-it-is-kept-current).

## Stability

Within `v1`, fields may be **added** but are never renamed or removed. A change that would break a program will come as `v2`, alongside `v1`, with notice in the [release notes](release-notes).
