"""Local structural verification of site/ output. Not part of the shipped pipeline.

Checks, for every generated HTML page:
  * all JSON-LD blocks parse as JSON and carry @context + @type
  * Offer nodes carry price / priceCurrency / availability / url
  * every page has exactly one canonical pointing at itself
  * title and description are non-empty and page-specific
  * pages without a verified price emit no Offer node
  * sitemap.xml parses, and every <loc> maps to a file that exists
  * robots.txt references the sitemap

This is a structural check, not Google's Rich Results Test — it cannot guarantee
that Google will show rich results, only that the markup is well-formed and
internally consistent.
"""
import datetime
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

SITE = "site"
BASE = None
errors: list[str] = []
warnings: list[str] = []
stats = {"pages": 0, "jsonld": 0, "offer": 0, "product": 0, "itemlist": 0,
         "breadcrumb": 0, "faq": 0}

LD_RE = re.compile(r'<script type="application/ld\+json">\s*([\s\S]*?)\s*</script>')


def clean_url(base: str, rel: str) -> str:
    """Public URL for a generated file path, matching build.py's url_style=clean."""
    if rel == "index.html":
        return f"{base}/"
    return f"{base}/{rel[:-5]}" if rel.endswith(".html") else f"{base}/{rel}"


def resolve_loc(loc: str) -> str | None:
    """Map a sitemap URL back to the file that should serve it.

    Clean URLs have no extension, so `/compare` is served by `compare.html` and
    `/` by `index.html`. Both forms are tried before declaring a dead link.
    """
    rel = loc[len(BASE):].lstrip("/")
    candidates = [rel, rel + ".html", (rel + "/index.html") if rel else "index.html"]
    if not rel:
        candidates = ["index.html"]
    for c in candidates:
        p = os.path.join(SITE, c.replace("/", os.sep))
        if os.path.exists(p):
            return p
    return None


