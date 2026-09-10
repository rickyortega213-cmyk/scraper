# gmscrape

Google Maps lead scraper for **local businesses**. You give it queries like
`business type in location`; it finds the businesses, visits their websites,
parses the HTML for real email addresses, guesses the likely ones when a site
publishes none, and verifies everything before it lands in your CSV.

```
"dentist in austin tx"
        │
        ├─▶ Maps API ──▶ businesses (name, site, phone, rating, reviews…)
        │                    │
        │                    ├─▶ chain check      Walmart / McDonald's / Great Clips → flagged
        │                    │
        │                    ├─▶ no website?      web search → pick their site → confirm it
        │                    │                     (phone / name / address must be on the page)
        │                    │
        │                    ├─▶ website crawl    homepage → contact / about / team
        │                    │      ├─▶ emails    mailto:, text, entities, Cloudflare,
        │                    │      │             "info [at] domain (dot) com", JSON-LD
        │                    │      └─▶ owner     "Jane Doe, Owner" · "founded by" · JSON-LD
        │                    │                     → not named? web search + AI Overview
        │                    │
        │                    ├─▶ permutations     general: only when nothing was found
        │                    │                     owner:   jane@ jane.doe@ jdoe@ …
        │                    │
        │                    └─▶ verification     cached, budgeted, stops at first hit;
        │                                          a guess is a lead only once it verifies
        │
        └─▶ leads.csv · leads_emails.csv · leads.json · leads.xlsx · SQLite
```

## Quick start

```bash
make dev                      # venv + install (one time)
source .venv/bin/activate
scraper buddy                 # that's it
```

`scraper buddy` walks you through everything:

```
  API keys on file
    Scraper Tech (Google Maps) ............ a25e…ce8   keep it? [Y/n]
    MailTester Ninja (email verification)  sub_…ABC   keep it? [Y/n]
    OpenWeb Ninja (web search) ............ not set    add it now? [y/N]
    Supabase URL (live table) ............. not set    add it now? [y/N]

  Searches - paste them, one per line (business type in location),
  then press Enter on an empty line. Or type the path to a .txt / .csv file.
    > dentist in austin tx
    > plumber in miami fl
    >

  2 searches. Businesses per search [40]:
  Start? [Y/n]
```

Then it runs, fills the live table, prints the finished leads and writes
`out/leads.csv`. Keys are remembered between runs. The full CLI is still there
for scripting: `gmscrape run "dentist in austin tx" -n 40`, `gmscrape run -f
queries.txt`, `gmscrape setup`, and everything below.

Keys are saved to `~/.config/gmscrape/config.env` (owner-only permissions), so
they work from any folder and survive a fresh clone. The first `run` with no
keys opens the setup wizard by itself. Every run starts by showing what it's
about to use:

```
maps: scraperapi   verification: mailtester   web search: openwebninja   supabase: off
Press Enter to start, or type k to change keys:
```

`k` opens the wizard (Enter keeps a value, typing replaces it, `-` clears it);
`gmscrape keys` shows them masked; `-y` skips the check for scripts and cron.
A project `.env` still works and takes precedence over saved keys, and real
environment variables beat both.

## Plugging in your APIs

Both API layers are adapters, so nothing in the pipeline changes when you swap
vendors. Set **one** key and `MAPS_PROVIDER=auto` picks it up.

**Google Maps** — built-in support for `scraperapi`, `serpapi`, `serper`,
`outscraper`, `apify`, `scrapingdog`.

```bash
SCRAPERAPI_KEY=...     # or SERPAPI_KEY / SERPER_KEY / OUTSCRAPER_KEY /
                       #    APIFY_TOKEN / SCRAPINGDOG_KEY
```

The ScraperAPI adapter targets their structured endpoint
(`/structured/google/mapssearch`) and reads listings from `local_results`,
`results`, `places` or a bare array, so a shape change doesn't break it. Paging
stops as soon as a page returns nothing new, so it collects everything when
`page` is honoured and never loops when it isn't.

