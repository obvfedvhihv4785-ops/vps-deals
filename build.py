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

import hashlib
import html
import json
import os
import re
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ilang  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, ".ilang", "site.ilang")
DATA_PATH = os.path.join(HERE, "data", "offers.json")
TPL_DIR = os.path.join(HERE, "templates")
OUT_DIR = os.path.join(HERE, "site")

# Remembers, per output page, the hash of its last substantive content and the
# date that content last changed. Committed by the workflow, so lastmod survives
# across runs.
STATE_PATH = os.path.join(HERE, "data", "page_state.json")

# Every path written this run. The build does NOT wipe site/ up front: it writes
# the pages it produces and then prunes only what it did not produce. That keeps
# the run idempotent without mass-deleting a directory, and it means a provider
# removed from site.ilang leaves exactly one orphan behind to clean up.
WRITTEN: set[str] = set()

# Strings that change on every run but say nothing about the page's content
# (the build timestamp appears in the footer and in JSON-LD). They are masked
# out before hashing, otherwise every page would look modified every 6 hours and
# the lastmod field would be worthless to a crawler.
_VOLATILE: list[str] = []
_prev_state: dict[str, dict] = {}
_new_state: dict[str, dict] = {}
_NOW_ISO: str = ""

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


def make_url(base: str, rel: str, style: str) -> str:
    """Public URL for a generated file.

    Cloudflare Pages serves `compare.html` at `/compare` and 308-redirects the
    `.html` form. A canonical pointing at a redirect is a wasted signal, so the
    default is the extensionless URL. Set `url_style: html` in site.ilang for a
    host that does not do this.
    """
    rel = rel.lstrip("/")
    if style == "html" or not rel.endswith(".html"):
        return f"{base}/{rel}"
    clean = rel[:-5]
    return f"{base}/" if clean == "index" else f"{base}/{clean}"


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

# Compact labels for the comparison table, keyed on the kind the scraper read
# off the page. Kept short because the column has to stay scannable next to the
# price — the point is that a 24-month rate is visibly not a month-to-month one.
TERM_SHORT_LABEL = {
    "intro": "intro rate",
    "intro_word": "intro offer",
    "annual": "annual billing",
    "ambiguous_terms": "term unclear",
    "ambiguous_toggle": "term unclear",
    "prepay": "paid upfront",
}


def term_short(offer: dict) -> str:
    """One or two words describing the commitment, or "" when there is none.

    A commitment and a promotional period can both apply to the same figure, so
    both are named when both are known.
    """
    kind = offer.get("billing_term_kind", "")
    months = offer.get("billing_term_months") or 0
    label = TERM_SHORT_LABEL.get(kind)
    # Annual billing is its own phrasing. "12-month term" would be true, but the
    # provider's own words are clearer and the number adds nothing a reader
    # cannot infer — CloudCone's "$2.33 /MO Billed $28 per year" reads as annual
    # billing, and calling it a 12-month term makes it sound like a contract
    # rather than a billing frequency.
    if kind == "annual":
        return label or (f"{months}-month term" if months else "")
    parts = []
    if months:
        parts.append(f"{months}-month term")
    if label and (not months or kind in ("intro", "intro_word")):
        parts.append(label)
    return ", ".join(parts)


def renewal_display(offer: dict) -> str:
    """The later price, formatted, or "" when the page states none."""
    value = offer.get("renewal_price")
    if not isinstance(value, (int, float)):
        return ""
    return money(value, offer.get("renewal_currency") or offer.get("currency") or "USD")


def renewal_phrase(offer: dict) -> str:
    """The renewal as one readable sentence fragment, e.g. "renews at $11.99/mo
    for 2 years".

    The period matters as much as the figure: "renews at $11.99/mo" reads like a
    rate that then holds indefinitely, while the page actually says it holds for
    two years. Only the period the page states is appended — never an assumed one.
    """
    shown = renewal_display(offer)
    if not shown:
        return ""
    months = offer.get("renewal_months")
    if isinstance(months, int) and months and months % 12 == 0:
        return f"renews at {shown}/mo for {months // 12} years"
    if isinstance(months, int) and months:
        return f"renews at {shown}/mo for {months} months"
    return f"renews at {shown}/mo"