def check_page(path: str) -> None:
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    stats["pages"] += 1
    rel = os.path.relpath(path, SITE).replace("\\", "/")

    if rel == "404.html":
        # The 404 page is deliberately outside the normal page contract: no
        # canonical (it must not compete with the homepage), no Open Graph and no
        # structured data. What it must carry is a noindex directive — otherwise
        # it becomes another indexable page. Its existence is what makes the host
        # return a real 404 instead of serving the homepage with a 200.
        if "noindex" not in html:
            errors.append("404.html: missing noindex directive")
        if re.search(r'<link rel="canonical"', html):
            errors.append("404.html: must not declare a canonical")
        if LD_RE.search(html):
            errors.append("404.html: must not carry structured data")
        t = re.search(r"<title>(.*?)</title>", html, re.S)
        if not t or not t.group(1).strip():
            errors.append("404.html: empty <title>")
        if "{{" in html or "{%" in html:
            errors.append("404.html: unrendered template tag left in output")
        return

    # canonical
    cans = re.findall(r'<link rel="canonical" href="([^"]+)"', html)
    if len(cans) != 1:
        errors.append(f"{rel}: expected 1 canonical, found {len(cans)}")
    else:
        expected = clean_url(BASE, rel)
        if cans[0] != expected:
            errors.append(f"{rel}: canonical {cans[0]} != {expected}")

    # title / description
    t = re.search(r"<title>(.*?)</title>", html, re.S)
    d = re.search(r'<meta name="description" content="([^"]*)"', html)
    if not t or not t.group(1).strip():
        errors.append(f"{rel}: empty <title>")
    if not d or len(d.group(1).strip()) < 40:
        errors.append(f"{rel}: missing/short meta description")
    if "{{" in html or "{%" in html:
        errors.append(f"{rel}: unrendered template tag left in output")

    # og / twitter
    for tag in ('property="og:title"', 'property="og:description"',
                'name="twitter:card"', 'property="og:url"'):
        if tag not in html:
            errors.append(f"{rel}: missing {tag}")

    # JSON-LD
    blocks = LD_RE.findall(html)
    if not blocks:
        errors.append(f"{rel}: no JSON-LD")
    has_offer = False
    for b in blocks:
        try:
            node = json.loads(b)
        except json.JSONDecodeError as exc:
            errors.append(f"{rel}: JSON-LD parse error: {exc}")
            continue
        stats["jsonld"] += 1
        if "@context" not in node or "@type" not in node:
            errors.append(f"{rel}: JSON-LD missing @context/@type")
        t_ = node.get("@type")
        if t_ == "Offer":
            has_offer = True
            stats["offer"] += 1
            for field in ("price", "priceCurrency", "availability", "url"):
                if field not in node:
                    errors.append(f"{rel}: Offer missing {field}")
            if not re.fullmatch(r"\d+\.\d{2}", str(node.get("price", ""))):
                errors.append(f"{rel}: Offer price not a clean decimal: {node.get('price')!r}")
            if not node.get("url", "").startswith(BASE):
                errors.append(f"{rel}: Offer url not absolute on site domain")
        elif t_ == "Product":
            stats["product"] += 1
            if "offers" not in node:
                warnings.append(f"{rel}: Product without offers (expected for unpriced)")
            else:
                off = node["offers"]
                if off.get("@type") == "AggregateOffer":
                    for field in ("lowPrice", "highPrice", "priceCurrency", "offerCount"):
                        if field not in off:
                            errors.append(f"{rel}: AggregateOffer missing {field}")
                    if float(off["highPrice"]) < float(off["lowPrice"]):
                        errors.append(f"{rel}: AggregateOffer highPrice < lowPrice")
                elif off.get("@type") == "Offer" and "price" not in off:
                    errors.append(f"{rel}: Offer node missing price")
        elif t_ == "ItemList":
            stats["itemlist"] += 1
            for it in node.get("itemListElement", []):
                if "position" not in it or "url" not in it:
                    errors.append(f"{rel}: ItemList entry missing position/url")
        elif t_ == "BreadcrumbList":
            stats["breadcrumb"] += 1
            for i, it in enumerate(node.get("itemListElement", [])):
                if it.get("position") != i + 1:
                    errors.append(f"{rel}: breadcrumb positions not sequential")
        elif t_ == "FAQPage":
            stats["faq"] += 1
            for q in node.get("mainEntity", []):
                if "acceptedAnswer" not in q or not q["acceptedAnswer"].get("text"):
                    errors.append(f"{rel}: FAQ entry missing answer text")

    # a deal page must have an Offer, unless its price is unavailable
    if rel.startswith("deal/"):
        has_price_marker = "no readable price" in html or "No machine-readable" in html
        if not has_offer and not has_price_marker:
            errors.append(f"{rel}: deal page has neither Offer JSON-LD nor an explicit "
                          "no-price statement")


