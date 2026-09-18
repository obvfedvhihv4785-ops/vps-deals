# ILANG
# TYPE:module PROJECT:vps-deals-promo-radar LANG:zh
#
# ::STATE{@FILE, role:抓取器, input:.ilang/site.ilang, output:data/offers.json}
# ::RULE{厂商清单和参数只从 .ilang/site.ilang 读 本文件不写第二份硬编码清单}
# ::RULE{抓不到 price 就不写 price 字段 不许用估算值填}
# ::RULE{遵守 robots.txt 不绕反爬 不抓登录后内容 只抓公开官方页}
# ::BOUNDARY{never:编优惠 编价格 编折扣|scope:file}

"""Scraper for vps-deals-promo-radar.

Reads the provider list, field list and run parameters from `.ilang/site.ilang`
(via ilang.py) and writes `data/offers.json`.

Design rules that this file must not break:
  * No price is ever invented. If the page does not expose a machine-readable
    monthly price, the record simply has no `price` key.
  * Every price carries `price_evidence` (the literal text it was matched from)
    and `source_url` + `fetched_at`, so any number on the site is traceable.
  * A fetch failure is recorded as a failure. It is never rendered as "no deal".
  * robots.txt is honoured. No anti-bot circumvention, no logged-in content.

Runtime cost: zero. Stdlib only. No LLM, no API keys.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ilang  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, ".ilang", "site.ilang")
OUT_PATH = os.path.join(HERE, "data", "offers.json")
HISTORY_PATH = os.path.join(HERE, "data", "history.json")

# ---------------------------------------------------------------------------
# Extraction patterns
# ---------------------------------------------------------------------------

# "$4.00/mo", "US$ 12 / month", "$6.99 per month", "$5 a month"
PRICE_MONTHLY_RE = re.compile(
    r"(?P<sym>US\$|\$|€)\s?(?P<amt>\d{1,4}(?:,\d{3})*(?:[.,]\d{1,2})?)(?![.,]?\d)"
    r"\s*(?:/|per\s+|a\s+|each\s+)?(?:mo\b|month\b|monthly\b|mos\b)",
    re.I,
)
# "starting at $4.00" / "from $3.50" / "as low as $2.99" (no /mo suffix).
#
# `(?![.,]?\d)` stops the amount from backtracking into a *partial* number:
# without it, "€1.78/hour" backtracks to "1.7" (the "/hour" guard is then past
# the "8"), and "From $1/year" is a real string on these pages but is NOT a
# plan price. The negative lookahead rejects non-monthly billing units.
_NON_MONTHLY = r"(?!\s*(?:/|per\s+|a\s+|each\s+)?(?:hour|hr|day|week|year|yr|annum|minute)\b)"
PRICE_FROM_RE = re.compile(
    r"(?:starting\s+(?:at|from)|from|as\s+low\s+as|only)\s+(?P<sym>US\$|\$|€)\s?"
    r"(?P<amt>\d{1,4}(?:,\d{3})*(?:[.,]\d{1,2})?)(?![.,]?\d)" + _NON_MONTHLY,
    re.I,
)
# Lead-in text that means the following price is an accessory, not a VPS plan.
# Matched against the text BEFORE the price only — text after the price is
# usually a feature list ("...Daily backups included") and must not disqualify.
#
# Note: "per hour" / "per year" are deliberately NOT here. Non-monthly billing
# units are already rejected by the _NON_MONTHLY lookahead on the price itself,
# and a *neighbouring* price's unit ("...€0.010 per hour or monthly starting at
# €5.91") must not poison an otherwise valid monthly plan price.
ACCESSORY_LEAD_RE = re.compile(
    r"backup|add-?on|additional|minimum|licens|per\s+gb|inbox|floating\s+ip"
    r"|ip\s+address|ipv4\s+address|snapshot|money-?\s?back|cpanel|plesk",
    re.I,
)
# A price only counts as a plan price when the surrounding text is talking about
# a server/plan at all.
PLAN_CONTEXT_RE = re.compile(
    r"vps|vcpu|vcore|cpu|core|ram|memory|droplet|instance|cloud\s+server"
    r"|server|plan|hosting",
    re.I,
)
# The same idea applied AFTER the price. Some pages put the unit on the right:
# "Price: $1.00 /month Storage: 10 GB" is an object-storage rate, not a VPS plan,
# and the lead-in gives no hint at all.
ACCESSORY_AFTER_RE = re.compile(
    r"storage|backup|snapshot|per\s+gb|/\s?gb\b|egress",
    re.I,
)
# Marketing prose about the market rather than a price this provider charges:
# "VPS hosting costs vary widely, starting from around $4/month".
MARKET_PROSE_RE = re.compile(
    r"varies|costs\s+vary|price\s+varies|starting\s+from\s+around|on\s+average"
    r"|typically|generally\s+(?:cost|start)",
    re.I,
)
# A title matching this means the page is a third-party ranking, so every price
# on it belongs to some other company and must not be attributed to this one.
THIRD_PARTY_TITLE_RE = re.compile(
    r"rankings?\b|top\s+\d+\s|best\s+[\w\s]{0,20}\bproviders\b", re.I
)
ACCESSORY_WINDOW = 45
ACCESSORY_AFTER_WINDOW = 50
PLAN_WINDOW = 90
MARKET_WINDOW = 120
DISCOUNT_RE = re.compile(
    r"(?P<pct>\d{1,3})\s?%\s?(?:off|discount|savings|cheaper)", re.I)
SAVE_RE = re.compile(r"save\s+(?P<sym>US\$|\$|€)\s?(?P<amt>\d{1,4}(?:\.\d{1,2})?)", re.I)
EXPIRES_RE = re.compile(
    r"(?:expires?|ends?|valid\s+until|valid\s+through|offer\s+ends)\s*(?:on|:)?\s*"
    r"(?P<date>[A-Z][a-z]{2,8}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4})",
    re.I,
)

# How long a buyer must commit before the advertised rate applies.
#
# This exists because "/mo" is not one thing. HostGator's "$2.09 /mo" is the
# 24-month prepay rate and InMotion's "$9.99 /mo" is likewise "For 24 month",
# while Vultr's "$2.50 /mo" is genuinely month to month. Publishing both as
# plain per-month figures makes the comparison table wrong: a reader comparing
# 2.09 against 2.50 would be choosing between a two-year commitment and no
# commitment at all. The term is already stated next to the number on the page,
# so it is extracted and shown rather than dropped.
TERM_EXPLICIT_RE = re.compile(
    r"(?:for|on|over|with|after|requires?|across)\s+(?:the\s+|a\s+)?"
    r"(?P<n>\d{1,2})\s*[-\s]?\s*(?:months?|mos?)\b",
    re.I,
)
TERM_AFTER_RE = re.compile(
    r"(?P<n>\d{1,2})\s*[-\s]?\s*(?:months?|mos?)\s*"
    r"(?:term|commitment|contract|billing|plan|period|upfront|prepay)",
    re.I,
)
# "First 3 months at $8.99/mo" is not a price, it is a temporary rate. Saying
# so is the difference between a useful listing and a bait price.
#
# The phrase must sit immediately before the figure, joined to it by nothing
# more than a price connector ("at", "for", "only"). A looser search would also
# match "free SSL for the first 12 months. Only $4.50/mo" and then label an
# ordinary price as introductory, which is a fabricated claim about money —
# worse than the omission it was meant to fix.
INTRO_NEAR_RE = re.compile(
    r"\b(?:first|initial|introductory|intro)\s+(?P<n>\d{1,2})\s*[-\s]?\s*months?\b"
    r"\s*(?:at|for|of|only|just|:)?\s*$",
    re.I,
)
INTRO_WORD_NEAR_RE = re.compile(r"\bintro(?:ductory)?\s+(?:offer|price|rate|pricing)\b.{0,25}$", re.I)
# "Snapshots retained for the first 6 months" is a duration attached to a
# feature, not to the price that happens to follow it. When one of these verbs
# governs the clause, the "first N months" belongs to whatever that verb is
# about, so the price is left alone. Without this guard the phrase immediately
# before a figure is indistinguishable from a real introductory rate, and
# labelling a plain price "introductory" is a fabricated claim about money.
INTRO_FEATURE_VERB_RE = re.compile(
    r"\b(?:retain(?:ed)?|kept|stored|included|includes|available|valid|free|provided|"
    r"accessible|supported|protected|backed\s+up|deleted|expires?|renew(?:ed)?|"
    r"released|issued|granted)\b",
    re.I,
)
# "…/month for 3 months with a 1-year term" — IONOS states the promotional
# period directly after the figure rather than as "first N months" before it.
# Anchored to the start of the after-window so it can only match text that
# begins right at the price.
#
# The trailing lookahead matters: "For 24 month term" also starts with "for N
# months" but is a commitment, not a promotional period, and reading it as one
# would label a two-year contract an introductory rate.
INTRO_AFTER_RE = re.compile(
    r"^\s*(?:/\s*mo(?:nth)?)?\s*for\s+(?P<n>\d{1,2})\s*[-\s]?\s*months?\b"
    r"(?!\s*(?:term|commitment|contract|billing|plan|period))",
    re.I,
)
# A commitment can be stated in years ("a 1-year term"), which is the same fact
# as 12 months and belongs in the same field.
YEAR_TERM_RE = re.compile(
    r"(?P<n>\d{1,2})\s*[-\s]?\s*years?\s*(?:term|commitment|contract|billing|plan|period)",
    re.I,
)
ANNUAL_RE = re.compile(r"billed\s+(?:annually|yearly|per\s+year)|\bper\s+year\b|/\s*yr\b", re.I)
# ScalaHosting renders term toggles as "36 M 12 M 1 M" with one price beside
# them. Which toggle the price belongs to is not in the static HTML, so the
# honest result is "term not stated", not a guess at one of the three.
TERM_TOGGLE_RE = re.compile(r"\b\d{1,2}\s*M\b(?=\s+\d{1,2}\s*M\b)", re.I)
# Some providers state the billing rule once for the whole page instead of beside
# each price. Hostinger says: "All plans are paid upfront. The monthly rate
# reflects the total plan price divided by the number of months in your plan."
# That single sentence means every "/mo" figure on the page is an amortised
# prepay, not a monthly charge — so a term column reading "month to month"
# there would be flatly wrong.
#
# Both halves are required. "Paid upfront" alone can describe a setup fee, and
# the amortisation sentence alone can describe one plan rather than the page.
UPFRONT_PAID_RE = re.compile(r"\bpaid\s+upfront\b", re.I)
UPFRONT_AMORTISE_RE = re.compile(
    r"monthly\s+rate\s+reflects\s+the\s+total\s+plan\s+price\s+divided\s+by", re.I)

# What the figure becomes after the promotional period.
#
# This is arguably the most important number after the advertised one. HostGator
# advertises "$2.09 /mo" and states two words later that it "Renews at $4.68
# /mo" — more than double. Verpex goes from $10 to $19.99. Nine of the
# twenty-two tracked providers state a renewal price next to the headline, and a
# deals page that shows only the introductory figure is showing half the offer.
RENEWAL_AFTER_RE = re.compile(
    r"(?:auto[-\s]?)?renews?\s+at\s+(?P<sym>US\$|\$|€)\s?(?P<amt>\d{1,4}(?:,\d{3})*(?:[.,]\d{1,2})?)",
    re.I,
)
RENEWAL_BEFORE_RE = re.compile(
    r"(?P<sym>US\$|\$|€)\s?(?P<amt>\d{1,4}(?:,\d{3})*(?:[.,]\d{1,2})?)"
    r"\s*(?:/\s*mo(?:nth)?)?\s*(?:when you renew|at renewal|on renewal|upon renewal)",
    re.I,
)
REGULAR_PRICE_RE = re.compile(
    r"(?:regular|list|standard)\s+price\s*(?::|of|is)?\s*"
    r"(?P<sym>US\$|\$|€)\s?(?P<amt>\d{1,4}(?:,\d{3})*(?:[.,]\d{1,2})?)",
    re.I,
)
# Wide enough to reach the renewal sentence in every case observed ("Renews at"
# lands 20-60 characters after the figure), but bounded, and guarded below
# against reaching past another plan's price.
RENEWAL_AFTER_WINDOW = 90
RENEWAL_BEFORE_WINDOW = 70
ANY_MONEY_RE = re.compile(r"(?:US\$|\$|€)\s?\d", re.I)
# Hostinger states the renewal *period* as well as the figure: "Renews at
# $11.99/mo for 2 years." That "for 2 years" is part of the same sentence and
# belongs in the same disclosure — dropping it would make the renewal look like
# a rate that holds indefinitely. Anchored immediately after the figure so it
# cannot pick up a duration from elsewhere in the sentence.
RENEWAL_TERM_RE = re.compile(
    r"^\s*(?:/\s*mo(?:nth)?)?\s*(?:for|over)\s+(?P<n>\d{1,2})\s*[-\s]?\s*"
    r"(?P<unit>months?|mos?|years?|yrs?)",
    re.I,
)

# The term sits immediately beside the figure in every case observed ("$2.09
# /mo For 24 month", "on the 12-month", "Billed $28 per year"), so the search is
# tight. A wide window would start attributing a term mentioned elsewhere on the
# page to the wrong price, which is worse than saying nothing.
BEFORE_WINDOW = 45
AFTER_WINDOW = 45

SYM_TO_CURRENCY = {"$": "USD", "US$": "USD", "€": "EUR"}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def http_get(url: str, ua: str, timeout: int) -> tuple[int, str, dict]:
    """GET a URL. Returns (status, body_text, headers). Raises on transport error."""
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "From": "promo-radar-bot",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        ctype = resp.headers.get("Content-Type", "") or ""
        m = re.search(r"charset=([\w-]+)", ctype)
        cs = m.group(1) if m else "utf-8"
        try:
            text = raw.decode(cs, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        return resp.status, text, dict(resp.headers)


class RobotsCache:
    """Minimal robots.txt checker. Fails open only when robots.txt is absent (404)."""

    def __init__(self, ua: str, timeout: int, enabled: bool):
        self.ua = ua
        self.timeout = timeout
        self.enabled = enabled
        self._cache: dict[str, tuple[list[str], list[str]]] = {}

    def _load(self, root: str) -> tuple[list[str], list[str]]:
        if root in self._cache:
            return self._cache[root]
        disallow: list[str] = []
        allow: list[str] = []
        try:
            status, text, _ = http_get(root + "/robots.txt", self.ua, self.timeout)
            if status == 200:
                active = False
                for line in text.splitlines():
                    line = line.split("#", 1)[0].strip()
                    if not line or ":" not in line:
                        continue
                    field, _, value = line.partition(":")
                    field = field.strip().lower()
                    value = value.strip()
                    if field == "user-agent":
                        active = value == "*" or value.lower() in self.ua.lower()
                    elif active and field == "disallow" and value:
                        disallow.append(value)
                    elif active and field == "allow" and value:
                        allow.append(value)
        except Exception:
            # robots.txt unreachable -> treat as "no rules published"
            pass
        self._cache[root] = (disallow, allow)
        return disallow, allow

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        parsed = urllib.parse.urlsplit(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        disallow, allow = self._load(root)
        path = parsed.path or "/"
        # Longest matching rule wins; Allow beats Disallow on equal length.
        best_d = max((d for d in disallow if path.startswith(d)), key=len, default=None)
        best_a = max((a for a in allow if path.startswith(a)), key=len, default=None)
        if best_d is None:
            return True
        if best_a is not None and len(best_a) >= len(best_d):
            return True
        return False


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def strip_to_visible_text(html: str) -> str:
    """Remove script/style/svg/noscript, then tags, then collapse whitespace."""
    for tag in ("script", "style", "noscript", "svg", "template", "iframe"):
        html = re.sub(rf"<{tag}\b[\s\S]*?</{tag}>", " ", html, flags=re.I)
    html = re.sub(r"<!--[\s\S]*?-->", " ", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&#39;", "'").replace("&quot;", '"')
                .replace("&euro;", "€").replace("&pound;", "£"))
    return re.sub(r"\s+", " ", text).strip()


def _to_float(amt: str) -> float | None:
    """Parse '1,299.00' or '1299,00' or '4.54' into a float."""
    a = amt.strip()
    if "," in a and "." in a:
        a = a.replace(",", "") if a.rindex(".") > a.rindex(",") else a.replace(".", "").replace(",", ".")
    elif "," in a:
        head, _, tail = a.rpartition(",")
        a = f"{head}.{tail}" if len(tail) in (1, 2) and head else a.replace(",", "")
    try:
        return float(a)
    except ValueError:
        return None


def _snippet(text: str, start: int, end: int, pad: int = 55) -> str:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    return ("…" if lo > 0 else "") + text[lo:hi].strip() + ("…" if hi < len(text) else "")


def classify_candidate(text: str, start: int) -> tuple[str, str]:
    """Decide whether a price at `start` is a VPS plan price or something else.

    Returns (tier, reason) where tier is one of:
      "plan"  — no accessory marker before it, and server/plan words around it
      "weak"  — no accessory marker, but no plan words either (usable, flagged)
      "no"    — an accessory lead-in such as "backups", "minimum", "additional IP"
    """
    lead = text[max(0, start - ACCESSORY_WINDOW):start]
    m = ACCESSORY_LEAD_RE.search(lead)
    if m:
        return "no", f"accessory lead-in {m.group(0)!r}"
    after = text[start:start + ACCESSORY_AFTER_WINDOW]
    m = ACCESSORY_AFTER_RE.search(after)
    if m:
        return "no", f"accessory unit after the price ({m.group(0)!r})"
    around = text[max(0, start - MARKET_WINDOW):start + MARKET_WINDOW]
    m = MARKET_PROSE_RE.search(around)
    if m:
        return "no", f"market commentary, not a price they charge ({m.group(0)!r})"
    if PLAN_CONTEXT_RE.search(around):
        return "plan", "server/plan wording nearby"
    return "weak", "no accessory marker, but no server/plan wording nearby"


def extract_prices(text: str, min_p: float, max_p: float) -> list[dict]:
    """Collect monthly prices, each with the literal evidence text it came from."""
    found: dict[tuple[str, float], dict] = {}
    for regex, ctx in ((PRICE_MONTHLY_RE, "monthly"), (PRICE_FROM_RE, "from")):
        for m in regex.finditer(text):
            sym = m.group("sym")
            currency = SYM_TO_CURRENCY.get(sym if sym in SYM_TO_CURRENCY else sym.upper(), "USD")
            value = _to_float(m.group("amt"))
            if value is None or not (min_p <= value <= max_p):
                continue
            tier, reason = classify_candidate(text, m.start())
            key = (currency, value)
            rank = ({"plan": 2, "weak": 1, "no": 0}[tier], ctx == "monthly")
            prev = found.get(key)
            if prev and prev["_rank"] >= rank:
                continue
            found[key] = {
                "currency": currency,
                "value": value,
                "context": ctx,
                "tier": tier,
                "eligible": tier != "no",
                "reason": reason,
                "evidence": _snippet(text, m.start(), m.end()),
                # Offsets are kept so the billing term can be read from the same
                # spot in the page rather than from the truncated snippet. They
                # are dropped again before the record is written.
                "start": m.start(),
                "end": m.end(),
                "_rank": rank,
            }
    rows = sorted(found.values(), key=lambda r: r["value"])
    for r in rows:
        r.pop("_rank", None)
    return rows


def pick_headline(prices: list[dict], site_currency: str) -> dict | None:
    """Cheapest price that survived classification, preferring the site currency.

    Prefers "plan"-tier candidates; only falls back to "weak"-tier ones when no
    plan-tier candidate exists, so a weak match is never presented as a strong one.
    """
    for tier in ("plan", "weak"):
        pool = [p for p in prices if p["tier"] == tier]
        if not pool:
            continue
        same_ccy = [p for p in pool if p["currency"] == site_currency]
        return (same_ccy or pool)[0]
    return None


def extract_discount(text: str) -> dict | None:
    m = DISCOUNT_RE.search(text)
    if m:
        pct = int(m.group("pct"))
        if 1 <= pct <= 95:
            return {"kind": "percent", "value": pct, "label": f"{pct}% off",
                    "evidence": _snippet(text, m.start(), m.end())}
    m = SAVE_RE.search(text)
    if m:
        value = _to_float(m.group("amt"))
        if value:
            cur = SYM_TO_CURRENCY.get(m.group("sym"), "USD")
            return {"kind": "amount", "value": value, "currency": cur,
                    "label": f"save {m.group('sym')}{value:g}",
                    "evidence": _snippet(text, m.start(), m.end())}
    return None


def extract_billing_term(text: str, start: int, end: int) -> dict:
    """Read how long a buyer must commit for the quoted rate to apply.

    Returns {"kind": str, "months": int | None, "note": str}. `months` is only
    set when the page names exactly one term, because a page showing several
    terms beside a single price does not say which of them that price belongs to
    — guessing one would be inventing a fact. `note` carries what can honestly
    be said instead: an introductory rate, an ambiguous term list, annual
    billing, or a page-wide upfront-prepay rule.

    A commitment and a promotional period can both be true at once — IONOS sells
    "$2 /month for 3 months with a 1-year term" — so months and note are
    independent rather than alternatives.

    Both windows are deliberately short. A term read from far away on the page
    might describe a different plan, and a wrong term is a fabricated claim
    about money — worse than the silence it would be replacing.
    """
    before = text[max(0, start - BEFORE_WINDOW):start]
    after = text[end:end + AFTER_WINDOW]

    # Work out the promotional period first, then mask it out of the text the
    # term patterns see. "for 3 months" is a duration, not a commitment, and
    # reading it as one would contradict the "1-year term" in the same sentence.
    intro_n = None
    after_for_terms = after
    intro = INTRO_NEAR_RE.search(before)
    if intro and not INTRO_FEATURE_VERB_RE.search(before[:intro.start()]):
        intro_n = int(intro.group("n"))
    else:
        intro = INTRO_AFTER_RE.search(after)
        if intro:
            intro_n = int(intro.group("n"))
            after_for_terms = after[:intro.start()] + " " + after[intro.end():]

    terms: set[int] = set()
    for chunk in (after_for_terms, before):
        for rx in (TERM_EXPLICIT_RE, TERM_AFTER_RE, YEAR_TERM_RE):
            for m in rx.finditer(chunk):
                n = int(m.group("n"))
                if rx is YEAR_TERM_RE:
                    n *= 12
                if 1 <= n <= 60:
                    terms.add(n)

    months = None
    ambiguous = None
    if len(terms) > 1:
        listed = ", ".join(f"{n}-month" for n in sorted(terms))
        ambiguous = (f"the page lists several terms ({listed}) without saying which one "
                     "the advertised rate applies to")
    elif len(terms) == 1:
        months = terms.pop()

    if intro_n:
        return {"kind": "intro", "months": months,
                "note": f"introductory rate for the first {intro_n} months only"}
    if ambiguous:
        return {"kind": "ambiguous_terms", "months": None, "note": ambiguous}
    if months:
        return {"kind": "term", "months": months, "note": ""}
    if ANNUAL_RE.search(after) or ANNUAL_RE.search(before):
        return {"kind": "annual", "months": 12, "note": "billed annually"}
    if TERM_TOGGLE_RE.search(before):
        return {"kind": "ambiguous_toggle", "months": None,
                "note": "the page shows term toggles beside one price and does not "
                        "state which term that price applies to"}
    if INTRO_WORD_NEAR_RE.search(before):
        return {"kind": "intro_word", "months": None,
                "note": "advertised as an introductory offer; the ongoing rate is higher"}
    # A page-wide billing rule. This is read from the whole page rather than the
    # price window because that is where the provider chose to say it, and it is
    # the difference between "month to month" and "a prepay divided by its
    # length" — the exact distinction this column exists to make.
    if UPFRONT_PAID_RE.search(text) and UPFRONT_AMORTISE_RE.search(text):
        # The sentence is captured rather than paraphrased, because this claim
        # applies to every price on the page and a reader is entitled to see the
        # words it rests on. It is page-level, so it cannot live in
        # price_evidence, which belongs to one figure.
        m = UPFRONT_PAID_RE.search(text)
        return {"kind": "prepay", "months": None,
                "note": "the provider states all plans are paid upfront and the monthly "
                        "rate is the total plan price divided by the number of months",
                "evidence": _snippet(text, m.start(), m.end())}
    return {"kind": "none", "months": None, "note": ""}


def extract_renewal(text: str, start: int, end: int, price: float, currency: str) -> dict:
    """Find the price this figure becomes later, when the page states one.

    Returns {} or {"price", "currency", "kind", "evidence"}.

    Only a renewal *above* the advertised figure is recorded — that is the fact a
    reader needs and would otherwise miss, and a lower later price is not a
    caveat. Two guards keep it honest: the sentence has to sit near the figure,
    and no other price may fall between the two, because in a plan table the
    nearest following sentence often belongs to the next plan down.
    """
    after_off = end
    before_off = max(0, start - RENEWAL_BEFORE_WINDOW)
    after = text[after_off:after_off + RENEWAL_AFTER_WINDOW]
    before = text[before_off:start]

    probes = (
        # "…$2.09 /mo For 24 month term Renews at $4.68 /mo" — the claim follows.
        ("renews_at", RENEWAL_AFTER_RE, after, after_off, True),
        # "…$29.95 /mo INTRO OFFER … $54.95 /mo when you renew" — the renewal
        # price itself follows the headline, phrased backwards.
        ("renewal_price", RENEWAL_BEFORE_RE, after, after_off, True),
        ("regular_price", REGULAR_PRICE_RE, after, after_off, True),
        # The same two forms stated before the figure, for completeness.
        ("renewal_price", RENEWAL_BEFORE_RE, before, before_off, False),
        ("regular_price", REGULAR_PRICE_RE, before, before_off, False),
    )
    for kind, rx, chunk, off, claim_follows in probes:
        m = rx.search(chunk)
        if not m:
            continue
        # If another price sits between this figure and the sentence, the
        # sentence probably describes that other plan, not this one. When the
        # claim follows the figure the gap is the text before it; when the claim
        # carries its own price and precedes the figure, the gap is after it.
        between = chunk[:m.start()] if claim_follows else chunk[m.end():]
        if ANY_MONEY_RE.search(between):
            continue
        cur = SYM_TO_CURRENCY.get(m.group("sym"), "USD")
        if cur != currency:
            continue  # never compare across currencies, not even to warn
        val = _to_float(m.group("amt"))
        if val is None or val <= price:
            continue
        # "…Renews at $11.99/mo for 2 years." — the period the renewed rate
        # holds for is part of the same sentence, so it is read from directly
        # after the figure and nowhere else.
        rmonths = None
        tail = text[off + m.end():off + m.end() + 40]
        tm = RENEWAL_TERM_RE.match(tail)
        if tm:
            n = int(tm.group("n"))
            if tm.group("unit").lower().startswith(("year", "yr")):
                n *= 12
            if 1 <= n <= 60:
                rmonths = n
        return {"price": val, "currency": cur, "kind": kind, "months": rmonths,
                "evidence": _snippet(text, off + m.start(), off + m.end())}
    return {}


def _occurrences(text: str, value: float, currency: str) -> list[tuple[int, int]]:
    """Every place this exact figure appears, as (start, end) offsets."""
    out = set()
    for rx in (PRICE_MONTHLY_RE, PRICE_FROM_RE):
        for m in rx.finditer(text):
            sym = m.group("sym")
            cur = SYM_TO_CURRENCY.get(sym if sym in SYM_TO_CURRENCY else sym.upper(), "USD")
            if cur != currency:
                continue
            v = _to_float(m.group("amt"))
            if v is not None and abs(v - value) < 1e-9:
                out.add((m.start(), m.end()))
    return sorted(out)


def _signal_score(text: str, start: int, end: int, price: float, currency: str) -> int:
    """How much a single mention of a figure tells us about the offer."""
    score = 0
    term = extract_billing_term(text, start, end)
    if term["months"]:
        score += 2
    if term["note"]:
        score += 1
    if extract_renewal(text, start, end, price, currency):
        score += 3
    return score


def best_occurrence(text: str, price: float, currency: str) -> tuple[int, int] | None:
    """Pick the most informative mention of a figure the page repeats.

    A page often states the same price twice: once in a hero line and again in
    the plan table, which is where the commitment and the renewal price live.
    The hero mention comes first, so taking it blindly throws the caveats away —
    IONOS's "$ 2 /month" is the hero, while "$ 2 /month for 3 months with a
    1-year term" is the actual offer. Choosing the mention with the most
    surrounding signal keeps the stored evidence and the published caveat
    describing the same sentence.
    """
    occ = _occurrences(text, price, currency)
    if not occ:
        return None
    return max(occ, key=lambda se: (_signal_score(text, se[0], se[1], price, currency),
                                    -se[0]))


def extract_valid_until(text: str) -> str | None:
    m = EXPIRES_RE.search(text)
    if not m:
        return None
    raw = m.group("date").strip().rstrip(".")
    raw = re.sub(r"(st|nd|rd|th)\b", "", raw, flags=re.I).strip()
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    m2 = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s*(\d{4})", raw)
    if m2:
        mon = MONTHS.get(m2.group(1)[:3].lower())
        if mon:
            try:
                return datetime(int(m2.group(3)), mon, int(m2.group(2))).date().isoformat()
            except ValueError:
                pass
    return None


def extract_page_meta(html: str) -> tuple[str | None, str | None]:
    """Pull <title> and meta description out of a page, entity-decoded.

    Entities must be decoded here rather than left in place: the build step
    escapes values on output, so an undecoded `&amp;` in the source would be
    escaped twice and rendered literally as "&amp;".
    """
    t = re.search(r"<title[^>]*>([\s\S]{0,300}?)</title>", html, re.I)
    title = unescape(re.sub(r"\s+", " ", t.group(1))).strip() if t else None
    d = (re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{0,400})["\']', html, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']{0,400})["\'][^>]+name=["\']description["\']', html, re.I))
    desc = unescape(re.sub(r"\s+", " ", d.group(1))).strip() if d else None
    return title or None, desc or None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape_provider(prov: dict, cfg: ilang.SiteConfig, settings: dict,
                    robots: RobotsCache, ua: str, timeout: int) -> dict:
    name, url = prov["name"], prov["offer_url"]
    rec: dict = {
        "provider": name,
        "homepage": prov["homepage"],
        "offer_url": url,
        "affiliate_url": prov["affiliate_url"],
        "source_url": url,
        "fetched_at": _now_iso(),
    }

    if not robots.allowed(url):
        rec.update(status="blocked_by_robots", http_status=None,
                   note="robots.txt disallows this path for our user-agent")
        return rec

    try:
        status, html, _headers = http_get(url, ua, timeout)
    except urllib.error.HTTPError as exc:
        rec.update(status="blocked" if exc.code in (401, 403, 429) else "error",
                   http_status=exc.code, note=f"HTTP {exc.code}")
        return rec
    except Exception as exc:
        rec.update(status="error", http_status=None,
                   note=f"{type(exc).__name__}: {exc}")
        return rec

    if status != 200:
        rec.update(status="error", http_status=status, note=f"HTTP {status}")
        return rec

    title, desc = extract_page_meta(html)

    # A ranking/list page about the market prices other companies, so any number
    # on it would be attributed to the wrong provider. Refuse the whole page.
    if title and THIRD_PARTY_TITLE_RE.search(title):
        rec.update(status="third_party_page", http_status=status, title=title, summary=desc,
                   note=("page title indicates a third-party ranking/list, so its prices "
                         "belong to other companies; nothing extracted"))
        return rec

    text = strip_to_visible_text(html)
    prices = extract_prices(text, float(settings["min_plausible_monthly_price"]),
                            float(settings["max_plausible_monthly_price"]))
    discount = extract_discount(text)
    valid_until = extract_valid_until(text)

    rec["http_status"] = status
    rec["title"] = title
    rec["summary"] = desc
    rec["page_chars"] = len(text)
    rec["price_candidates_count"] = len(prices)
    # Keep the record small: the cheapest dozen candidates is plenty for audit.
    # The match offsets are internal plumbing for the billing-term lookup and
    # have no meaning in the published data, so they are stripped here.
    rec["price_candidates"] = [
        {k: v for k, v in p.items() if k not in ("start", "end")} for p in prices[:12]
    ]

    headline = pick_headline(prices, settings.get("currency", "USD"))
    if headline:
        # A page may state the same figure more than once. The first mention is
        # usually a hero line; the plan table repeats it with the commitment and
        # the renewal price attached. Re-point at the most informative mention so
        # the stored evidence and the published caveats describe one sentence.
        better = best_occurrence(text, headline["value"], headline["currency"])
        if better:
            headline["start"], headline["end"] = better
            headline["evidence"] = _snippet(text, better[0], better[1])

        rec["status"] = "ok"
        rec["price"] = headline["value"]
        rec["currency"] = headline["currency"]
        rec["price_context"] = headline["context"]
        rec["price_tier"] = headline["tier"]
        rec["price_evidence"] = headline["evidence"]
        rec["price_reason"] = headline["reason"]

        term = extract_billing_term(text, headline["start"], headline["end"])
        if term["kind"] != "none":
            rec["billing_term_kind"] = term["kind"]
        if term["months"]:
            rec["billing_term_months"] = term["months"]
        if term["note"]:
            rec["billing_term_note"] = term["note"]
        if term.get("evidence"):
            rec["billing_term_evidence"] = term["evidence"]

        renewal = extract_renewal(text, headline["start"], headline["end"],
                                  headline["value"], headline["currency"])
        if renewal:
            rec["renewal_price"] = renewal["price"]
            rec["renewal_currency"] = renewal["currency"]
            rec["renewal_kind"] = renewal["kind"]
            rec["renewal_evidence"] = renewal["evidence"]
            if renewal.get("months"):
                rec["renewal_months"] = renewal["months"]
    elif prices:
        # We saw numbers but none looked like a plan price. Say so; do not guess.
        rec["status"] = "no_price_in_html"
        rec["note"] = (f"{len(prices)} price-like figure(s) found but none classified as a "
                       "VPS plan price (all looked like add-ons)")
    else:
        rec["status"] = "no_price_in_html"
        rec["note"] = ("page returned 200 but exposes no server-rendered monthly price "
                       "(likely JS-injected); not inventing a figure")

    if discount:
        rec["discount"] = discount
    if valid_until:
        rec["valid_until"] = valid_until
    return rec


def carry_forward(prev: dict | None, rec: dict) -> dict:
    """If a refetch failed, keep the last verified price but mark it stale.

    The original fetched_at is preserved as last_verified_at, so the site can say
    'last verified <date>' instead of pretending the number is fresh. The record
    is explicitly labelled stale — it is never silently presented as current.
    """
    if not prev or "price" not in prev or rec["status"] == "ok":
        return rec
    merged = dict(rec)
    merged["fetch_status"] = rec["status"]
    merged["status"] = "stale"
    merged["price"] = prev["price"]
    merged["currency"] = prev.get("currency", "USD")
    merged["price_evidence"] = prev.get("price_evidence")
    merged["price_context"] = prev.get("price_context")
    merged["price_tier"] = prev.get("price_tier")
    # The carried-forward price keeps whatever commitment it was quoted under;
    # dropping it here would quietly turn a 24-month rate back into a plain
    # monthly one on the next failed refetch. The same goes for the renewal
    # price: losing it would make a promotional rate look permanent.
    for k in ("billing_term_kind", "billing_term_months", "billing_term_note",
              "billing_term_evidence",
              "renewal_price", "renewal_currency", "renewal_kind", "renewal_evidence",
              "renewal_months"):
        if prev.get(k):
            merged[k] = prev[k]
    merged["last_verified_at"] = prev.get("fetched_at")
    merged["stale"] = True
    if prev.get("discount") and "discount" not in merged:
        merged["discount"] = prev["discount"]
    return merged


def update_history(offers: list[dict], now_iso: str) -> tuple[dict, int]:
    """Record each provider's price over time. Append-only; never rewrites the past.

    This is the one asset here that cannot be backfilled: a run that does not
    record a price loses that observation permanently. A record of how a
    provider's price actually moved across months is something no affiliate page
    and no competitor has, and it is the part of this dataset that gets more
    valuable the longer the pipeline runs.

    An entry is appended only when the observable state (price, currency, status)
    differs from the previous one, so the file stays small and every entry marks
    a real change. A second change on the same day replaces that day's entry
    rather than stacking duplicates.
    """
    hist: dict = {"schema": "price-history/1", "providers": {}}
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict) and isinstance(loaded.get("providers"), dict):
                hist["providers"] = loaded["providers"]
        except (OSError, ValueError):
            pass  # unreadable history must not stop a refresh; we just start again

    day = now_iso[:10]

    def same_state(a: dict | None, state: dict) -> bool:
        return bool(a) and (a.get("price"), a.get("currency"), a.get("status")) == \
                          (state["price"], state["currency"], state["status"])

    appended = 0
    for r in offers:
        state = {"date": day, "price": r.get("price"), "currency": r.get("currency"),
                 "status": r.get("status")}
        entries = hist["providers"].setdefault(r["provider"], [])
        prev = entries[-1] if entries else None
        if same_state(prev, state):
            continue                       # nothing observable changed
        if prev and prev.get("date") == day:
            # A revision later the same day. If it lands back on the value that
            # was already recorded before today, today's entry has nothing to
            # say and is dropped rather than left behind as a consecutive
            # duplicate — which is exactly what a transient fetch failure
            # followed by a successful retry used to leave in this file.
            before = entries[-2] if len(entries) > 1 else None
            if same_state(before, state):
                entries.pop()
            else:
                entries[-1] = state
        else:
            entries.append(state)
            appended += 1
    return hist, appended


def main() -> int:
    cfg = ilang.load(CONFIG_PATH)
    settings = cfg.settings()
    ua = settings["user_agent"]
    timeout = int(settings["request_timeout_seconds"])
    delay = float(settings["request_delay_seconds"])
    robots = RobotsCache(ua, timeout, settings.get("respect_robots", "true") == "true")

    providers = cfg.providers
    print(f"[scraper] config={CONFIG_PATH}")
    print(f"[scraper] brand={cfg.brand!r} niche={cfg.niche!r} domain={cfg.domain!r}")
    print(f"[scraper] providers from config: {len(providers)}")

    prev_by_name: dict[str, dict] = {}
    prev_priced = None
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as fh:
                prev_doc = json.load(fh)
            for r in prev_doc.get("offers", []):
                prev_by_name[r.get("provider", "")] = r
            # Kept so verify.py can tell a normal refresh from a run where the
            # whole crawl fell over, and refuse to publish the latter.
            prev_priced = prev_doc.get("providers_with_price")
        except (OSError, ValueError):
            pass

    offers = []
    for i, prov in enumerate(providers):
        rec = scrape_provider(prov, cfg, settings, robots, ua, timeout)
        rec = carry_forward(prev_by_name.get(prov["name"]), rec)
        offers.append(rec)
        bits = [rec["status"]]
        if "price" in rec:
            bits.append(f"{rec['currency']} {rec['price']:g}")
            if rec.get("stale"):
                bits.append("STALE")
        if rec.get("discount"):
            bits.append(rec["discount"]["label"])
        print(f"[scraper] {rec['provider']:<20} {' | '.join(bits)}")
        if i < len(providers) - 1:
            time.sleep(delay)

    with open(CONFIG_PATH, "rb") as fh:
        cfg_hash = hashlib.sha256(fh.read()).hexdigest()[:16]

    doc = {
        "schema": "vps-deals-promo-radar/offers@1",
        "generated_at": _now_iso(),
        "config_path": ".ilang/site.ilang",
        "config_sha256_16": cfg_hash,
        "brand": cfg.brand,
        "niche": cfg.niche,
        "domain": cfg.domain,
        "locale": settings.get("locale", "en-US"),
        "site_currency": settings.get("currency", "USD"),
        "providers_configured": len(providers),
        "providers_with_price": sum(1 for r in offers if r.get("status") == "ok"),
        "previous_providers_with_price": prev_priced,
        "notes": [
            "Every price carries price_evidence: the literal page text it was matched from.",
            "A provider with no price key means no machine-readable monthly price was found; "
            "no figure was invented.",
            "stale=true means a refetch failed and the last verified price is shown with its date.",
        ],
        "offers": offers,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    print(f"[scraper] wrote {OUT_PATH}")

    hist, appended = update_history(offers, doc["generated_at"])
    with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
        json.dump(hist, fh, ensure_ascii=False, indent=2)
    tracked = sum(len(v) for v in hist["providers"].values())
    print(f"[scraper] history: {tracked} observation(s) across "
          f"{len(hist['providers'])} provider(s); +{appended} new this run")

    print(f"[scraper] with price: {doc['providers_with_price']}/{len(providers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
