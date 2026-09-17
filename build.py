# ILANG
# TYPE:module PROJECT:vps-deals-promo-radar LANG:zh
#
# ::STATE{@FILE, role:静态站生成器, input:data/offers.json + .ilang/site.ilang, output:site/}
# ::RULE{品牌 细分 域名 页面开关 全部从 .ilang/site.ilang 读 不硬编码}
# ::RULE{抓不到 price 的条目不许生成 Offer 结构化数据 宁缺勿造}
# ::RULE{每条价格旁边必须能追到 source_url 和 fetched_at}
# ::BOUNDARY{never:编价格 编折扣 编评价 编销量|scope:file}

"""Static site generator for vps-deals-promo-radar.

Reads `data/offers.json` (produced by scraper.py) plus `.ilang/site.ilang`, and
writes a complete static site into `site/`:

    site/index.html                 ranked table of every tracked provider
    site/compare.html               side-by-side price comparison
    site/about.html                 methodology + provenance + FAQ
    site/provider/<slug>.html       provider profile (Product + AggregateOffer)
    site/deal/<slug>.html           current offer (Offer JSON-LD)
    site/sitemap.xml                real per-page lastmod
    site/robots.txt

No dependencies, no API keys, no runtime inference. Pure stdlib.

The one rule this file must never break: it does not invent data. If a price is
absent in offers.json, the page says so; no Offer structured data is emitted and
no number is rendered.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ilang  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, ".ilang", "site.ilang")
DATA_PATH = os.path.join(HERE, "data", "offers.json")
TPL_DIR = os.path.join(HERE, "templates")
OUT_DIR = os.path.join(HERE, "site")

CURRENCY_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£"}


# ---------------------------------------------------------------------------
# Minimal template engine (stdlib only)
#
# Supports exactly what templates/ needs and nothing more:
#   {{ expr }}              escaped output
#   {{{ expr }}}            raw output
#   {% for x in xs %} ... {% endfor %}
#   {% if expr %} ... {% else %} ... {% endif %}
#   {% include "path" %}
#   {# comment #}
# Expressions are dotted lookups ("offer.price_display") optionally prefixed
# with "not ". No arithmetic, no function calls — templates stay dumb.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"(\{\{\{.*?\}\}\}|\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\})", re.S)


class TemplateError(Exception):
    pass


class _Missing:
    """Sentinel for an undefined template value.

    Rendering an undefined value is a build error, not an empty string. A silent
    empty is how a missing context key ships to production unnoticed.
    """

    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


MISSING = _Missing()


def _lookup(ctx: dict, expr: str):
    cur = ctx
    for part in expr.strip().split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def _literal(token: str):
    t = token.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    if t == "true":
        return True
    if t == "false":
        return False
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    return t


def _eval(expr: str, ctx: dict):
    """Evaluate a template condition: bare truthiness, `not x`, `a == b`, `a != b`."""
    expr = expr.strip()
    if expr.startswith("not "):
        return not _eval(expr[4:], ctx)
    for op in ("==", "!="):
        if op in expr:
            left, _, right = expr.partition(op)
            lhs = _lookup(ctx, left)
            if lhs is MISSING:
                raise TemplateError(f"undefined value {left.strip()!r} in condition {expr!r}")
            rhs = _literal(right)
            return (lhs == rhs) if op == "==" else (lhs != rhs)
    val = _lookup(ctx, expr)
    if val is MISSING:
        raise TemplateError(f"undefined value {expr!r} in condition")
    return bool(val)


def _render_nodes(nodes: list, ctx: dict, base: str) -> str:
    out: list[str] = []
    for kind, payload in nodes:
        if kind == "text":
            out.append(payload)
        elif kind == "raw":
            val = _lookup(ctx, payload)
            if val is MISSING:
                raise TemplateError(f"undefined value {{{{{payload.strip()}}}}}")
            out.append("" if val is None else str(val))
        elif kind == "var":
            val = _lookup(ctx, payload)
            if val is MISSING:
                raise TemplateError(f"undefined value {{{{{payload.strip()}}}}}")
            out.append("" if val is None else html.escape(str(val), quote=True))
        elif kind == "if":
            cond, then_nodes, else_nodes = payload
            branch = then_nodes if _eval(cond, ctx) else else_nodes
            out.append(_render_nodes(branch, ctx, base))
        elif kind == "for":
            var, expr, body = payload
            items = _lookup(ctx, expr)
            if items is MISSING:
                raise TemplateError(f"undefined list {expr!r} in for-loop")
            items = items or []
            if isinstance(items, dict):
                items = list(items.values())
            for i, item in enumerate(items):
                sub = dict(ctx)
                sub[var] = item
                sub["loop"] = {"index": i + 1, "first": i == 0, "last": i == len(items) - 1}
                out.append(_render_nodes(body, sub, base))
        elif kind == "include":
            path = os.path.join(TPL_DIR, payload.strip().strip("\"'"))
            with open(path, encoding="utf-8") as fh:
                out.append(render(fh.read(), ctx))
        else:
            raise TemplateError(f"unknown node {kind}")
    return "".join(out)


def _parse(tokens: list[str], pos: int = 0, stop: tuple[str, ...] = ()):
    nodes: list = []
    while pos < len(tokens):
        tok = tokens[pos]
        if tok.startswith("{#"):
            pos += 1
            continue
        if tok.startswith("{{{"):
            nodes.append(("raw", tok[3:-3]))
            pos += 1
        elif tok.startswith("{{"):
            nodes.append(("var", tok[2:-2]))
            pos += 1
        elif tok.startswith("{%"):
            inner = tok[2:-2].strip()
            kw = inner.split(None, 1)[0] if inner else ""
            if kw in stop:
                return nodes, pos
            if kw == "include":
                nodes.append(("include", inner[len("include"):].strip()))
                pos += 1
            elif kw == "if":
                cond = inner[2:].strip()
                then_nodes, pos = _parse(tokens, pos + 1, ("else", "endif"))
                else_nodes: list = []
                if pos < len(tokens) and tokens[pos].startswith("{%") and \
                        tokens[pos][2:-2].strip() == "else":
                    else_nodes, pos = _parse(tokens, pos + 1, ("endif",))
                if not (pos < len(tokens) and tokens[pos].startswith("{%")
                        and tokens[pos][2:-2].strip() == "endif"):
                    raise TemplateError("unclosed {% if %}")
                nodes.append(("if", (cond, then_nodes, else_nodes)))
                pos += 1
            elif kw == "for":
                m = re.match(r"for\s+(\w+)\s+in\s+(.+)$", inner)
                if not m:
                    raise TemplateError(f"bad for-tag: {inner!r}")
                body, pos = _parse(tokens, pos + 1, ("endfor",))
                if not (pos < len(tokens) and tokens[pos][2:-2].strip() == "endfor"):
                    raise TemplateError("unclosed {% for %}")
                nodes.append(("for", (m.group(1), m.group(2).strip(), body)))
                pos += 1
            else:
                raise TemplateError(f"unknown tag: {inner!r}")
        else:
            nodes.append(("text", tok))
            pos += 1
    return nodes, pos


def render(source: str, ctx: dict) -> str:
    tokens = [t for t in _TOKEN_RE.split(source) if t]
    nodes, _ = _parse(tokens)
    return _render_nodes(nodes, ctx, TPL_DIR)


def render_file(name: str, ctx: dict) -> str:
    # Every jsonld_* value on the context becomes one <script> block, so the
    # shared head partial does not need to know which page type it is in.
    ctx["jsonld_blocks"] = [v for k, v in ctx.items()
                            if k.startswith("jsonld_") and isinstance(v, str) and v]
    with open(os.path.join(TPL_DIR, name), encoding="utf-8") as fh:
        return render(fh.read(), ctx)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")


def money(value: float, currency: str) -> str:
    sym = CURRENCY_SYMBOL.get(currency, currency + " ")
    if abs(value - round(value)) < 0.005:
        return f"{sym}{int(round(value))}"
    return f"{sym}{value:.2f}"


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return dt.strftime("%d %b %Y, %H:%M UTC")


def month_label(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return dt.strftime("%B %Y")


STATUS_LABEL = {
    "ok": "verified",
    "stale": "stale — last verified earlier",
    "no_price_in_html": "price not machine-readable",
    "blocked": "source blocked our fetch",
    "blocked_by_robots": "disallowed by robots.txt",
    "error": "fetch error",
}


# ---------------------------------------------------------------------------
# View models
# ---------------------------------------------------------------------------

def build_view(offer: dict, cfg: ilang.SiteConfig, site: dict, generated_at: str) -> dict:
    """Turn one scraped record into the flat dict the templates consume."""
    slug = slugify(offer["provider"])
    has_price = isinstance(offer.get("price"), (int, float))
    currency = offer.get("currency") or site["currency"]
    status = offer.get("status", "error")
    valid_until = offer.get("valid_until")
    expired = bool(valid_until and valid_until < date.today().isoformat())

    # A price we cannot stand behind is treated as absent everywhere: no number
    # rendered, no Offer structured data. Expired offers keep their history but
    # are marked out of stock instead of being presented as live.
    show_price = has_price and not expired

    cta = offer.get("affiliate_url") or offer.get("offer_url") or offer.get("homepage")

    cands = []
    for c in offer.get("price_candidates", [])[:8]:
        cands.append({
            "display": money(c["value"], c["currency"]),
            "tier": c["tier"],
            "context": c["context"],
            "evidence": c["evidence"],
            "eligible": c["tier"] != "no",
        })

    return {
        "provider": offer["provider"],
        "slug": slug,
        "homepage": offer.get("homepage", ""),
        "offer_url": offer.get("offer_url", ""),
        "cta_url": cta,
        "has_price": has_price,
        "show_price": show_price,
        "price_value": offer.get("price") if has_price else None,
        "price_display": money(offer["price"], currency) if show_price else "",
        "price_currency": currency,
        "price_evidence": offer.get("price_evidence", ""),
        "price_tier": offer.get("price_tier", ""),
        "discount_label": (offer.get("discount") or {}).get("label", ""),
        "discount_evidence": (offer.get("discount") or {}).get("evidence", ""),
        "valid_until": valid_until or "",
        "expired": expired,
        "status": status,
        "status_label": STATUS_LABEL.get(status, status),
        "fetch_status": offer.get("fetch_status", ""),
        "stale": bool(offer.get("stale")),
        "note": offer.get("note", ""),
        "page_title": offer.get("title") or offer["provider"],
        "summary": offer.get("summary", ""),
        "fetched_at": offer.get("fetched_at", ""),
        "last_verified_at": offer.get("last_verified_at") or offer.get("fetched_at", ""),
        "last_verified_display": fmt_date(offer.get("last_verified_at") or offer.get("fetched_at")),
        "fetched_display": fmt_date(offer.get("fetched_at")),
        "candidates": cands,
        "url": f"{site['base_url']}/deal/{slug}.html",
        "provider_url": f"{site['base_url']}/provider/{slug}.html",
        "in_stock": show_price,
    }


def jsonld(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def offer_jsonld(v: dict, site: dict) -> str:
    """schema.org Offer for a deal page. Only emitted when we have a real price."""
    if not v["show_price"]:
        return ""
    node: dict = {
        "@context": "https://schema.org",
        "@type": "Offer",
        "name": f"{v['provider']} VPS — {v['price_display']}/month",
        "url": v["url"],
        "price": f"{v['price_value']:.2f}",
        "priceCurrency": v["price_currency"],
        "availability": "https://schema.org/InStock",
        "seller": {"@type": "Organization", "name": v["provider"], "url": v["homepage"]},
        "itemOffered": {
            "@type": "Service",
            "name": f"{v['provider']} VPS hosting",
            "serviceType": "Virtual private server hosting",
            "provider": {"@type": "Organization", "name": v["provider"], "url": v["homepage"]},
        },
        "description": (
            f"Lowest server-rendered monthly price found on {v['provider']}'s public "
            f"VPS page, captured {v['last_verified_display']}."
        ),
    }
    # priceValidUntil is only emitted when the page actually stated a date.
    if v["valid_until"] and not v["expired"]:
        node["priceValidUntil"] = v["valid_until"]
    return jsonld(node)


def provider_jsonld(v: dict, site: dict) -> str:
    """schema.org Product + Offer/AggregateOffer for a provider page."""
    eligible = [c for c in v["candidates"] if c["eligible"]]
    node: dict = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": f"{v['provider']} VPS hosting",
        "url": v["provider_url"],
        "brand": {"@type": "Brand", "name": v["provider"]},
        "category": "Virtual private server hosting",
    }
    if v["summary"]:
        node["description"] = v["summary"]
    if not v["show_price"]:
        return jsonld(node)

    if len(eligible) >= 2:
        vals = [c["display"] for c in eligible]
        node["offers"] = {
            "@type": "AggregateOffer",
            "url": v["cta_url"],
            "priceCurrency": v["price_currency"],
            "lowPrice": f"{v['price_value']:.2f}",
            "highPrice": f"{max(float(re.sub(r'[^0-9.]', '', x)) for x in vals):.2f}",
            "offerCount": len(eligible),
            "availability": "https://schema.org/InStock",
        }
    else:
        node["offers"] = {
            "@type": "Offer",
            "url": v["cta_url"],
            "price": f"{v['price_value']:.2f}",
            "priceCurrency": v["price_currency"],
            "availability": "https://schema.org/InStock",
        }
    return jsonld(node)


def breadcrumb_jsonld(trail: list[tuple[str, str]]) -> str:
    return jsonld({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": url}
            for i, (name, url) in enumerate(trail)
        ],
    })


def itemlist_jsonld(items: list[dict], name: str) -> str:
    return jsonld({
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": name,
        "numberOfItems": len(items),
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": it["provider"], "url": it["url"]}
            for i, it in enumerate(items)
        ],
    })


def faq_jsonld(faq: list[dict]) -> str:
    return jsonld({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q["q"],
             "acceptedAnswer": {"@type": "Answer", "text": q["a"]}}
            for q in faq
        ],
    })


# ---------------------------------------------------------------------------
# Page writers
# ---------------------------------------------------------------------------

def write(path: str, content: str) -> None:
    full = os.path.join(OUT_DIR, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def main() -> int:
    cfg = ilang.load(CONFIG_PATH)
    settings = cfg.settings()
    render_cfg = cfg.render()

    with open(DATA_PATH, encoding="utf-8") as fh:
        doc = json.load(fh)

    generated_at = doc["generated_at"]
    site = {
        "brand": cfg.brand,
        "niche": cfg.niche,
        "domain": cfg.domain,
        "base_url": cfg.base_url,
        "name": settings.get("site_name", cfg.brand),
        "tagline": settings.get("site_tagline", ""),
        "currency": settings.get("currency", "USD"),
        "locale": settings.get("locale", "en-US"),
        "generated_at": generated_at,
        "generated_display": fmt_date(generated_at),
        "month": month_label(generated_at),
        "update_interval_hours": settings.get("update_interval_hours", "6"),
        "config_sha256_16": doc.get("config_sha256_16", ""),
        # Single-locale for v1, so hreflang is off. Flip it in site.ilang when a
        # second language directory is added, otherwise Google sees duplicates.
        "hreflang_enabled": render_cfg.get("hreflang", "false") == "true",
    }

    views = [build_view(o, cfg, site, generated_at) for o in doc["offers"]]
    priced = [v for v in views if v["show_price"]]
    unpriced = [v for v in views if not v["show_price"]]
    priced_sorted = sorted(priced, key=lambda v: (v["price_currency"], v["price_value"]))
    index_order = priced_sorted + unpriced

    # RENDER flags from site.ilang genuinely control what gets emitted. Turning a
    # page type off here removes it from the build and from the sitemap.
    def on(key: str, default: str = "true") -> bool:
        return render_cfg.get(key, default) == "true"

    index_cap = int(settings.get("max_deals_on_index", len(index_order)))

    stats = {
        "tracked": len(views),
        "priced": len(priced),
        "currencies": sorted({v["price_currency"] for v in priced}),
        "sources": len({v["offer_url"] for v in views}),
    }

    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR, exist_ok=True)

    # ---- stylesheet --------------------------------------------------------
    # Copied verbatim (not rendered) so CSS braces are never mistaken for tags.
    css_src = os.path.join(TPL_DIR, "assets", "style.css")
    if os.path.exists(css_src):
        with open(css_src, encoding="utf-8") as fh:
            write("assets/style.css", fh.read())

    # ---- index -------------------------------------------------------------
    index_shown = index_order[:index_cap]
    emitted: list[tuple[str, str, str, str]] = []  # (loc, lastmod, priority, changefreq)
    if on("index"):
        ctx = dict(site=site, stats=stats, offers=index_shown, priced=priced_sorted,
                   jsonld_itemlist=(itemlist_jsonld(index_shown, f"{site['name']} — tracked VPS providers")
                                    if on("jsonld_itemlist") else ""),
                   jsonld_breadcrumb=(breadcrumb_jsonld([("Home", site["base_url"] + "/")])
                                      if on("jsonld_breadcrumb") else ""),
                   page_title=f"{site['name']} — verified VPS prices, updated every "
                              f"{site['update_interval_hours']}h ({site['month']})",
                   page_description=(
                       f"{stats['priced']} VPS providers with machine-verified monthly prices, "
                       f"each traced to the provider's own public pricing page. "
                       f"Refreshed every {site['update_interval_hours']} hours."),
                   canonical=f"{site['base_url']}/",
                   active="home")
        write("index.html", render_file("index.html", ctx))
        emitted.append((f"{site['base_url']}/", generated_at, "1.0", "hourly"))

    # ---- compare -----------------------------------------------------------
    if on("compare"):
        ctx = dict(site=site, stats=stats, offers=index_order, priced=priced_sorted,
                   jsonld_itemlist=(itemlist_jsonld(index_order, f"VPS price comparison — {site['name']}")
                                    if on("jsonld_itemlist") else ""),
                   jsonld_breadcrumb=(breadcrumb_jsonld(
                       [("Home", site["base_url"] + "/"), ("Compare", site["base_url"] + "/compare.html")])
                       if on("jsonld_breadcrumb") else ""),
                   page_title=f"VPS price comparison — {stats['priced']} providers side by side "
                              f"({site['month']})",
                   page_description=("Side-by-side comparison of the lowest machine-readable VPS "
                                     "price at each tracked provider, with the exact page text each "
                                     "price was taken from."),
                   canonical=f"{site['base_url']}/compare.html",
                   active="compare")
        write("compare.html", render_file("compare.html", ctx))
        emitted.append((f"{site['base_url']}/compare.html", generated_at, "0.9", "hourly"))

    # ---- provider + deal pages --------------------------------------------
    for v in views:
        trail = [("Home", site["base_url"] + "/"),
                 ("Providers", site["base_url"] + "/#providers"),
                 (v["provider"], v["provider_url"])]
        deal_trail = trail + [("Deal", v["url"])]
        lastmod = v["last_verified_at"] or generated_at

        if on("provider"):
            ctx = dict(site=site, stats=stats, o=v,
                       jsonld_product=provider_jsonld(v, site),
                       jsonld_breadcrumb=(breadcrumb_jsonld(trail) if on("jsonld_breadcrumb") else ""),
                       page_title=(f"{v['provider']} VPS pricing — "
                                   f"{v['price_display'] + '/mo' if v['show_price'] else 'no readable price'}"
                                   f" ({site['month']}) | {site['name']}"),
                       page_description=(
                           f"{v['provider']} VPS pricing as published on their own site. "
                           + (f"Lowest monthly price found: {v['price_display']}. "
                              if v["show_price"] else "No machine-readable monthly price found. ")
                           + f"Last verified {v['last_verified_display']}."),
                       canonical=v["provider_url"],
                       active="")
            write(f"provider/{v['slug']}.html", render_file("provider.html", ctx))
            emitted.append((v["provider_url"], lastmod, "0.8", "daily"))

        if on("deal"):
            ctx = dict(site=site, stats=stats, o=v,
                       jsonld_offer=(offer_jsonld(v, site) if on("jsonld_offer") else ""),
                       jsonld_breadcrumb=(breadcrumb_jsonld(deal_trail)
                                          if on("jsonld_breadcrumb") else ""),
                       page_title=(
                           f"{v['provider']} VPS deal"
                           + (f" — {v['price_display']}/mo" if v["show_price"] else "")
                           + (f", {v['discount_label']}" if v["discount_label"] else "")
                           + f" ({site['month']}) | {site['name']}"),
                       page_description=(
                           (f"{v['provider']} VPS from {v['price_display']}/month"
                            + (f", {v['discount_label']}" if v["discount_label"] else "")
                            + ". " if v["show_price"]
                            else f"{v['provider']} VPS pricing could not be read as a number. ")
                           + f"Verified against the provider's own page on {v['last_verified_display']}."),
                       canonical=v["url"],
                       active="")
            write(f"deal/{v['slug']}.html", render_file("deal.html", ctx))
            if v["show_price"]:
                emitted.append((v["url"], lastmod, "0.7", "daily"))

    # ---- about -------------------------------------------------------------
    faq = [
        {"q": "Where do these prices come from?",
         "a": "Every price is read from the provider's own public pricing or VPS page at build "
              "time. Each one is shown next to the exact sentence it was taken from, and the "
              "page it came from is linked. Nothing is typed in by hand."},
        {"q": "Why do some providers show no price?",
         "a": "Some pages render their prices with client-side JavaScript, and some refuse "
              "automated requests. In those cases no number is published at all, because a "
              "guessed or remembered price would be worse than none."},
        {"q": "How often is this page updated?",
         "a": f"The pipeline runs every {site['update_interval_hours']} hours on a scheduled "
              "job. Each run re-reads every source page and commits whatever changed."},
        {"q": "What does 'stale' mean?",
         "a": "It means a refresh attempt failed for that provider, so the last successfully "
              "verified price is still shown together with the date it was verified. It is "
              "never presented as current."},
        {"q": "Do you get paid for the links?",
         "a": "Some outbound links may become affiliate links once a provider's published "
              "programme accepts this site. That never changes which price is shown, and the "
              "price always comes from the provider's own page."},
    ]
    if on("about"):
        ctx = dict(site=site, stats=stats, faq=faq,
                   jsonld_faq=faq_jsonld(faq),
                   jsonld_breadcrumb=(breadcrumb_jsonld(
                       [("Home", site["base_url"] + "/"), ("About", site["base_url"] + "/about.html")])
                       if on("jsonld_breadcrumb") else ""),
                   page_title=f"Method & data sources — {site['name']}",
                   page_description=("How every price on this site is collected, what happens when a "
                                     "source cannot be read, and what is deliberately never done."),
                   canonical=f"{site['base_url']}/about.html",
                   active="about")
        write("about.html", render_file("about.html", ctx))
        emitted.append((f"{site['base_url']}/about.html", generated_at, "0.4", "monthly"))

    # ---- sitemap + robots --------------------------------------------------
    # The sitemap is derived from what was actually written, so a page type
    # switched off in site.ilang never leaves a dead URL behind.
    if on("sitemap"):
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for loc, lastmod, prio, freq in emitted:
            lines += ["  <url>", f"    <loc>{html.escape(loc)}</loc>",
                      f"    <lastmod>{lastmod[:10]}</lastmod>",
                      f"    <changefreq>{freq}</changefreq>",
                      f"    <priority>{prio}</priority>", "  </url>"]
        lines.append("</urlset>")
        write("sitemap.xml", "\n".join(lines) + "\n")

    if on("robots"):
        write("robots.txt",
              "User-agent: *\n"
              "Allow: /\n"
              f"Sitemap: {site['base_url']}/sitemap.xml\n")

    # ---- report ------------------------------------------------------------
    print(f"[build] brand={site['brand']!r} domain={site['domain']!r}")
    print(f"[build] pages: index={on('index')} compare={on('compare')} about={on('about')} "
          f"provider={on('provider')} deal={on('deal')} -> {len(emitted)} urls in sitemap")
    print(f"[build] priced {stats['priced']}/{stats['tracked']} "
          f"({', '.join(stats['currencies']) or 'none'})")
    for v in index_shown:
        flag = v["price_display"] if v["show_price"] else "—"
        print(f"[build]   {v['provider']:<20} {flag:<9} {v['status_label']}")
    print(f"[build] wrote {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