**Using an API that isn't on that list?** Describe it in JSON instead of writing
code — copy `examples/maps_api.example.json`, adjust, and point at it:

```bash
GENERIC_MAPS_CONFIG=my_maps_api.json
MAPS_API_KEY=your-key
```

```json
{
  "url": "https://api.example.com/v1/places/search",
  "headers": {"Authorization": "Bearer {api_key}"},
  "query": {"q": "{query}", "page": "{page}", "limit": "{page_size}"},
  "results_path": "data.results",
  "pagination": {"style": "page", "start": 1, "size": 20}
}
```

Placeholders: `{api_key} {query} {business_type} {location} {limit} {page}
{offset} {page_size} {language} {country} {cursor}`. Pagination styles: `page`,
`offset`, `cursor`, `none`. Field names like `name`/`title`,
`website`/`site`/`url`, `phone`, `address`, `avg_rating`, `review_count`,
`gmaps_id` are recognized automatically; add a `field_map` only for unusual ones.

### Don't know your API's shape? Let it work that out

`probe-maps` calls the endpoint a handful of times, figures out how it wants to
be called, and writes the config for you:

```bash
gmscrape probe-maps https://api.example.com --key YOUR_KEY \
    --query "dentist in austin tx" --write my_maps_api.json
```

```
✓ found a working shape after 3 request(s)
  endpoint     https://api.example.com/maps
  auth         query:apikey
  search param q
  listings at  data.results (2 rows)

  place field   value
  name          Austin Family Dental
  website       https://austinfamilydental.com
  phone         (512) 555-0142
  reviews       412
```

It is frugal, because successful calls cost credits: one request establishes
which path exists, the next which auth style is accepted (a `400` means the key
worked and only the parameters are off — that is already the answer), the next
what the search parameter is called. Usually three requests, never more than
`--max-requests` (default 12). Keys are redacted in all output.

If nothing works, the status codes tell you what to do next: `401/403`
everywhere means the key isn't accepted in any style tried, `400/422` means
auth worked but a required parameter is missing (add it with `--param key=value`
and re-run), and `404` on every path means you should pass the exact endpoint
URL rather than the host.

**Email verification** — built-in support for `mailtester` (MailTester Ninja),
`millionverifier`, `zerobounce`, `neverbounce`, `reoon`, `emaillistverify`,
`bouncer`, or your own via `GENERIC_VERIFY_CONFIG` (see
`examples/verify_api.example.json`, which also supports a bulk endpoint).
Vendor vocabularies (`deliverable`, `ok`, `accept_all`, `catch-all`, …) are
normalized to `valid / invalid / risky / catch_all / disposable / unknown`.

```bash
MAILTESTER_KEY=sub_...          # your MailTester Ninja subscription id
```

MailTester Ninja's two-step flow is handled for you: the key is exchanged for a
short-lived bearer token, the token is cached for its whole lifetime (read from
its JWT `exp`), and it is re-fetched automatically when it lapses or the API
rejects it mid-run. Their `code` values map as `ok` → valid, `ko` → invalid,
`mb` (mailbox busy/greylisted) → unknown, `ca` → catch-all; an `ok` whose
message reveals a catch-all domain is downgraded rather than sold as
deliverable. An unrecognized code becomes `unknown`, never `valid` — the raw
`code:message` is always kept in `sub_status`, so `gmscrape verify` shows you
the exact vocabulary on the first live call.

With **no verification key at all** the pipeline still runs: it falls back to
local syntax + MX + disposable/junk checks, which can rule an address out but
never confirm a mailbox — those come back `risky`, never `valid`.

**Web search** (website discovery + owner lookup) — OpenWeb Ninja's Real-Time
Web Search, which returns Google organic results plus the AI Overview:

```bash
OPENWEBNINJA_KEY=ak_...
```