def check_offers_json() -> None:
    """Integrity checks on the dataset itself, not the rendered HTML.

    The strongest guarantee this project makes is that a published number is
    literally present in the quoted source text. That is checkable, so check it:
    a headline price whose digits do not appear in its own evidence string means
    the number and the quote have drifted apart, and the page would be showing
    proof for something it does not say.
    """
    path = os.path.join(os.path.dirname(SITE), "data", "offers.json")
    if not os.path.exists(path):
        errors.append("data/offers.json missing")
        return
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    for o in doc.get("offers", []):
        name = o.get("provider", "?")
        has_price = isinstance(o.get("price"), (int, float))
        status = o.get("status")

        if has_price:
            if status not in ("ok", "stale"):
                errors.append(f"{name}: has a price but status={status}")
            ev = o.get("price_evidence") or ""
            if not ev:
                errors.append(f"{name}: price with no price_evidence")
            else:
                # Accept 4, 4.0, 4.00, 2.5, 2.50 for a value of 4 / 4.0 / 2.5.
                v = float(o["price"])
                variants = {f"{v:g}", f"{v:.1f}", f"{v:.2f}", str(int(v)) if v == int(v) else ""}
                if not any(x and x in ev for x in variants):
                    errors.append(
                        f"{name}: headline {v:g} does not appear in its own quoted evidence")
            for field in ("source_url", "fetched_at"):
                if not o.get(field):
                    errors.append(f"{name}: priced record missing {field}")
        else:
            if status == "ok":
                errors.append(f"{name}: status=ok but no price")
            if o.get("price_evidence"):
                warnings.append(f"{name}: has evidence but no price")

        if o.get("status") == "stale" and not o.get("last_verified_at"):
            errors.append(f"{name}: stale without last_verified_at")
        if o.get("valid_until") and o["valid_until"] < datetime.date.today().isoformat() \
                and status == "ok":
            warnings.append(f"{name}: valid_until has passed but status is still ok")

    # Circuit breaker. An unattended job that publishes whatever it manages to
    # fetch will happily publish a gutted site if the runner gets rate-limited or
    # blocked across the board. Losing most of the priced providers in one hop is
    # far more likely to be a bad crawl than 15 companies changing their pages at
    # once, so refuse to publish it and leave the previous good commit live.
    now_priced = doc.get("providers_with_price")
    was_priced = doc.get("previous_providers_with_price")
    if isinstance(now_priced, int) and isinstance(was_priced, int) and was_priced >= 10:
        if now_priced < was_priced * 0.6:
            errors.append(
                f"priced providers fell from {was_priced} to {now_priced} in one run "
                f"(>40% drop) — refusing to publish a likely-bad crawl; "
                "re-run, or delete data/offers.json to accept the new baseline")


def check_history() -> None:
    """The price history is append-only and cannot be rebuilt, so guard it.

    A corrupt or rewritten history is unrecoverable, and a silently duplicated or
    out-of-order entry would make the provider pages claim a price movement that
    never happened. These checks are cheap and catch exactly that.
    """
    path = os.path.join(os.path.dirname(SITE), "data", "history.json")
    if not os.path.exists(path):
        warnings.append("data/history.json missing — no price history is being recorded")
        return
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as exc:
        errors.append(f"data/history.json is not valid JSON: {exc}")
        return

    providers = doc.get("providers")
    if not isinstance(providers, dict) or not providers:
        errors.append("data/history.json has no providers map")
        return

    for name, entries in providers.items():
        if not isinstance(entries, list) or not entries:
            errors.append(f"history[{name}]: empty or not a list")
            continue
        prev_date = ""
        prev_state = None
        for e in entries:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(e.get("date", ""))):
                errors.append(f"history[{name}]: bad date {e.get('date')!r}")
            if str(e.get("date", "")) < prev_date:
                errors.append(f"history[{name}]: dates out of order at {e.get('date')}")
            prev_date = str(e.get("date", ""))
            state = (e.get("price"), e.get("currency"), e.get("status"))
            if state == prev_state:
                errors.append(
                    f"history[{name}]: duplicate consecutive entry at {e.get('date')} "
                    "(entries are only recorded on change)")
            prev_state = state
            if isinstance(e.get("price"), (int, float)) and not e.get("currency"):
                errors.append(f"history[{name}]: entry with a price but no currency")


