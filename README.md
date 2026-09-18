# vps-deals-promo-radar

**Live site:** https://vps-deals-radar.pages.dev *(update this line once the domain is registered)*

A self-updating VPS pricing index. Every six hours it re-reads the public pricing pages of
the providers listed in [`.ilang/site.ilang`](.ilang/site.ilang), extracts the lowest monthly
price each page actually states, and republishes the site.

The point is not to be another coupon blog. The point is that **every number on the site can
be checked**. Each price is shown next to the exact sentence it was read from, and links to the
page it came from.

---

## What it does

| | |
|---|---|
| Providers tracked | 23 (list lives in `.ilang/site.ilang`) |
| With a machine-readable price | 22 at the time of writing |
| Refresh cycle | every 6 hours, via GitHub Actions |
| Running cost | zero — no server, no database, no API keys, no LLM calls at runtime |
| Dependencies | none beyond the Python standard library |

## What it deliberately does not do

- It never invents a price, a discount, a rating, or a review count.
- It never estimates a missing figure, or substitutes a similar provider's price.
- It never carries a number forward silently — a failed refresh is labelled `stale` and dated.
- It never publishes `Offer` structured data for a price it cannot quote.
- It never bypasses anti-bot protection, and it honours `robots.txt`.
- It never converts between currencies. EUR and USD entries are shown as published and ranked
  **within** their own currency, so nothing implies a cross-currency ordering that no exchange
  rate was applied to justify.

Providers whose pricing is JavaScript-only, or who refuse automated requests, are listed with
**no price at all**. A blank is more useful than a guess.

---

## How it works

```
.ilang/site.ilang   ← the single source of truth: brand, niche, providers, fields, settings
        │
        ├─► scraper.py ──► data/offers.json     fetch public pages, extract prices + evidence
        │
        └─► build.py   ──► site/                render templates, JSON-LD, sitemap, robots.txt
                                  │
                                  └─► Cloudflare Pages ──► https://vps-deals-radar.pages.dev
```

`.github/workflows/update.yml` runs the whole chain every six hours and commits whatever
changed. Each commit is both an activity signal and a record of what the site claimed on that
date.

### Files

| Path | Role |
|---|---|
| `.ilang/site.ilang` | Site rules. Providers, fields, settings, render switches. The only place to edit the provider list. |
| `AGENTS.md` | Written in I-Lang, for any AI tool that later touches this repository. States scope, allowed actions, and hard prohibitions. |
| `ilang.py` | Parser for the I-Lang config format. Read-only. |
| `scraper.py` | Fetches pages, extracts prices with their evidence text. Never invents a value. |
| `build.py` | Renders `templates/` into `site/`. Generates JSON-LD, `sitemap.xml`, `robots.txt`. |
| `templates/` | Presentation only. Contains no pricing logic. |
| `tests/test_extraction.py` | The term and renewal extractions, tested against verbatim excerpts from the live pages. Run it with `python tests/test_extraction.py`. |
| `data/offers.json` | Dataset, overwritten on every run. Do not hand-edit. |
| `data/history.json` | Append-only price history. One entry per provider per observable change. See below. |
| `data/page_state.json` | Per-page content hash and the date that content last changed. This is what makes `sitemap.xml` `lastmod` mean something — see below. |
| `site/` | Build output, regenerated each run. Do not hand-edit. |

### Why the price history matters more than it looks

`data/history.json` records a provider's price only when the observable state (price, currency,
status) changes, so it stays small and every entry marks something real. It is the one asset here
that **cannot be backfilled**: a run that does not record a price loses that observation forever,
which is why it started on the first run rather than once the site "needed" it. A record of how
twenty-odd providers actually moved their pricing over months is something no affiliate page and
no competitor has, and it is the part of this dataset that gets more valuable the longer the
pipeline runs. `verify.py` treats it as append-only: duplicated consecutive entries and
out-of-order dates fail the build, because a rewritten history is unrecoverable.

### The term is published with the price, not hidden behind it

Not every figure a host writes as "/mo" is a month-to-month price. When this was first checked
against the live pages, several priced providers turned out not to be quoting a plain monthly rate
at all: two were the rate for prepaying **24 months**, one was a **12-month** rate, one was a
promotion that holds for the **first three months**, one showed term toggles (`36 M 12 M 1 M`)
beside a single price without saying which one it belonged to, and one was an **annual** prepay.
The comparison table states the current count rather than this paragraph, because the count moves
whenever a provider is added.

Publishing them as bare per-month numbers would have made the comparison table wrong in the way
that matters most — a reader choosing between `$2.09` and `$2.50` would really be choosing between
a two-year commitment and no commitment at all, and would have no way to see that. So the scraper
reads the commitment off the page and the site shows it in a **Term** column, on the provider and
deal pages, and in the page `<title>`.