Without it, both features are simply off and everything else runs unchanged.
`gmscrape search "any query"` shows exactly what the API returned — organic
hits, AI Overview text, knowledge panel — so you can see the shape on a live
call; `--raw` dumps the JSON.

Already have places from elsewhere? Skip the Maps call:

```bash
gmscrape enrich --places-file leads.csv    # JSON, JSONL or CSV
```

## Finding emails on the website

Small-business sites hide addresses in every way imaginable, so the extractor
handles all of it:

| Published as | Example |
|---|---|
| `mailto:` link | `<a href="mailto:office@acme.com?subject=Quote">` |
| plain text | `sales@acme.com` |
| HTML entities | `info&#64;acme.com` |
| Cloudflare protection | `data-cfemail="7a1813161613…"` → decoded |
| human obfuscation | `info [at] acme (dot) com`, `info AT acme DOT com` |
| structured data | JSON-LD `"email": "mailto:hello@acme.com"` |

It crawls the homepage, ranks internal links (`/contact`, `/about`, `/team`,
`/impressum`…), follows the best few, and stops early once it has an address on
the business's own domain. Then it throws out what a naive regex always drags
in: `logo@2x.png`, `key@sentry.io`, `you@example.com`, `name@yourdomain.com`,
minified-JS artefacts, and `noreply@`/`postmaster@` mailboxes.

**Personal mailboxes are kept.** For local businesses a `@gmail.com` or
`@hotmail.com` address is often the only inbox anyone reads, so they are
reported and tagged `is_personal_domain` rather than filtered out.

## No website on Maps? Find it

Plenty of local businesses have a site Google Maps doesn't link. When the
listing has none, the business is searched (`"Bluebonnet Roofing" Austin TX`)
and every organic hit is scored: does the domain spell the business name, does
the title name it, is the listing's phone number in the snippet? Directories
and social platforms (Yelp, Facebook, YellowPages, Nextdoor…) are excluded
outright, and if two sites are equally plausible nothing is chosen.

Then the winner has to **prove itself**: after the page is fetched it must
contain the business's phone number, name, or street address, or it is thrown
away — `website_status = discovered_unconfirmed`, no emails, no guesses. A
short generic name ("Smile Dental") only counts alongside the phone or address,
because every Smile Dental in the country says "Smile Dental" on its homepage.
A plausible-looking wrong site is worse than none: every address scraped from
it would be a confident, verified, wrong lead.

## Who's in charge

Every business gets **one** decision-maker — the most senior person the
evidence names: owner › founder › CEO › president › principal › managing
partner › director › manager. On medical, dental, legal and vet sites,
"Dr. Jane Doe, DDS" counts as the practice principal.

Names come from explicit statements only — `Jane Doe, Owner`, `Owner: Jane
Doe`, `founded by Jane Doe`, JSON-LD `founder` — never from a bare capitalized
pair. A candidate must look like a person (no page furniture: "Our Team",
"Owner Response", "Director Of Operations" are all rejected), and must not be
the business name itself. When the site doesn't name anyone, the fallback is a
web search (`who is the owner of Joe's Plumbing in Austin`), reading Google's
AI Overview, the knowledge panel and snippets — but **only sentences that also
name the business**, so the owner of the taqueria next door can't be picked up.
If two different people have equal support, nobody is chosen.

```bash
gmscrape owner "Joe's Plumbing" --city Austin     # shows every mention and the verdict
```

Once the owner is known, their mailbox is guessed on the business domain even
when a general address was found, in order of how common each pattern is:

```
jane@   jane.doe@   jdoe@   janed@   jane_doe@   janedoe@   doe@   j.doe@
```

An address already on the site that spells the owner's name (`jdoe@…`) is
recognised as theirs and no guessing happens.

## Two rows when there are two contacts

The leads export has **one row per contact**. A business with both a general
inbox and an owner address gets two rows that are identical in every column —
name, phone, address, city, query — except the contact ones:

