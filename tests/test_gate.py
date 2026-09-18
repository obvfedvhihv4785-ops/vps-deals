"""Tests for the gate itself. Run with:  python tests/test_gate.py

verify.py is what stands between a bad crawl and a published page, so the thing
worth testing is not that it passes on good data — it is that it *fails* on bad
data. A gate that has never been shown to reject anything is indistinguishable
from no gate at all.

Each case below injects one specific falsehood, runs verify.py, and requires it
to be caught. Every file touched is restored afterwards and the dataset is
re-verified, so a failing test cannot leave the data corrupted.

These are the exact mistakes this pipeline has been at risk of making:
inventing a renewal, dropping one that exists, warning about a rise that is not
there, and asserting a commitment the page never stated.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OFFERS = ROOT / "data" / "offers.json"
HISTORY = ROOT / "data" / "history.json"


def run_verify() -> list[str]:
    """Run verify.py and return the FAIL lines it produced."""
    proc = subprocess.run(
        [sys.executable, "verify.py"], cwd=ROOT, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    return [line for line in out.splitlines() if "FAIL" in line]


PASS: list[str] = []
FAIL: list[str] = []


def expect_caught(label: str, path: Path, mutate) -> None:
    original = path.read_text(encoding="utf-8")
    try:
        doc = json.loads(original)
        mutate(doc)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        caught = run_verify()
    finally:
        path.write_text(original, encoding="utf-8")

    if caught:
        PASS.append(label)
        print(f"  ok   {label}")
        print(f"         {caught[0].split('FAIL', 1)[1].strip()[:118]}")
    else:
        FAIL.append(label)
        print(f"  FAIL {label} — verify.py accepted it")


ORIGINAL = json.loads(OFFERS.read_text(encoding="utf-8"))


def expect_caught_in_html(label: str, rel: str, mutate) -> None:
    """Same idea as expect_caught, but the fault is injected into a built page.

    The stale-disclosure checks read the rendered output, so they cannot be
    exercised by editing offers.json alone — the pages would still hold the
    unmutated HTML. The file is restored in a finally block either way.
    """
    path = ROOT / "site" / rel
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text(mutate(original), encoding="utf-8")
        caught = run_verify()
    finally:
        path.write_text(original, encoding="utf-8")

    if caught:
        PASS.append(label)
        print(f"  ok   {label}")
        print(f"         {caught[0].split('FAIL', 1)[1].strip()[:118]}")
    else:
        FAIL.append(label)
        print(f"  FAIL {label} — verify.py accepted it")


def _empty_rejected_table(h: str) -> str:
    """Drop every row between </thead> and </tbody>, keeping the heading."""
    before, promise, rest = h.partition("Figures the pipeline rejected")
    head, thead, after = rest.partition("</thead>")
    body, tbody, tail = after.partition("</tbody>")
    return before + promise + head + thead + "\n        " + tbody + tail


def offer(doc: dict, provider: str) -> dict:
    return next(o for o in doc["offers"] if o["provider"] == provider)


print("the gate must reject these")
print("-" * 72)

expect_caught(
    "a renewal figure that appears nowhere in its own evidence", OFFERS,
    lambda d: offer(d, "HostGator").update(renewal_price=99.99))

expect_caught(
    "a renewal below the advertised price (a warning that is not true)", OFFERS,
    lambda d: offer(d, "HostGator").update(renewal_price=1.00))

expect_caught(
    "a renewal kind its own evidence does not support", OFFERS,
    lambda d: offer(d, "HostGator").update(renewal_kind="regular_price"))

expect_caught(
    "renewal metadata with no figure behind it", OFFERS,
    lambda d: offer(d, "Vultr").update(renewal_kind="renews_at",
                                       renewal_evidence="Renews at $9.99"))

expect_caught(
    "a renewal in a different currency from the headline", OFFERS,
    lambda d: offer(d, "HostGator").update(renewal_currency="EUR"))

expect_caught(
    "a prepay claim with no quoted page sentence", OFFERS,
    lambda d: offer(d, "Hostinger").pop("billing_term_evidence", None))

expect_caught(
    "a prepay claim whose evidence never says 'paid upfront'", OFFERS,
    lambda d: offer(d, "Hostinger").update(
        billing_term_evidence="Save 67% on all VPS plans"))

expect_caught(
    "an introductory rate with no promotional period in the evidence", OFFERS,
    lambda d: offer(d, "DreamHost").update(
        price_evidence="DreamHost VPS $8.99 /mo Sign Up Now"))

expect_caught(
    "a commitment in months where the page states only years", OFFERS,
    lambda d: offer(d, "IONOS").update(billing_term_months=24))

expect_caught(
    "a history entry duplicating the one before it", HISTORY,
    lambda d: d["providers"]["UpCloud"].append(dict(d["providers"]["UpCloud"][-1])))

expect_caught(
    "a history entry dated out of order", HISTORY,
    lambda d: d["providers"]["Vultr"].append(
        {"date": "2020-01-01", "price": 9.99, "currency": "USD", "status": "ok"}))

# The two summary counts measure different things and disagreed by one for a
# release: the site published 25 prices while the commit message, the automation
# report and the scraper summary all said 24, because a provider had gone stale
# with its price intact. Both are now recomputed from the records.
expect_caught(
    "a summary that undercounts the prices the site will show", OFFERS,
    lambda d: d.update(providers_with_price_shown=d["providers_with_price_shown"] - 1))

expect_caught(
    "a summary that overcounts what was read fresh this run", OFFERS,
    lambda d: d.update(providers_with_price=d["providers_with_price"] + 1))

# A stale record's fetched_at is the time of the *failed* refetch, while the
# published price and its quoted evidence come from last_verified_at. Dating a
# freshness claim with fetched_at therefore stamps the number with a fetch that
# returned nothing: a seven-minute lie in the mild case, and a
# days-old-number-labelled-today lie across a real outage. These three cases are
# the exact shapes that shipped before the check existed.
expect_caught_in_html(
    "a stale price under a green 'verified' badge dated to the failed refetch",
    "index.html",
    lambda h: h.replace("stale — last verified 18 Sep 2026, 11:43 UTC",
                        "verified 18 Sep 2026, 11:50 UTC"))

expect_caught_in_html(
    "quoted evidence dated to the refetch that failed, not to the read",
    "provider/vultr.html",
    lambda h: h.replace(
        "on 18 Sep 2026, 11:43 UTC — the last time this page could be read",
        "on 18 Sep 2026, 11:50 UTC"))

expect_caught_in_html(
    "a heading promising the rejected figures, standing over an empty table",
    "provider/vultr.html", _empty_rejected_table)

print()
print("-" * 72)
restored = run_verify()
print(f"restored dataset -> {len(restored)} failure(s)")
if restored:
    FAIL.append("dataset did not return to a clean state after the tests")
    for line in restored:
        print(f"         {line}")

print(f"{len(PASS)} caught, {len(FAIL)} missed")
if FAIL:
    for label in FAIL:
        print(f"  {label}")
    raise SystemExit(1)