The extraction is deliberately narrow. The term is only accepted when it sits immediately beside
the figure and is joined to it by nothing more than a price connector, so "free SSL for the first
12 months. Only $4.50/mo" is not read as an introductory price. When a page offers several terms
beside one price, the result is "term not stated" rather than a guess at one of them. A term read
from the wrong sentence would be a false statement about money, which is worse than the silence it
replaced — and `verify.py` now checks that any claimed term is actually supported by the text the
scraper quoted.

One further case came out of this. Hostinger does not put the term beside the price at all; it
states a rule once for the whole page:

> All plans are paid upfront. The monthly rate reflects the total plan price divided by the number
> of months in your plan.

That single sentence means every `/mo` figure on that page is an amortised prepay. Reading it as
"month to month" — which is what a Term column that defaults to the friendliest answer would do —
would have been wrong about all of them at once. So the page-level rule is detected too, and
because it applies to every price rather than one, it carries its own quoted sentence in
`billing_term_evidence` rather than reusing one figure's snippet. Both halves of the rule must be
present: "paid upfront" alone can describe a setup fee, so a page saying only that is left alone,
and so is a page with the amortisation sentence but no upfront statement.

Where a provider states **no** term, the column says *not stated*. It does not say "month to
month". Those are different claims, and only one of them is supported by a page that is silent.

### The renewal price is published too

The headline figure on a hosting page is usually a first-term price, and the provider almost
always says so within a few words of it. A large share of the tracked providers state a renewal
price beside the advertised one, and the gap is not small. The comparison table carries the live
count; these are the figures as first recorded:

| Provider | Advertised | Renews at |
| --- | --- | --- |
| HostGator | $2.09 | **$4.68** |
| DreamHost | $8.99 (first 3 months) | **$15.99** |
| Hostinger | $6.49 | **$11.99** for 2 years |
| Verpex | $10 | **$19.99** |
| InMotion Hosting | $9.99 | **$16.99** |
| ScalaHosting | $29.95 | **$54.95** |
| MilesWeb | $69.99 | **$89.99** |
| Bluehost | $4.69 | **$5.69** |

A deals page that publishes only the left column is showing half the offer, so both are shown —
in the **Renews at** column on the comparison table, as a callout and a fact on the deal page, in
the page description and in the `Offer` JSON-LD. Each figure is quoted from the provider's own
page in a "What it renews at" section, so it can be checked rather than trusted.

Three guards keep this honest, and all three exist because of a specific failure seen in testing:

- **It must be a rise.** A renewal at or below the headline is not a caveat, and publishing it as
  one would be an invented warning.
- **No other price may sit between the figure and the sentence.** In a plan table the nearest
  following sentence often belongs to the *next* plan down; without this guard HostGator's
  `$2.09` was being paired with the `$9.35` renewal of a larger plan.
- **Currencies never mix.** A `$10` headline is not compared with a `€12.99` renewal, not even to
  warn.

The renewal *period* is read too, so the site says "renews at $11.99/mo **for 2 years**" rather
than a bare figure that reads like a rate held indefinitely.

`verify.py` gates all of it: a published renewal must be a positive number, above the advertised
price, in the same currency, traceable to its own quoted evidence, and that evidence must contain
the wording that makes it a renewal claim. Metadata without a figure, and a figure without
evidence, both fail the build.

### Why `lastmod` is tracked separately

A full refresh rewrites every page every six hours, so using the run timestamp as `lastmod`
would tell a crawler that all 48 URLs changed four times a day — which is how you teach Google
to ignore the field. `build.py` instead hashes each page with the run stamp and the per-record
"verified on" dates masked out, and only advances `lastmod` when that hash actually moves.
A refresh that changes nothing changes no dates, and makes no commit.

### The CI gate refuses to publish a bad crawl

`verify.py` runs before the commit step and fails the job on malformed markup — unparsable
JSON-LD, a missing canonical, a price that is not a clean decimal, a price that does not appear
in its own quoted evidence. It also acts as a circuit breaker: if the number of providers with a
readable price collapses by more than 40% in one run, the job fails and the previous good commit
stays live, because a blocked or rate-limited crawl is far likelier than a dozen providers
changing their pages simultaneously. Re-run it, or delete `data/offers.json` to accept the new
baseline.

### Run it locally

```bash
python scraper.py     # ~1 minute; fetches every provider page
python build.py       # writes site/
python verify.py      # the gate — must pass before anything ships
python -m http.server -d site 8000
```

Both scripts read their configuration from `.ilang/site.ilang` at run time. Nothing is
hard-coded — changing a provider there and re-running changes the site.

### Or run the whole thing in one command

```bash
bash refresh.sh                 # scrape -> build -> verify -> commit -> publish
SKIP_DEPLOY=1 bash refresh.sh   # stop before publishing
```