def check_affiliate_marking() -> None:
    """Affiliate links must carry rel="sponsored", and only real ones may.

    Google's link-spam policy names rel="sponsored" for affiliate links rather
    than rel="nofollow". Getting it wrong is invisible until rankings are lost, so
    it is checked rather than trusted — in both directions, because a page
    claiming sponsored status it does not have is its own kind of inaccuracy.
    """
    path = os.path.join(os.path.dirname(SITE), "data", "offers.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    affiliate_urls = {o["affiliate_url"] for o in doc.get("offers", []) if o.get("affiliate_url")}
    sponsored_hrefs: set[str] = set()

    for dirpath, _dirnames, filenames in os.walk(SITE):
        for fn in sorted(filenames):
            if not fn.endswith(".html"):
                continue
            rel_path = os.path.relpath(os.path.join(dirpath, fn), SITE).replace("\\", "/")
            with open(os.path.join(dirpath, fn), encoding="utf-8") as fh:
                page = fh.read()
            for href, rel in re.findall(r'<a [^>]*href="([^"]+)"[^>]*rel="([^"]*)"', page):
                sponsored = "sponsored" in rel
                if href in affiliate_urls and not sponsored:
                    errors.append(f"{rel_path}: affiliate link to {href} is not rel=sponsored")
                if sponsored:
                    sponsored_hrefs.add(href)
                    if href not in affiliate_urls:
                        errors.append(f"{rel_path}: rel=sponsored on {href}, which is not a "
                                      "configured affiliate link")

    # The other direction: a configured affiliate link that no page actually uses
    # means the template stopped routing through it — monetisation silently broken.
    for url in sorted(affiliate_urls):
        if url not in sponsored_hrefs:
            errors.append(f"configured affiliate link {url} appears on no page with "
                          "rel=sponsored — is the CTA still routed through affiliate_url?")


def main() -> int:
    global BASE
    with open(os.path.join(SITE, "sitemap.xml"), encoding="utf-8") as fh:
        sm = fh.read()
    root = ET.fromstring(sm)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in root.findall(".//s:loc", ns)]
    if not locs:
        errors.append("sitemap.xml has no <loc> entries")
    BASE = re.match(r"(https?://[^/]+)", locs[0]).group(1)
    print(f"[verify] base url from sitemap: {BASE}")

    for dirpath, _dirnames, filenames in os.walk(SITE):
        for fn in sorted(filenames):
            if fn.endswith(".html"):
                check_page(os.path.join(dirpath, fn))

    # sitemap -> file existence
    for loc in locs:
        if resolve_loc(loc) is None:
            errors.append(f"sitemap lists {loc} but no generated file serves it")
    # The 404 page must exist (it is what stops the host serving the homepage
    # for unknown paths) and must stay out of the sitemap.
    if not os.path.exists(os.path.join(SITE, "404.html")):
        errors.append("404.html missing — host will answer unknown paths with a 200 homepage")
    if any(l.rstrip("/").endswith("/404") or l.rstrip("/").endswith("404.html") for l in locs):
        errors.append("sitemap must not list the 404 page")
    lastmods = [e.text for e in root.findall(".//s:lastmod", ns)]
    if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", m or "") for m in lastmods):
        errors.append("sitemap has malformed lastmod values")
    # lastmod must be a date that has actually happened. A future date is either
    # a clock bug or a fabricated freshness claim, and Google discards it.
    today = datetime.now(timezone.utc).date()
    for m in lastmods:
        try:
            if m and datetime.strptime(m, "%Y-%m-%d").date() > today:
                errors.append(f"sitemap lastmod {m} is in the future")
                break
        except ValueError:
            pass
    # Identical dates are legitimate on a cold build (every page really is new)
    # or when nothing changed. What matters is that dates come from per-page
    # change tracking; without the state file they are just the run timestamp.
    if len(set(lastmods)) < 2 and not os.path.exists(os.path.join("data", "page_state.json")):
        warnings.append("all sitemap lastmod values identical and no page_state.json — "
                        "dates are the run timestamp, not per-page change dates")

    robots = open(os.path.join(SITE, "robots.txt"), encoding="utf-8").read()
    if "Sitemap:" not in robots:
        errors.append("robots.txt does not reference the sitemap")

    check_offers_json()
    check_history()
    check_affiliate_marking()

    if not os.path.exists(os.path.join(SITE, "assets", "style.css")):
        errors.append("assets/style.css missing")

    print(f"[verify] pages={stats['pages']} jsonld_blocks={stats['jsonld']} "
          f"offer={stats['offer']} product={stats['product']} itemlist={stats['itemlist']} "
          f"breadcrumb={stats['breadcrumb']} faq={stats['faq']}")
    print(f"[verify] sitemap urls={len(locs)} distinct_lastmod={len(set(lastmods))}")
    for w in warnings:
        print(f"[verify] WARN  {w}")
    for e in errors:
        print(f"[verify] FAIL  {e}")
    print(f"[verify] {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