| name | contact_type | contact_name | contact_title | email | email_status |
|---|---|---|---|---|---|
| Hill Country Landscaping | owner | Maria Lopez | owner | maria.lopez@hillcountrylandscaping.com | valid |
| Hill Country Landscaping | general | | | info@hillcountrylandscaping.com | valid |
| Walmart Supercenter #1234 | manager | Dana Whitfield | store manager | dana.whitfield@walmart.com | valid |
| McDonald's | owner | Rosa Delgado | franchise owner | rosa.delgado@mcdfranchise.com | valid |

A business with no email still gets one row, so nothing is silently dropped.
The Supabase table follows the same shape (`<business>|general`, `<business>|owner`).

## Guessing addresses (permutations)

When a site publishes nothing usable, addresses are generated from the domain
in tiers, so you trade verification credits for coverage:

| Tier | Local parts |
|---|---|
| 1 | `info` `contact` `hello` |
| 2 (default) | + `office` `admin` `sales` `team` `mail` `support` |
| 3 | + `booking` `service` `quotes` `dispatch` `reception` `orders` … |

Two extras that pay off on local businesses:

- **Owner-style names from the business name** — `Joe's Plumbing` also tries
  `joe@`, `Kowalski Roofing` tries `kowalski@`.
- **Industry mailboxes from the category** — a dentist gets `frontdesk@` and
  `newpatients@`, a plumber `dispatch@` and `estimates@`, a hotel
  `reservations@`.

Guessing is **refused** where it cannot work, with the reason recorded in
`permutations_skipped_reason`:

| Reason | Why |
|---|---|
| `free_mail_domain` | you cannot guess someone's Gmail |
| `platform_or_social_domain` | `*.wixsite.com`, Facebook, Linktree, Yelp… |
| `domain_has_no_mx` | the domain cannot receive mail at all |
| `national_chain` | `info@walmart.com` is not a lead |
| `no_domain` | no website to derive a domain from |

Verification walks the guesses in order and **stops at the first deliverable
one**, so a lead costs one or two credits instead of a dozen. If the domain
turns out to be **catch-all**, guessing stops immediately and the address is
flagged `catch_all_domain_guess_unproven` — a catch-all accepts anything, so a
"pass" there proves nothing.

**A guess becomes a lead only once a verifier said the mailbox exists.** With a
verification key configured, guessed addresses that came back anything other
than `valid` (unknown, risky, catch-all, never checked) stay in
`leads_emails.csv` flagged `lead_eligible = no`, and never appear in the leads
rows. `--allow-unverified-guesses` relaxes this; without any verification key
it is relaxed automatically, since nothing could ever verify.

## National chains: the right local person

Every business is scored against ~740 known brands and ~510 corporate domains,
plus signals like store numbers in the name (`Walmart Supercenter #1234`),
franchise wording and review volume — a popular local taqueria with 2,900
reviews stays local.

Chains are **not** skipped. They go through enrichment looking for the person
who actually runs *that location*, chosen by how the chain is run:

| Kind | Examples | Who we look for | Row |
|---|---|---|---|
| **franchise** | McDonald's, Subway, Great Clips, Anytime Fitness, ServPro, Hampton Inn | the franchise owner in that city | `owner` |
| **corporate store** | Walmart, Target, Home Depot, CVS, Chase, Starbucks, Chipotle | store manager → district / regional manager | `manager` |
| **corporate restaurant** | Olive Garden, Chili's, Texas Roadhouse, Cheesecake Factory | general manager → managing partner | `manager` |

~620 brands are mapped; unknown chains fall back to the category (fast food,
salon, hotel, home services → franchise; restaurant → general manager;
otherwise store manager).

