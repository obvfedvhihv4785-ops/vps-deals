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


def _long_date(iso: str | None) -> str:
    """Same rendering as build.fmt_date, so the strings can be compared."""
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return dt.strftime("%d %b %Y, %H:%M UTC")


def _slugify(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")


def check_stale_disclosure(doc: dict) -> None:
    """A stale number must never be dated to the fetch that failed.

    On a stale record fetched_at is the time of the *failed* refetch, while the
    published price and its quoted evidence come from last_verified_at. Using
    fetched_at for a freshness claim therefore dates the number to a fetch that
    returned nothing — a seven-minute lie in the mild case, and a
    days-old-number-labelled-today lie across a real outage. The data layer
    already carries the distinction, so what is checked here is that the
    rendered pages do not throw it away.
    """
    home_path = os.path.join(SITE, "index.html")
    if not os.path.exists(home_path):
        errors.append("index.html missing — cannot check stale disclosure")
        return
    with open(home_path, encoding="utf-8") as fh:
        home = fh.read()

    # Attribute each card to its provider before looking for a date, because the
    # same timestamp is legitimately printed on other providers' cards. A plain
    # substring search over the whole page would flag those instead.
    cards: dict[str, str] = {}
    for card in home.split('<article class="card">')[1:]:
        m = re.search(r'<h3><a href="[^"]*">([^<]+)</a></h3>', card)
        if m:
            cards[m.group(1).strip()] = card

    for o in doc.get("offers", []):
        if o.get("status") != "stale":
            continue
        name = o.get("provider", "?")
        read = _long_date(o.get("last_verified_at"))
        attempt = _long_date(o.get("fetched_at"))
        slug = _slugify(name)

        # A missing card is not an error: max_deals_on_index caps the homepage,
        # so the least competitive providers are legitimately absent from it.
        # What matters is that a stale card, when it is shown, is labelled with
        # the read date and not with the failed attempt.
        card = cards.get(name)
        if card is not None and attempt != read:
            if f"verified {attempt}" in card:
                errors.append(
                    f"{name}: homepage badge calls a stale price verified at {attempt}, "
                    f"which is the failed refetch — the read was {read}")
            if f"last verified {read}" not in card:
                errors.append(
                    f"{name}: homepage card does not state the last-verified date ({read})")

        # The evidence line must be dated to the read, not to the attempt. Scope
        # the search to the evidence section so the footer's run stamp and the
        # history table cannot satisfy or trip it.
        for rel, marker in ((f"provider/{slug}.html", "Where this number came from"),
                            (f"deal/{slug}.html", "The evidence")):
            path = os.path.join(SITE, rel)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as fh:
                html = fh.read()
            if marker not in html:
                continue
            seg = html.split(marker, 1)[1].split("</section>", 1)[0]
            if attempt != read and f"on {attempt}" in seg:
                errors.append(
                    f"{rel}: evidence is dated to the failed refetch ({attempt}) "
                    f"instead of the read ({read})")
            if f"on {read}" not in seg:
                errors.append(
                    f"{rel}: evidence line does not carry the read date ({read})")

            # The Offer/Product block asserts availability InStock, which is a
            # claim about the present. A search engine reads that block on its
            # own, without the prose, so the qualifier has to be inside it.
            if ('"availability": "https://schema.org/InStock"' in html
                    and "last verified figure rather than a current reading" not in html):
                errors.append(
                    f"{rel}: structured data asserts InStock for a stale figure with "
                    f"no qualifier")

            # A heading that promises the rejected figures must not be printed
            # above an empty table.
            promise = "Figures the pipeline rejected are listed too"
            if promise in html:
                body = html.split(promise, 1)[1].split("</table>", 1)[0]
                body = body.split("</thead>", 1)[-1]
                if "<tr>" not in body:
                    errors.append(
                        f"{rel}: rejected-figures table is empty under its own promise")

            # update_history() appends a row only when the price or status
            # changes, so copy promising a row per run describes a table this
            # pipeline does not produce. The wording is part of the claim, and a
            # claim the data cannot support is the failure this whole file exists
            # to catch — so the phrase is checked rather than trusted.
            if "on every run" in html:
                errors.append(
                    f"{rel}: history copy claims a row per run, but entries are only "
                    f"recorded when the price or status changes")

    # The comparison table prints its own status badge, and it is the page a
    # buyer scans prices on. A bare "verified" there is the same contradiction
    # as the long form on a card, so it is checked against the status too.
    compare_path = os.path.join(SITE, "compare.html")
    if os.path.exists(compare_path):
        with open(compare_path, encoding="utf-8") as fh:
            compare = fh.read()
        rows: dict[str, str] = {}
        for row in compare.split("<tr")[1:]:
            m = re.search(r'<a href="[^"]*">([^<]+)</a>', row)
            if m:
                rows.setdefault(m.group(1).strip(), row)
        for o in doc.get("offers", []):
            if o.get("status") != "stale":
                continue
            name = o.get("provider", "?")
            row = rows.get(name)
            if row is None:
                continue
            if ">verified<" in row:
                errors.append(
                    f"compare.html: {name}'s row is badged verified although its "
                    f"refresh failed")
            read = _long_date(o.get("last_verified_at"))
            if read not in row:
                errors.append(
                    f"compare.html: {name}'s row does not carry the read date ({read})")


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
        # A stale record's date is only ever inherited from a run that really
        # read the page, so it can never be the timestamp of the attempt that
        # failed. The freeze across runs is enforced in carry_forward() and
        # covered by tests/test_carry_forward.py; what one snapshot can show is
        # that the date is not the failed attempt's own time.
        if o.get("status") == "stale":
            lv, fa = o.get("last_verified_at"), o.get("fetched_at")
            if lv and fa and lv >= fa:
                errors.append(
                    f"{name}: stale but last_verified_at ({lv}) is not earlier than "
                    f"the failed refetch ({fa}) — the date is drifting")
        if o.get("valid_until") and o["valid_until"] < datetime.date.today().isoformat() \
                and status == "ok":
            warnings.append(f"{name}: valid_until has passed but status is still ok")

    # The two summary counts must agree with the records they summarise. They
    # measure different things on purpose — one is what the site shows, the
    # other is what was read fresh this run and is what the breaker watches —
    # and they were off by one for a release, so the site published 25 prices
    # while every report said 24. Recomputed here rather than trusted.
    offers = doc.get("offers", [])
    expected_shown = sum(1 for o in offers if isinstance(o.get("price"), (int, float)))
    expected_fresh = sum(1 for o in offers if o.get("status") == "ok")
    for field, expected, what in (
        ("providers_with_price_shown", expected_shown, "prices to show"),
        ("providers_with_price", expected_fresh, "read fresh this run"),
    ):
        claimed = doc.get(field)
        if claimed != expected:
            errors.append(
                f"offers.json {field}={claimed!r} but the records contain {expected} "
                f"{what} — the summary and the data disagree")

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


TERM_KINDS = {"term", "intro", "annual", "ambiguous_terms", "ambiguous_toggle",
              "intro_word", "prepay", "none", ""}

# How each kind of claim has to be traceable back to the quoted page text. The
# notes for the ambiguous kinds are paraphrase, so there is nothing to match
# them against — but the ones that assert a specific commitment must be checkable,
# because an invented term is a false statement about money.
#
# A commitment may be written in months or in years ("for 24 month term" vs
# "with a 1-year term"), and the same page can phrase the promotional period
# either before the figure ("First 3 months at $8.99") or after it ("$2 /month
# for 3 months"). Both spellings are the page saying the same thing, so both
# count as traceable; the check is that the page said it, not how.
TERM_YEAR_RE = re.compile(r"\b(?P<n>\d{1,2})\s*[-\s]?\s*years?\b", re.I)
TERM_PROMO_MARK_RE = re.compile(
    r"\b(?:first|initial|introductory|intro)\s+\d{1,2}\s*[-\s]?\s*months?\b"
    r"|/\s*mo(?:nth)?\s*for\s+\d{1,2}\s*[-\s]?\s*months?\b",
    re.I,
)


def _commitment_in_evidence(months: int, ev: str) -> bool:
    """True when the quoted text states this many months of commitment."""
    if str(months) in ev:
        return True
    if months % 12 == 0:
        for m in TERM_YEAR_RE.finditer(ev):
            if int(m.group("n")) * 12 == months:
                return True
    return False


# Kinds whose claim rests on page-level wording rather than the price snippet,
# so they carry their own quoted evidence and are checked against it instead.
TERM_PAGE_EVIDENCE = {"prepay": r"paid\s+upfront"}

RENEWAL_KINDS = {"renews_at", "renewal_price", "regular_price"}

# Each kind names the wording that makes it a renewal claim rather than a
# sentence that merely contains the same number.
RENEWAL_TRACE = {
    "renews_at": lambda ev: bool(re.search(r"renews?\s+at", ev, re.I)),
    "renewal_price": lambda ev: bool(re.search(
        r"when you renew|at renewal|on renewal|upon renewal", ev, re.I)),
    "regular_price": lambda ev: bool(re.search(r"(regular|list|standard)\s+price", ev, re.I)),
}


def check_billing_terms() -> None:
    """A billing-term claim must be consistent and traceable to the page text.

    The site tells readers that a price depends on a 24-month commitment. That
    is a statement about money, so it gets the same treatment as the price
    itself: it must be present exactly when the data says it is, it must be
    plausible, and the specific numbers in it must appear in the text the
    scraper quoted from the provider's own page.
    """
    path = os.path.join(os.path.dirname(SITE), "data", "offers.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    for o in doc.get("offers", []):
        who = o.get("provider", "?")
        kind = o.get("billing_term_kind", "")
        months = o.get("billing_term_months")
        note = o.get("billing_term_note", "")

        if kind not in TERM_KINDS:
            errors.append(f"{who}: unknown billing_term_kind {kind!r}")
            continue

        # Presence has to agree with the kind in both directions: a kind with
        # nothing to say, or a claim with no kind, both mean the renderer and
        # the data have drifted apart.
        if kind and kind != "none":
            if not months and not note:
                errors.append(f"{who}: billing_term_kind={kind!r} but no months and no note")
        elif months or note:
            errors.append(f"{who}: term data present ({months!r}/{note!r}) but kind is empty")

        ev = o.get("price_evidence") or ""

        if months is not None:
            if not isinstance(months, int) or not 1 <= months <= 60:
                errors.append(f"{who}: implausible billing_term_months {months!r}")
                continue
            # A commitment stated as "billed annually" has no number in the text
            # to match, so it is traced by its own wording instead.
            if kind == "annual":
                ok = bool(re.search(r"per\s+year|annually|/\s*yr", ev, re.I))
            else:
                ok = _commitment_in_evidence(months, ev)
            if not ok:
                errors.append(
                    f"{who}: claims a {months}-month term, but that is not supported by its own "
                    f"price_evidence ({ev[:70]!r}) — the claim is not traceable to the page")

        # An introductory rate is a claim about a promotional period, which the
        # page states in its own words. The note paraphrases it, so the wording
        # has to be found in the quoted text.
        if kind in ("intro", "intro_word") and not TERM_PROMO_MARK_RE.search(ev):
            errors.append(
                f"{who}: claims an introductory rate, but no promotional period is stated in "
                f"its own price_evidence ({ev[:70]!r})")

        # A page-level claim (currently only "paid upfront") applies to every
        # price on the page, so it cannot be checked against one figure's
        # snippet. It carries its own quoted sentence, and that sentence has to
        # contain the wording the claim rests on.
        pattern = TERM_PAGE_EVIDENCE.get(kind)
        if pattern:
            ev = o.get("billing_term_evidence") or ""
            if not ev:
                errors.append(f"{who}: billing_term_kind={kind!r} publishes no quoted evidence")
            elif not re.search(pattern, ev, re.I):
                errors.append(
                    f"{who}: billing_term_kind={kind!r} is not supported by its own "
                    f"billing_term_evidence ({ev[:70]!r})")


def check_renewal_claims() -> None:
    """A published renewal figure must be real, higher, and quoted from the page.

    The renewal is the most consequential number on a deal page after the
    headline: it is the difference between "this costs $2.09/mo" and "this costs
    $2.09/mo for now". Getting it wrong in either direction is a false statement
    about money — inventing a rise that is not there, or dropping one that is.

    So a renewal may only appear when all of the following hold, and the checks
    are deliberately symmetrical: data without a figure and a figure without
    traceable evidence both fail.
    """
    path = os.path.join(os.path.dirname(SITE), "data", "offers.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    for o in doc.get("offers", []):
        who = o.get("provider", "?")
        value = o.get("renewal_price")
        kind = o.get("renewal_kind", "")
        evidence = o.get("renewal_evidence") or ""
        months = o.get("renewal_months")
        has_figure = isinstance(value, (int, float))

        if not has_figure:
            # A kind, an evidence string or a period with no figure is drift.
            if kind or evidence or months:
                errors.append(
                    f"{who}: renewal metadata present (kind={kind!r}, months={months!r}) "
                    f"but no renewal_price")
            continue

        if value <= 0:
            errors.append(f"{who}: renewal_price {value!r} is not a positive number")
            continue

        if kind not in RENEWAL_KINDS:
            errors.append(f"{who}: unknown renewal_kind {kind!r}")
            continue

        price = o.get("price")
        if not isinstance(price, (int, float)):
            errors.append(f"{who}: has a renewal_price but no advertised price to compare it to")
        elif value <= price:
            # The scraper only records a rise. A renewal at or below the
            # headline is not a caveat, and publishing it as one would be a
            # fabricated warning.
            errors.append(
                f"{who}: renewal_price {value} is not above the advertised price {price}")

        # Currency must match the headline; the site never compares across
        # currencies, and a mismatched pair would mean the two numbers cannot be
        # read together at all.
        cur = o.get("renewal_currency") or ""
        if cur and cur != o.get("currency"):
            errors.append(
                f"{who}: renewal_currency {cur!r} differs from offer currency "
                f"{o.get('currency')!r}")

        if not evidence:
            errors.append(f"{who}: publishes a renewal figure with no quoted evidence")
            continue

        # The figure itself must appear in the quoted text. Both spellings are
        # accepted because pages write "$4.68" and "4.68" interchangeably.
        if not _figure_in_text(value, evidence):
            errors.append(
                f"{who}: renewal figure {value} does not appear in its own evidence "
                f"({evidence[:70]!r}) — the claim is not traceable to the page")

        # And the evidence must actually be a renewal claim, not some other
        # sentence that happens to contain the number.
        probe = RENEWAL_TRACE.get(kind)
        if probe and not probe(evidence):
            errors.append(
                f"{who}: renewal_kind={kind!r} is not supported by its own evidence "
                f"({evidence[:70]!r})")

        if months is not None:
            if not isinstance(months, int) or not 1 <= months <= 60:
                errors.append(f"{who}: implausible renewal_months {months!r}")


def _figure_in_text(value: float, text: str) -> bool:
    """True when a money figure appears in a quoted snippet, in either spelling."""
    plain = f"{value:g}"
    two_dp = f"{value:.2f}"
    return plain in text or two_dp in text


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
    check_billing_terms()
    check_renewal_claims()

    offers_path = os.path.join(os.path.dirname(SITE), "data", "offers.json")
    if os.path.exists(offers_path):
        with open(offers_path, encoding="utf-8") as fh:
            check_stale_disclosure(json.load(fh))

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
