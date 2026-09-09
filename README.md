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
        │                    ├─▶ website crawl    homepage → contact / about / team
        │                    │      └─▶ parse     mailto:, text, entities, Cloudflare,
        │                    │                     "info [at] domain (dot) com", JSON-LD
        │                    │
        │                    ├─▶ permutations     only when nothing was found:
        │                    │                     info@ contact@ hello@ office@ …
        │                    │
        │                    └─▶ verification     cached, budgeted, stops at first hit
        │
        └─▶ leads.csv · leads_emails.csv · leads.json · leads.xlsx · SQLite
```

## Quick start

```bash
make dev                      # venv + install + dev deps
cp .env.example .env          # add your API keys
gmscrape providers            # what's configured?
gmscrape doctor               # config + connectivity check

gmscrape run "dentist in austin tx" "plumber in miami fl"
gmscrape run -f examples/queries.txt --limit 60 --format all
```

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

## Local businesses vs. national chains

The point of the run is local operators, so every business is scored against
~740 known brands and ~510 corporate domains, plus signals like store numbers
in the name (`Walmart Supercenter #1234`), franchise wording and review volume.

```
Walmart Supercenter #1234   chain  ['brand_prefix:walmart supercenter',
                                    'corporate_domain:walmart.com',
                                    'store_number_in_name']
Great Clips                 chain  ['brand_exact:great clips', 'corporate_domain:greatclips.com']
Riverside Taqueria          local  — popular (2,900 reviews) but not a brand
Austin Family Dental        local
```

Chains are never guessed at, and `--chain-mode` decides what happens to them:
`flag` (default, keep with `is_chain=yes`), `skip` (drop) or `only` (keep just
the chains). Emails actually *found* on a chain's site are still reported —
they're real, just rarely the local decision-maker.

## Output

`out/leads.csv` — one row per business (best email + counts + chain flags +
crawl diagnostics), `out/leads_emails.csv` — one row per address, ready to
import into a sending tool. Plus `leads.json`, `leads.jsonl`, `leads.xlsx`
(two filtered, frozen-header sheets), and everything in SQLite.

Every address carries a **confidence 0-100** blending provenance, verification
and context, so a `mailto:` beats a guess and an unverified guess can never
outrank a scraped address:

```
office@joesplumbing.com      mailto              valid   93
joe.plumber1972@gmail.com    html_text           valid   74   personal mailbox
dispatch@joesplumbing.com    obfuscated          risky   58
info@bluebonnetroofing.com   permutation         valid   50   guessed
sales@bluebonnetroofing.com  permutation         invalid  —   dropped
```

## Commands

```bash
gmscrape run "med spa in scottsdale az"    # full pipeline
gmscrape enrich --places-file places.csv   # email stages only, no Maps call
gmscrape extract https://acme.com          # crawl one site, print what's found
gmscrape guess acme.com --business-name "Joe's Plumbing"
gmscrape verify info@acme.com sales@acme.com
gmscrape probe-maps https://api.example.com --key KEY   # discover an API's shape
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
--max-pages N          pages per site (default 6)
--concurrency N        parallel site fetches (default 12)
--min-confidence N     drop weak addresses
--format all           csv + json + jsonl + xlsx
--no-robots            ignore robots.txt
```

## Re-runs are cheap

SQLite caches fetched pages (`CACHE_TTL_HOURS`, default a week), every
verification result, and per-domain MX/catch-all facts. Re-running the same
queries re-fetches nothing and re-verifies nothing — a second identical run
spends zero API credits. Businesses are deduplicated across overlapping
queries by place ID, then domain, then phone.

## Politeness and compliance

Concurrency is capped globally and per host, `robots.txt` is honoured by
default, requests retry with backoff, responses are size-capped, and user
agents rotate. Scraped contact data is still regulated — CAN-SPAM, GDPR/ePrivacy
and each API's terms all apply to what you do with the output.

## Tests

```bash
make test     # 63 tests, no network or API keys needed
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
  web/                fetch (async, robots, cache) · crawl · extract
  emails/             patterns (permutations) · score (confidence)
  filters/chains.py   local business vs. national chain
  store/              SQLite cache + results · CSV/JSON/XLSX export
  data/               brand, free-mail, platform and junk-domain lists
```