The lookup is by web search — `who is the store manager of Walmart Supercenter
in Austin, TX`, `who owns the McDonald's franchise in Austin, TX` — reading the
AI Overview, knowledge panel and snippets (LinkedIn titles like *Dana Whitfield
- Store Manager - Walmart · Austin, Texas* are exactly the shape it reads).
Guards specific to chains, because "Walmart" is in every snippet on the web:

- a sentence only counts if it names **the brand and the city** — a Dallas
  store manager is never attached to the Austin store
- a name that borrows the brand or the city ("Austin McDonald's") is rejected
- a **corporate executive is never the store contact** — CEO/president mentions
  are dropped rather than demoted
- a single "the store manager of X is Y" sentence is not evidence on its own;
  something else on the web has to name the same person
- no city on the listing → no search (nothing to gate on, no credit spent)

The person's mailbox is then guessed on the **corporate domain**
(`dana.whitfield@walmart.com`) and must verify like any other guess; an address
in a search result that spells their name (a franchisee's email in a press
piece) is picked up directly. Generic `info@walmart.com` guesses are still
never made. `--chain-mode skip` drops chains entirely, `--no-chain-people`
keeps them but skips the person lookup.

## Live lead table in Supabase

Every run streams into a Clay-style table you can watch fill in, and the
finished rows stay there as a running database of your market.

```bash
gmscrape setup --only supabase       # paste Project URL + service_role key (+ optional access token)
gmscrape run "dentist in austin tx"  # that's it - Supabase is on whenever keys are saved
```

The tables are created for you on first use when a Supabase **access token**
(`sbp_…`, from supabase.com/dashboard/account/tokens) is saved; without one,
the run tells you exactly where to paste `gmscrape supabase-init`. Each run
ends with the link:

```
Live table:
  https://supabase.com/dashboard/project/<ref>/editor  → open gmscrape_latest (this run)
                                                          or gmscrape_table (every run)
```

`gmscrape_latest` is the current run; `gmscrape_table` is every run, filterable
by `run_label` ("dentist in austin tx · 2026-09-10"). Rows appear as `queued`
the moment the run starts and advance in place: `queued → crawled → guessed →
verified → done`. Re-running a query updates rows instead of duplicating them.
Writes happen on a background thread and can never fail a scrape — errors are
counted and reported. Use the **service_role** key; RLS stays on so the anon
key can't read your leads. `--no-supabase` skips it for one run.

## Output

The run ends with the finished table in the terminal and `out/leads.csv` —
the same rows, title-cased, one per contact:

| company_name | city | state | address | phone_number | verified_email | contact_first_name | contact_last_name | contact_title | business_type |
|---|---|---|---|---|---|---|---|---|---|
| Hill Country Landscaping | Austin | TX | 9 Elm St, Austin, TX 78701 | (512)-555-0190 | maria.lopez@hillcountrylandscaping.com | Maria | Lopez | Owner | Landscaper |
| Hill Country Landscaping | Austin | TX | 9 Elm St, Austin, TX 78701 | (512)-555-0190 | info@hillcountrylandscaping.com | | | | Landscaper |
| Walmart Supercenter | Austin | TX | … | (512)-555-0001 | dana.whitfield@walmart.com | Dana | Whitfield | Store Manager | Department Store |

Plus `contact_type`, `email` (best candidate even when unverified),
`email_status`, `email_confidence`, `website`, `google_maps_link`, `rating`,
`reviews`, `is_chain`, `search_query`, `run_date`. `verified_email` is filled
only when a verifier confirmed the mailbox exists.

Also written: `leads_detailed.csv` (every diagnostic column — crawl status,
sources, chain reasons, why no guess was made), `leads_emails.csv` (every
address considered and why it was kept or dropped), and `leads.json` /
`.jsonl` / `.xlsx` on request (`--format all`).

Every address carries a **confidence 0-100** blending provenance, verification
and context, so a `mailto:` beats a guess and an unverified guess can never
outrank a scraped address.

## Commands

