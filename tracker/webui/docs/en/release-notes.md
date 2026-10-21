# Release notes

Changes a reader of the console will notice, newest first. The console's version number is in its top bar.

## 21 October 2026

### A project's page, rewritten for reading

- **A summary first**: planned, running, how much is still to come and when, the expected online date, the delay risk with its reasons, and the investment with its cost per megawatt; then the buyers, operator, power, what to watch for, the biggest obstacle and how well sourced the row is. Hover a figure for the sentence behind it. See [A project's page](project-page).
- **Sections in an investor's order.** Progress and timeline, In context and Citations start closed; click a heading to open or close a section, and the page remembers.
- **The twelve fields in two columns**, one line each. The sentence is a hover away, and opening a field shows every claim made about it.
- **Obstacles one line each**, the sentence when you open one, resolved ones folded away. A tranche list shows its first twelve sections.
- **A running campus with capacity still to come is read for delay risk** on its slips and obstacles — on its page and on its company's — rather than counted as low because part of it runs.

### Numbers that agree on every page

- **Running capacity is never more than planned.** When the two disagree, the better-supported figure decides which one describes something else. Englewood no longer reads "18 MW planned, 6,750 MW running": the 6,750 was kilowatts written as megawatts, and such a figure is now set aside. See [Numbers and dates](numbers-and-dates).
- **The tranche panel's headline is the campus's own running figure**, the same as the card above it. A line under the bar says when a campus's tranches describe more than the campus. See [A project's page](project-page).
- **One name per company, everywhere.** AWS, Amazon Web Services and Amazon all read *Amazon*; Facebook/Meta reads *Meta*. A campus's page still shows its source's spelling beside the name.
- **A company's totals are the Capex page's.** A campus stored twice counts once, a cancelled one not at all, and a tenant gets its own buildings rather than the whole campus. See [Company pages](companies).
- **A campus whose sources name several buyers is credited to none of them** until a source says how it divides. Fermi America's 11 GW Amarillo campus had been counted as Amazon's. See [Capex](capex).
- **Power stations and solar farms are not campuses.** Three built to supply AWS in Mississippi are gone, and new ones are kept out.

### Delays

- **Suspected delays.** A campus whose expected online date has passed — the whole month, quarter or year it names — with no news of it running is marked in amber: on its page, in a **Late** column and a **Suspected delays only** filter on [Projects](projects), and on its company's page. See [Numbers and dates](numbers-and-dates).

### Company pages

- **Read in megawatts.** The cards are planned, running, under construction, and due in the next three years. **Stage, in megawatts** splits the planned figure by how far along it is.
- **Supply and delay risk.** A new **Supply** section shows the next three years of capacity by year, each campus's share, and its delay risk — high, elevated or low — with the evidence behind it. A campus that is a fifth or more of that supply and could slip is named above the bars. See [Company pages](companies).

### Project pages and Projects

- **Who is involved is a list of names**, one line per role. Hover a name for the sentence behind it; **new** marks a company first named in the last 30 days.
- **Fields are named in words**, in English and Chinese — *Planned · MW, 规划容量* — with the stored name shown when a field is opened.
- **The Projects table fits the window** and its column names stay in view as you scroll. Town and state are one **Location** column, and the columns run in reading order.

## September–October 2026

### Keeping up with news

- **Every campus is searched for news.** Each night the tracker asks what has been said about each campus since it last looked, so a campus going live, breaking ground or being cancelled is picked up within a day or two, not when somebody happens to research it. See [How the data is kept current](how-it-is-kept-current).
- **Builders' own sites are read every night.** Anything new on a builder's own data-center site — Meta's "Hello, Tulsa!" and "Kuna, we are online!" posts, for example — is read the next night. Campuses those sites announced long ago and the tracker had missed are being added now.

### Status and map

- **A status page.** The [status page]({{status}}) shows whether the console, the API and these docs are answering, and when each night's update ran. It is public, so it works without signing in.
- **A new Map.** The [Map](map) shows the national and state totals, counted the way the Capex page counts, sizes each dot by its planned capacity, and lets you hide a stage, zoom to 10× and read every state's figures in a table. The 3D view and the confidence and operator colours are gone.

### API

- **A read-only API.** Programs can read campuses, their figures and sources, companies, capex by buyer and your watchlist's news, with a key made on your Account page. See [The API](api).
- **API access per account.** An administrator can switch it on or off for each person on the Admin page. Off stops that person's keys without deleting them. Accounts that signed up without an invite code start with it off.

### Documentation

- **These docs.** The console's Help page is replaced by this documentation site, in English and Chinese, with search. **Help** in the console's top bar opens it.

### Reading the data

- **A date promising a period is no longer called overdue when the period starts.** "Expected in the second half of 2026" is not late until 2027. Before this, some correct expected dates were cleared as overdue.
- **Queued articles nobody can read are dropped** instead of waiting at the back of the queue, so the queue reflects what can actually be read.
- **Directories and wikis only fill gaps.** A facility directory or wiki no longer outweighs a first-hand source; it fills a field only when nothing first-hand states it.
- **One building's figure no longer stands as the whole campus's** while a figure for the whole campus exists.
- **Fields nobody publishes count as answered** once searched for twice, so a campus is no longer held back for facts that do not exist.

### Pages

- **Company pages.** Every company has a page with its campuses, where it builds and where it is a tenant, and its recent news. Company names across the console link to it.
- **A citations page for each campus,** so a campus with dozens of sources stays readable.
- **Watch one project** with ☆ on every Projects row and on each project's page.
- **Monitor** replaces *Watch for*; the watchlist is edited there. Old links still work.
- **The relief map** shows columns that read at a glance.

### Updates and email

- **The email reports news days old, not weeks,** and says when each item was reported.
- **Old articles found late no longer appear as news.** An article is dated by when it was published, not when the tracker found it.
- **Stories from publishers that block automated reading** reach the tracker again, through other outlets carrying the same news.

## Full changelog

Every change to the tracker as its developers record it — technical, in English, and long — by month and week, newest first, each week split into what was added, changed and fixed. Only the titles load with the page; open a change to read it.

{{changelog}}
