# FAQ

## Why does a campus's figure differ from what I read in the news?

Usually one of four reasons: the article's figure is for a different scope (one building, or a company's whole programme); it is a different kind of megawatt (facility power rather than IT load); another source is newer or more authoritative; or the tracker has not read that article. Open the campus's page and look at **N other claims** on the field: it shows every figure sources gave and which was kept. See [Where a value came from](provenance).

## A figure is wrong. How do I report it?

Tell whoever gave you access, with the campus's number and the field. Nobody can change data from the console; corrections are made by whoever maintains the tracker, and are recorded with their reason.

## What does 待确认 mean?

"To be confirmed": the value was read from an article, but no sentence could be found that states it. It may well be right; the tracker could not prove it. See [Where a value came from](provenance).

## Why is this field empty?

Often because empty is correct — no outside tenant, nothing running yet, nothing published. See [Why a field is empty](empty-fields).

## Why does a date show only a year?

Because the source gave only a year. "2027" means "in 2027", not 1 January. See [Numbers and dates](numbers-and-dates).

## Why is a campus counted under a company that is not building it?

[Capex](capex) totals by **buyer** — the company that will use the capacity — not by builder. A developer building for a cloud company is counted under the cloud company.

## Why is a campus counted under no buyer when its sources name several?

Because nothing says how it divides between them. Sources often name more than one tenant for one campus — a lease chain such as Oracle leasing a site OpenAI uses, or several tenants still in talks — and crediting the whole campus to one of them would be a guess. A building or phase that names its buyer still counts for that buyer. The [Capex](capex) page lists each such campus with the buyers its sources name.

## What does "suspected delay" mean?

The campus's expected online date has passed — the whole month, quarter or year the source named — nothing reported since says it is running, and a search for news of it going live, made after the date passed, found none. It is a suspicion, not a finding — usually a slip nobody has reported. See [Numbers and dates](numbers-and-dates).

## Why are two rows the same campus?

Different articles named it differently and the tracker could not yet tell they are one site. Capex counts such a group once. See [Duplicates](duplicates).

## A link to a campus says "No project #N".

The row was most likely merged into another as a duplicate. Search [Projects](projects) by its name to find the surviving row.

## I'm not getting the morning email.

The email follows your watchlist on [Monitor](monitor). It is not sent if your watchlist is empty, or if you have **watch everything** switched on. On a day with nothing new, it still arrives with a summary of what is open — check your spam folder.

## How do I stop the morning email?

Empty your watchlist, or switch on **watch everything**. There is no separate unsubscribe setting.

## The AI panels say they are not switched on.

The AI panels cost money each time they run, so they are switched on per account. If the panel says they are not switched on for your account, ask an administrator; if it says they are switched off on this console, they are off for everyone. See [A project's page](project-page).

## How current is the data?

It is updated every evening; **last crawl** on [Updates](updates) shows when new articles were last fetched. The [status page]({{status}}) shows when each night's update ran and how it ended. See [How the data is kept current](how-it-is-kept-current).

## Is the tracker down?

The [status page]({{status}}) says whether the console, the API and the documentation are answering, checked every few minutes, with 90 days of history and every outage and restart listed. It is public, so it works without signing in.

## Can I download the data?

Not as a file from the console. A program can read it through [the API](api), with a key from your Account page.

## Is the console available in Chinese?

The console's interface is in English. These docs are in English and Chinese — switch with the button at the top of any page.