```bash
gmscrape run "med spa in scottsdale az"    # full pipeline
gmscrape enrich --places-file places.csv   # email stages only, no Maps call
gmscrape extract https://acme.com          # crawl one site, print what's found
gmscrape guess acme.com --business-name "Joe's Plumbing"
gmscrape owner "Joe's Plumbing" --city Austin      # who runs it, with evidence
gmscrape search "who is the owner of Joe's Plumbing"   # raw web search
gmscrape verify info@acme.com sales@acme.com
gmscrape probe-maps https://api.example.com --key KEY   # discover an API's shape
gmscrape supabase-init --write schema.sql   # SQL for the live lead table
gmscrape supabase-check                     # verify Supabase key + tables
gmscrape providers                          # which APIs are wired up
gmscrape doctor                             # config + DNS + HTTPS check
gmscrape stats                              # what's in the database
```

Useful flags on `run`:

```
-n, --limit N          businesses per query (default 40)
--tier {1,2,3}         how aggressively to guess (default 2)
--no-permutations      only report addresses actually found
--verify-budget N      hard cap on paid verification calls
--chain-mode skip      drop national chains entirely
--no-chain-people      keep chains but skip the franchisee / manager lookup
--max-pages N          pages per site (default 6)
--concurrency N        parallel site fetches (default 12)
--min-confidence N     drop weak addresses
--format all           csv + json + jsonl + xlsx
--no-robots            ignore robots.txt
--no-discover          don't search for a website when Maps has none
--no-owners            skip owner lookup and owner-address guessing
--no-owner-search      find owners on the site only, never via web search
--allow-unverified-guesses   let unverified guesses become lead rows
--no-supabase          skip the live table for this run
```

## Re-runs are cheap

SQLite caches fetched pages (`CACHE_TTL_HOURS`, default a week), every
verification result, every web search (30 days), and per-domain MX/catch-all
facts. Re-running the same
queries re-fetches nothing and re-verifies nothing — a second identical run
spends zero API credits. Businesses are deduplicated across overlapping
queries by place ID, then domain, then phone.

## Politeness and compliance

Website crawls, web searches and verifications all run concurrently
(`HTTP_CONCURRENCY`, `WEB_SEARCH_CONCURRENCY`, `VERIFY_CONCURRENCY`), and the
crawl stops the moment it has both an on-domain address and a named owner.
Concurrency is capped globally and per host, `robots.txt` is honoured by
default, requests retry with backoff, responses are size-capped, and user
agents rotate. Scraped contact data is still regulated — CAN-SPAM, GDPR/ePrivacy
and each API's terms all apply to what you do with the output.

## Tests

```bash
make test     # 171 tests, no network or API keys needed
```

The end-to-end test serves fake business sites over real HTTP and runs the
whole pipeline against them: obfuscated and Cloudflare-encoded addresses get
decoded, noise gets filtered, the unreachable site falls through to guessing,
Walmart gets flagged and skipped, verification stops at the first hit, and the
second run is served entirely from cache.

## Layout

```
gmscrape/
  cli.py              commands, flags, tables
  config.py           settings from env / .env / flags
  query.py            "business type in location" parsing
  probe.py            discover an unknown maps API's request/response shape
  core/pipeline.py    orchestration
  providers/maps/     scraperapi, serpapi, serper, outscraper, apify,
                      scrapingdog, generic, file
  providers/verify/   mailtester, millionverifier, zerobounce, neverbounce,
                      reoon, emaillistverify, bouncer, generic, local
  providers/search/   openwebninja (web search: site discovery + owner lookup)
  web/                fetch (async, robots, cache) · crawl · extract · discover
  emails/             patterns (permutations) · people (owner extraction)
                      score (confidence)
  filters/chains.py   local business vs. national chain
  store/              SQLite cache + results · CSV/JSON/XLSX export
                      sinks (live publishing) · supabase (live lead table)
  data/               brand, free-mail, platform and junk-domain lists
```
