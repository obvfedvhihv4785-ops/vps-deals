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
| `data/offers.json` | Dataset, overwritten on every run. Do not hand-edit. |
| `data/page_state.json` | Per-page content hash and the date that content last changed. This is what makes `sitemap.xml` `lastmod` mean something — see below. |
| `site/` | Build output, regenerated each run. Do not hand-edit. |

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
python -m http.server -d site 8000
```

Both scripts read their configuration from `.ilang/site.ilang` at run time. Nothing is
hard-coded — changing a provider there and re-running changes the site.

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

## A note on affiliate links

Some outbound links may become affiliate links once a provider's published partner programme
accepts this site. The fourth column of each `PROVIDERS` row is where those go. Affiliate status
never influences which price is shown, because no person chooses the price — the extraction step
returns whatever the page states, and a price it cannot read is published as nothing.

---

*Site rules are described using the I-Lang protocol — see [`.ilang/site.ilang`](.ilang/site.ilang); protocol notes at ilang.ai*