def _biggest_renewal_jump(priced: list[dict]) -> str:
    """Name the steepest renewal on the site, for the comparison page lede.

    Ratio rather than absolute difference, because the point being made is "the
    headline is not the price" and a $2 → $5 jump makes that point better than a
    $70 → $90 one. Returns "" when no provider states a renewal, so the sentence
    can be omitted rather than printed with a hole in it.
    """
    best, best_ratio = None, 0.0
    for v in priced:
        if not v["has_renewal"]:
            continue
        price = v["price_value"]
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        ratio = v["renewal_value"] / price
        if ratio > best_ratio:
            best, best_ratio = v, ratio
    if not best:
        return ""
    return (f"{best['provider']} (from {best['price_display']} to "
            f"{best['renewal_display']}/mo)")


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
    # Google's link-spam policy asks for rel="sponsored" on affiliate links, not
    # rel="nofollow". Getting this wrong is the kind of thing that quietly costs a
    # site its rankings once monetisation starts, so the value is derived from
    # where the URL actually came from rather than set by hand in a template.
    is_affiliate = bool(offer.get("affiliate_url"))

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
        "cta_is_affiliate": is_affiliate,
        "cta_rel": "sponsored noopener" if is_affiliate else "nofollow noopener",
        "has_price": has_price,
        "show_price": show_price,
        # The rejected-figures table is only rendered when there is something to
        # put in it. The template engine has no `and`, so the two conditions are
        # folded into one flag here rather than spelled out in the template.
        "show_candidates": bool(cands),
        "price_value": offer.get("price") if has_price else None,
        "price_display": money(offer["price"], currency) if show_price else "",
        "price_currency": currency,
        "price_evidence": offer.get("price_evidence", ""),
        "price_tier": offer.get("price_tier", ""),
        "discount_label": (offer.get("discount") or {}).get("label", ""),
        "discount_evidence": (offer.get("discount") or {}).get("evidence", ""),
        "billing_term_kind": offer.get("billing_term_kind", ""),
        "billing_term_months": offer.get("billing_term_months") or 0,
        # A plain commitment carries its fact in the months, not in a note, so
        # the sentence is composed here. Leaving it empty would print
        # "Read the term before the price. — so this is not a month-to-month
        # figure", which reads as a mistake and undermines the claim.
        "billing_term_note": (
            offer.get("billing_term_note")
            or (f"this rate requires a {offer['billing_term_months']}-month commitment"
                if offer.get("billing_term_months") else "")
        ),
        "billing_term_evidence": offer.get("billing_term_evidence", ""),
        "term_short": term_short(offer),
        "has_term_caveat": bool(offer.get("billing_term_kind")),
        "renewal_value": offer.get("renewal_price") if isinstance(
            offer.get("renewal_price"), (int, float)) else None,
        "renewal_display": renewal_display(offer),
        "renewal_phrase": renewal_phrase(offer),
        "renewal_months": offer.get("renewal_months") or 0,
        "renewal_kind": offer.get("renewal_kind", ""),
        "renewal_evidence": offer.get("renewal_evidence", ""),
        "has_renewal": isinstance(offer.get("renewal_price"), (int, float)),
        "renewal_multiple": (
            round(offer["renewal_price"] / offer["price"], 1)
            if isinstance(offer.get("renewal_price"), (int, float))
            and isinstance(offer.get("price"), (int, float)) and offer["price"] > 0
            else ""
        ),
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
        # On a stale record fetched_at is this run's *failed* attempt, so dating
        # the quoted evidence with it would stamp a timestamp on a fetch that
        # never returned the text. price_evidence comes from the last fetch that
        # actually read the page, so the evidence line is dated with that one.
        "evidence_read_display": fmt_date(
            offer.get("last_verified_at") or offer.get("fetched_at")),
        "evidence_read_note": (
            " — the last time this page could be read" if offer.get("stale") else ""),
        # The card badge is a freshness claim, so it is derived from the record's
        # status and not from show_price. A stale price under a green
        # "verified <this run>" badge contradicts the stale label next to it, and
        # across a multi-day outage it would date a days-old number to today.
        "freshness_badge": (
            "stale — last verified " + fmt_date(
                offer.get("last_verified_at") or offer.get("fetched_at"))
            if offer.get("stale") else "verified " + fmt_date(offer.get("fetched_at"))),
        "freshness_badge_kind": "warn" if offer.get("stale") else "ok",
        # The comparison table carries the date in its own "Last verified"
        # column, so the badge there is just the word. It still comes from the
        # status: a bare "verified" next to a failed refresh is the same
        # contradiction as the long form, and this table is where a buyer scans
        # prices.
        "freshness_badge_short": "stale" if offer.get("stale") else "verified",
        "candidates": cands,
        "url": make_url(site["base_url"], f"deal/{slug}.html", site["url_style"]),
        "provider_url": make_url(site["base_url"], f"provider/{slug}.html", site["url_style"]),
        "in_stock": show_price,
    }


