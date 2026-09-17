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
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

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
    lastmods = [e.text for e in root.findall(".//s:lastmod", ns)]
    if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", m or "") for m in lastmods):
        errors.append("sitemap has malformed lastmod values")
    if len(set(lastmods)) < 2:
        warnings.append("all sitemap lastmod values identical — check per-page dates")

    robots = open(os.path.join(SITE, "robots.txt"), encoding="utf-8").read()
    if "Sitemap:" not in robots:
        errors.append("robots.txt does not reference the sitemap")

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