Machine-specific settings — interpreter paths, the Cloudflare account id, the directory holding
the API token — are deliberately **not** in this repository. They live in
`$HOME/.workbuddy-ai/vps-deals.env`, which `refresh.sh` sources if it exists; see
`vps-deals.env.example`. Everything falls back to a portable default (`python3`, `npx wrangler`)
and `refresh.sh` exits `1` rather than deploying when no account id is configured. That way the
published script carries no local paths, no username, and no account identifier, and it runs
unchanged on a different machine.

`refresh.sh` exists so any scheduler can drive the pipeline without restating the ordering or
the safety rules. It refuses to commit or deploy when `verify.py` fails, so a malformed page, a
price that does not match its own quoted evidence, or a crawl that lost most of its providers
never reaches the live site. Exit codes: `0` ok, `1` scrape/build failed, `2` gate rejected it,
`3` source files are dirty.

That last one is worth explaining. `site/` is a build artefact, and the promise this repository
makes is that you can clone it and rebuild the published output byte-for-byte. `build.py` runs
from the working copy, so if `build.py`, `templates/` or `.ilang/site.ilang` have uncommitted
edits, the run would commit output built from code the repository does not contain and the two
would quietly drift apart. So `refresh.sh` checks the source paths first and exits `3` before
scraping anything. Commit or stash your edits, then re-run.

Three ways to schedule it, in order of preference:

1. **GitHub Actions** — `.github/workflows/update.yml`, runs every 6 hours, free on a public
   repository. This is the intended production setup.
2. **Any cron/Task Scheduler** on a machine that is on — just call `refresh.sh`.
3. **WorkBuddy automation** — a recurring task pointed at `refresh.sh`. Needs no third-party
   credentials at all, so it works before any repository exists. Remove it once option 1 is live,
   otherwise the pipeline runs twice.

### Adding or removing a provider

Edit one line in `.ilang/site.ilang`:

```
ProviderName | https://homepage.example/ | https://homepage.example/pricing |
```

The fourth cell is for an affiliate link and may be left empty, in which case the plain
official URL is used. Then re-run the pipeline. No code changes.

---

## Deploying

The site is static output in `site/`, so anything that serves files will do. This project uses
Cloudflare Pages:

- **Build command:** `python build.py`
- **Output directory:** `site`

`update.yml` also contains an optional Wrangler deploy step. It activates only if
`CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are set as repository secrets, so the site
can be published either through the Pages Git integration or entirely from CI.

---

## Registering a domain

While the site lives on `*.pages.dev`, its age belongs to Cloudflare. Search engines date a site
from the domain, and domain age is not something that can be bought or backfilled later — it only
starts counting from the day of registration. That is the whole argument for registering early
rather than after the content is good.

Availability and price below were read from Cloudflare Registrar's live registry check
(`POST /accounts/{account_id}/registrar/domain-check`), which is authoritative and at-cost — the
registrar charges the registry fee with no markup. Prices are USD per year and were current when
recorded:

| Domain | Register | Renew | Notes |
| --- | --- | --- | --- |
| `vps-deals.com` | 10.46 | 10.46 | exact match to `brand`; flat renewal |
| `vps-deals-radar.com` | 10.46 | 10.46 | matches the current Pages project name |
| `vps-deals.org` | 8.50 | 11.20 | cheaper first year, dearer to keep |
| `vps-deals.net` | 11.86 | 11.86 | |
| `vps-deals.io` | 32.00 | 50.00 | renewal is the real price |

Every `vpsdeals.*` without the hyphen (`vpsdeals.com`, `.net`, `.org`, `.co`) is already
registered by someone else, as is `vpsradar.com`. The hyphenated form is the one that is free.

Two things block an automated registration, and both need a human:

1. **The account has no billing profile.** `GET /accounts/{account_id}/billing/profile` returns
   `1324 Your billing profile could not be loaded`. A payment method must be added at
   `https://dash.cloudflare.com/{account_id}/billing/payment-info` first. Registration charges
   that card and, per the API, **all successful registrations are non-refundable**.
2. **Registrant contact details.** A `.com` registration requires
   `contacts.registrant` with `phone`, `email` and `postal_info`. Those are the owner's real
   details and are not something this project should invent.

Once registered, the domain needs a DNS record pointing at the Pages project, and then every
canonical, Open Graph URL and `sitemap.xml` entry must switch from `*.pages.dev` to the new
domain. `base_url` is derived from `domain` in `.ilang/site.ilang`, so that is a one-line change
followed by a rebuild — but it must happen in the same step as the DNS cutover, or the site will
be serving canonicals that point at the old host.

---

## A note on affiliate links

Some outbound links may become affiliate links once a provider's published partner programme
accepts this site. The fourth column of each `PROVIDERS` row is where those go. Affiliate status
never influences which price is shown, because no person chooses the price — the extraction step
returns whatever the page states, and a price it cannot read is published as nothing.

---

*Site rules are described using the I-Lang protocol — see [`.ilang/site.ilang`](.ilang/site.ilang); protocol notes at ilang.ai*