def jsonld(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# An Offer node carries `availability: InStock`, which is a claim about the
# present. For a stale record the most recent refresh *failed*, so the node would
# be asserting current availability for a figure we could not re-read. schema.org
# has no "unknown" availability, and dropping the field would break the gate, so
# the qualifier goes in the description — the same reasoning that puts the term
# and the renewal there rather than dropping them.
STALE_NOTE = ("The most recent refresh of that page failed, so this is the last "
              "verified figure rather than a current reading.")


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
            + (f" {STALE_NOTE}" if v["stale"] else "")
            # schema.org has no clean field for a minimum commitment, so the
            # caveat goes in the description rather than being dropped. A price
            # that only holds for 24 months should not read as a monthly rate.
            + (f" This rate applies to a {v['term_short']}."
               if v["term_short"] else "")
            # Same reasoning for the renewal: schema.org cannot express "the
            # price rises later", so the sentence carries it. A structured-data
            # block that quotes the headline and omits the renewal is telling
            # search engines half the offer.
            + (f" The provider states it {v['renewal_phrase']}."
               if v["has_renewal"] else "")
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

    # The offers node asserts InStock, so it carries the read date and the stale
    # qualifier as well. Without that the Product block tells a search engine the
    # plan is available now, with nothing to say the figure could not be re-read.
    offers_note = (f"Read from the provider's own public pricing page on "
                   f"{v['last_verified_display']}."
                   + (f" {STALE_NOTE}" if v["stale"] else ""))

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
            "description": offers_note,
        }
    else:
        node["offers"] = {
            "@type": "Offer",
            "url": v["cta_url"],
            "price": f"{v['price_value']:.2f}",
            "priceCurrency": v["price_currency"],
            "availability": "https://schema.org/InStock",
            "description": offers_note,
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

def _stable_hash(content: str) -> str:
    """Hash a page with run-volatile strings masked out."""
    stable = content
    for token in _VOLATILE:
        if token:
            stable = stable.replace(token, "\x00")
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def write(path: str, content: str) -> str:
    """Write one output file; return the lastmod to publish for it.

    lastmod means "when this page's content last changed", not "when we last
    ran". A full refresh rewrites every page every run, so using the run
    timestamp would make all URLs look modified on every refresh — which teaches
    crawlers to ignore the field. The masked content hash decides instead.
    """
    full = os.path.join(OUT_DIR, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    key = path.replace(os.sep, "/")
    WRITTEN.add(os.path.normcase(os.path.abspath(full)))

    digest = _stable_hash(content)
    prev = _prev_state.get(key) or {}
    if prev.get("sha256_16") == digest and prev.get("lastmod"):
        lastmod = prev["lastmod"]          # unchanged content keeps its old date
    else:
        lastmod = _NOW_ISO
    _new_state[key] = {"sha256_16": digest, "lastmod": lastmod}
    return lastmod


def prune_orphans() -> list[str]:
    """Delete files under site/ that this run did not write.

    This is the only deletion the build performs. It is intentionally narrow:
    a file survives if and only if it was produced above. Returns the relative
    paths removed, so the caller can report them.
    """
    removed: list[str] = []
    if not os.path.isdir(OUT_DIR):
        return removed
    for root, _dirs, files in os.walk(OUT_DIR):
        for name in files:
            full = os.path.abspath(os.path.join(root, name))
            if os.path.normcase(full) in WRITTEN:
                continue
            os.remove(full)
            removed.append(os.path.relpath(full, OUT_DIR).replace(os.sep, "/"))
    # Drop directories left empty by the prune, deepest first.
    for root, dirs, files in os.walk(OUT_DIR, topdown=False):
        if os.path.abspath(root) == os.path.abspath(OUT_DIR):
            continue
        if not os.listdir(root):
            os.rmdir(root)
    return removed


def main() -> int:
    global _NOW_ISO, _VOLATILE, _prev_state

    cfg = ilang.load(CONFIG_PATH)
    settings = cfg.settings()
    render_cfg = cfg.render()

    with open(DATA_PATH, encoding="utf-8") as fh:
        doc = json.load(fh)

    generated_at = doc["generated_at"]
    _NOW_ISO = generated_at

    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                _prev_state = (json.load(fh) or {}).get("pages", {}) or {}
        except (ValueError, OSError):
            _prev_state = {}   # unreadable state is not fatal; everything looks new
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
        # "clean" strips .html so canonicals match what the host actually serves.
        "url_style": settings.get("url_style", "clean"),
    }
    site["url_home"] = make_url(site["base_url"], "index.html", site["url_style"])
    site["url_compare"] = make_url(site["base_url"], "compare.html", site["url_style"])
    site["url_about"] = make_url(site["base_url"], "about.html", site["url_style"])

    # Affiliate status is read from the data, not asserted in prose. A disclosure
    # that says "may contain affiliate links" when there are none is as inaccurate
    # as one that stays silent when there are.
    site["has_affiliate"] = any(o.get("affiliate_url") for o in doc["offers"])

    views = [build_view(o, cfg, site, generated_at) for o in doc["offers"]]

    # Mask the run stamp and every per-record "verified on" date before hashing.
    # Those advance on each refresh without the page's substance changing, so
    # they must not count as a modification.
    _VOLATILE = [generated_at, site["generated_display"]]
    for v in views:
        _VOLATILE += [v.get("last_verified_display", ""), v.get("fetched_display", ""),
                      v.get("evidence_read_display", ""), v.get("freshness_badge", "")]
    _VOLATILE = [t for t in dict.fromkeys(_VOLATILE) if t]

    priced = [v for v in views if v["show_price"]]
    unpriced = [v for v in views if not v["show_price"]]
    priced_sorted = sorted(priced, key=lambda v: (v["price_currency"], v["price_value"]))
    index_order = priced_sorted + unpriced

    # Rank within each currency group, never across them. A single running number
    # over a mixed-currency list would place EUR 5.91 above USD 2 and read as
    # "cheaper", which is false. No exchange rate is applied anywhere in this
    # project, so no cross-currency ordering is claimed either.
    for v in views:
        v["rank_in_currency"] = ""
    _seen_cur: dict[str, int] = {}
    for v in priced_sorted:
        cur = v["price_currency"]
        _seen_cur[cur] = _seen_cur.get(cur, 0) + 1
        v["rank_in_currency"] = _seen_cur[cur]

    # ---- price history -----------------------------------------------------
    # Recorded by scraper.py on every run. It is the only part of this dataset
    # that grows more valuable with time and cannot be reconstructed later.
    hist: dict = {}
    hist_path = os.path.join(HERE, "data", "history.json")
    if os.path.exists(hist_path):
        try:
            with open(hist_path, encoding="utf-8") as fh:
                hist = (json.load(fh) or {}).get("providers", {}) or {}
        except (OSError, ValueError):
            hist = {}

    # ---- same-currency neighbours -----------------------------------------
    # Precomputed as flat strings: the template engine does not walk nested
    # objects, and a "next cheapest" link is a real internal link between pages.
    _by_cur: dict[str, list[dict]] = {}
    for v in priced_sorted:
        _by_cur.setdefault(v["price_currency"], []).append(v)

    for v in views:
        v["currency_group"] = ""
        v["group_size"] = ""
        v["cheaper_name"] = ""
        v["cheaper_url"] = ""
        v["cheaper_price"] = ""
        v["pricier_name"] = ""
        v["pricier_url"] = ""
        v["pricier_price"] = ""
        entries = hist.get(v["provider"], [])
        v["history"] = [
            {"date": e.get("date", ""),
             "display": money(e["price"], e.get("currency", v["price_currency"]))
                        if isinstance(e.get("price"), (int, float)) else "no readable price",
             "status": e.get("status", "")}
            for e in entries
        ]
        v["history_count"] = len(entries)
        v["history_multi"] = len(entries) >= 2
        v["first_seen"] = entries[0].get("date", "") if entries else ""
        v["last_change"] = entries[-1].get("date", "") if entries else ""
        v["changed_since_first"] = (
            len(entries) > 1 and entries[0].get("price") != entries[-1].get("price"))
        _prices = [e["price"] for e in entries if isinstance(e.get("price"), (int, float))]
        v["lowest_seen"] = money(min(_prices), v["price_currency"]) if _prices else ""
        v["highest_seen"] = money(max(_prices), v["price_currency"]) if _prices else ""

    for cur, group in _by_cur.items():
        for i, v in enumerate(group):
            v["currency_group"] = cur
            v["group_size"] = len(group)
            if i > 0:
                p = group[i - 1]
                v["cheaper_name"], v["cheaper_url"] = p["provider"], p["url"]
                v["cheaper_price"] = p["price_display"]
            if i + 1 < len(group):
                n = group[i + 1]
                v["pricier_name"], v["pricier_url"] = n["provider"], n["url"]
                v["pricier_price"] = n["price_display"]

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
        # How many of the published prices are not plain month-to-month. The
        # comparison page states this number outright rather than implying the
        # figures are all alike.
        "with_term_caveat": sum(1 for v in priced if v["has_term_caveat"]),
        "plain_monthly": sum(1 for v in priced if not v["has_term_caveat"]),
        # How many advertised prices are promotional and rise later. This is the
        # number that decides whether the headline is the price at all.
        "with_renewal": sum(1 for v in priced if v["has_renewal"]),
        "biggest_renewal_jump": _biggest_renewal_jump(priced),
    }

    # No bulk wipe: pages are overwritten in place, then anything not written
    # this run is pruned at the end. See prune_orphans().
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
                   jsonld_breadcrumb=(breadcrumb_jsonld([("Home", site["url_home"])])
                                      if on("jsonld_breadcrumb") else ""),
                   page_title=f"{site['name']} — verified VPS prices, updated every "
                              f"{site['update_interval_hours']}h ({site['month']})",
                   page_description=(
                       f"{stats['priced']} VPS providers with machine-verified monthly prices, "
                       f"each traced to the provider's own public pricing page. "
                       f"Refreshed every {site['update_interval_hours']} hours."),
                   canonical=site["url_home"],
                   active="home")
        lm = write("index.html", render_file("index.html", ctx))
        emitted.append((site["url_home"], lm, "1.0", "hourly"))

    # ---- compare -----------------------------------------------------------
    if on("compare"):
        ctx = dict(site=site, stats=stats, offers=index_order, priced=priced_sorted,
                   jsonld_itemlist=(itemlist_jsonld(index_order, f"VPS price comparison — {site['name']}")
                                    if on("jsonld_itemlist") else ""),
                   jsonld_breadcrumb=(breadcrumb_jsonld(
                       [("Home", site["url_home"]), ("Compare", site["url_compare"])])
                       if on("jsonld_breadcrumb") else ""),
                   page_title=f"VPS price comparison — {stats['priced']} providers side by side "
                              f"({site['month']})",
                   page_description=("Side-by-side comparison of the lowest machine-readable VPS "
                                     "price at each tracked provider, with the exact page text each "
                                     "price was taken from."),
                   canonical=site["url_compare"],
                   active="compare")
        lm = write("compare.html", render_file("compare.html", ctx))
        emitted.append((site["url_compare"], lm, "0.9", "hourly"))

    # ---- provider + deal pages --------------------------------------------
    for v in views:
        trail = [("Home", site["url_home"]),
                 ("Providers", site["url_home"] + "#providers"),
                 (v["provider"], v["provider_url"])]
        deal_trail = trail + [("Deal", v["url"])]

        if on("provider"):
            ctx = dict(site=site, stats=stats, o=v,
                       jsonld_product=provider_jsonld(v, site),
                       jsonld_breadcrumb=(breadcrumb_jsonld(trail) if on("jsonld_breadcrumb") else ""),
                       page_title=(f"{v['provider']} VPS pricing — "
                                   f"{v['price_display'] + '/mo' if v['show_price'] else 'no readable price'}"
                                   + (f", {v['term_short']}" if v["term_short"] else "")
                                   + f" ({site['month']}) | {site['name']}"),
                       page_description=(
                           f"{v['provider']} VPS pricing as published on their own site. "
                           + (f"Lowest monthly price found: {v['price_display']}. "
                              if v["show_price"] else "No machine-readable monthly price found. ")
                           + (f"That rate applies to a {v['term_short']}. " if v["term_short"] else "")
                           + (f"The provider states it {v['renewal_phrase']}. "
                              if v["has_renewal"] else "")
                           + f"Last verified {v['last_verified_display']}."),
                       canonical=v["provider_url"],
                       active="")
            lm = write(f"provider/{v['slug']}.html", render_file("provider.html", ctx))
            emitted.append((v["provider_url"], lm, "0.8", "daily"))

        if on("deal"):
            ctx = dict(site=site, stats=stats, o=v,
                       jsonld_offer=(offer_jsonld(v, site) if on("jsonld_offer") else ""),
                       jsonld_breadcrumb=(breadcrumb_jsonld(deal_trail)
                                          if on("jsonld_breadcrumb") else ""),
                       page_title=(
                           f"{v['provider']} VPS deal"
                           + (f" — {v['price_display']}/mo" if v["show_price"] else "")
                           + (f", {v['term_short']}" if v["term_short"] else "")
                           + (f", {v['discount_label']}" if v["discount_label"] else "")
                           + f" ({site['month']}) | {site['name']}"),
                       page_description=(
                           (f"{v['provider']} VPS from {v['price_display']}/month"
                            + (f", {v['discount_label']}" if v["discount_label"] else "")
                            + (f". That rate requires a {v['term_short']}."
                               if v["term_short"] else ". ")
                           if v["show_price"]
                           else f"{v['provider']} VPS pricing could not be read as a number. ")
                           + (f"The provider states it {v['renewal_phrase']}. "
                              if v["has_renewal"] else "")
                           + f"Verified against the provider's own page on {v['last_verified_display']}."),
                       canonical=v["url"],
                       active="")
            lm = write(f"deal/{v['slug']}.html", render_file("deal.html", ctx))
            if v["show_price"]:
                emitted.append((v["url"], lm, "0.7", "daily"))

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
         "a": ("Some outbound links on this site are affiliate links, marked rel=\"sponsored\" "
               "and disclosed in the footer. That never changes which price is shown: the price "
               "comes from the provider's own page and no person chooses it."
               if site["has_affiliate"] else
               "Not currently. No outbound link on this site is an affiliate link — every one "
               "goes straight to the provider's own page. If a provider's published partner "
               "programme ever accepts this site, those links will be marked rel=\"sponsored\" "
               "and disclosed in the footer before they go live. It would never change which "
               "price is shown, because no person chooses the price.")},
    ]
    if on("about"):
        ctx = dict(site=site, stats=stats, faq=faq,
                   jsonld_faq=faq_jsonld(faq),
                   jsonld_breadcrumb=(breadcrumb_jsonld(
                       [("Home", site["url_home"]), ("About", site["url_about"])])
                       if on("jsonld_breadcrumb") else ""),
                   page_title=f"Method & data sources — {site['name']}",
                   page_description=("How every price on this site is collected, what happens when a "
                                     "source cannot be read, and what is deliberately never done."),
                   canonical=site["url_about"],
                   active="about")
        lm = write("about.html", render_file("about.html", ctx))
        emitted.append((site["url_about"], lm, "0.4", "monthly"))

    # ---- 404 ---------------------------------------------------------------
    # Without this file the host answers every unknown path with the homepage and
    # a 200, which is a soft 404: unlimited duplicate URLs, all looking like the
    # index. Publishing a real 404.html makes the host return an actual 404.
    # It is intentionally absent from the sitemap and carries no canonical.
    if on("notfound"):
        ctx = dict(site=site, stats=stats, active="")
        write("404.html", render_file("404.html", ctx))

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

    # ---- prune -------------------------------------------------------------
    # Anything left in site/ that this run did not write is a page for a provider
    # or feature that no longer exists (e.g. a provider removed from site.ilang).
    removed = prune_orphans()

    # Persist content hashes + change dates so the next run can tell "unchanged"
    # from "changed" instead of stamping every URL with the run time. Skipped
    # when nothing moved, so an uneventful refresh does not churn the file (and
    # therefore does not churn the workflow's commit).
    if _new_state != _prev_state:
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"schema": "page-state/1", "updated_at": generated_at,
                           "pages": _new_state}, fh, indent=1, sort_keys=True)
                fh.write("\n")
        except OSError as exc:
            print(f"[build] WARN could not write {STATE_PATH}: {exc}", file=sys.stderr)

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
    changed = sum(1 for k, s in _new_state.items()
                  if (_prev_state.get(k) or {}).get("sha256_16") != s["sha256_16"])
    print(f"[build] content changed on {changed}/{len(_new_state)} page(s) this run")
    if removed:
        print(f"[build] pruned {len(removed)} orphaned file(s): {', '.join(sorted(removed)[:8])}"
              + (" …" if len(removed) > 8 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
