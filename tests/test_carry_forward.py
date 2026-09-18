"""Tests for carry_forward. Run with:  python tests/test_carry_forward.py

carry_forward is what lets the site keep showing a price it can no longer
re-read. Everything it does is a claim about a number it did not just fetch, so
the two things worth testing are:

  * the date it reports must be a real read, and must not advance while the
    refetches keep failing. A record that is stale for a week must still say it
    was last verified a week ago — otherwise the stale label is decoration and
    the page is asserting a freshness it does not have.
  * the evidence behind the number must survive with it. A price without its
    rejected-figures list leaves the provider page promising to show them over
    an empty table.

Both regressions shipped once. These tests exist so they cannot ship again.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import carry_forward  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        PASS.append(label)
        print(f"  ok   {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


def ok_record(fetched_at: str = "2026-09-18T11:43:30Z") -> dict:
    return {
        "provider": "Vultr", "status": "ok", "fetched_at": fetched_at,
        "price": 2.5, "currency": "USD",
        "price_evidence": "$2.50 /mo", "price_context": "from",
        "price_tier": "entry", "price_candidates_count": 125,
        "price_candidates": [{"value": 2.5, "currency": "USD", "tier": "entry"}],
    }


def failed_record(fetched_at: str, status: str = "blocked") -> dict:
    return {"provider": "Vultr", "status": status, "fetched_at": fetched_at,
            "http_status": 403, "note": "no readable price in the response"}


print("carry_forward")
print("-" * 72)

# A successful read is not a carry-forward at all: the record is returned as
# scraped, with no stale marker invented.
good = ok_record()
check("an ok record is returned untouched", carry_forward(good, good), good)

# A record with no price behind it cannot be carried forward, whatever failed.
check("no previous price -> nothing to carry",
      carry_forward(None, failed_record("2026-09-18T11:50:41Z"))["status"], "blocked")

first = carry_forward(ok_record(), failed_record("2026-09-18T11:50:41Z"))
check("first failure keeps the price", first["price"], 2.5)
check("first failure is marked stale", first["status"], "stale")
check("first failure dates to the real read", first["last_verified_at"],
      "2026-09-18T11:43:30Z")
check("the failed attempt is kept separately", first["fetch_status"], "blocked")

# The regression: the date used to be taken from prev["fetched_at"], which on an
# already-stale record is the previous *failed* attempt. Each blocked run moved
# the date forward until a days-old price claimed to have been verified today.
second = carry_forward(first, failed_record("2026-09-18T12:33:09Z"))
third = carry_forward(second, failed_record("2026-09-18T18:17:44Z"))
check("second failure does not advance the date", second["last_verified_at"],
      "2026-09-18T11:43:30Z")
check("third failure does not advance the date", third["last_verified_at"],
      "2026-09-18T11:43:30Z")
check("the date never reaches the failed attempt", third["last_verified_at"]
      == third["fetched_at"], False)

# The evidence has to travel with the price it belongs to, or the provider page
# prints a heading promising the rejected figures above an empty table.
check("candidates survive a failure", third["price_candidates_count"], 125)
check("candidate list survives a failure", len(third["price_candidates"]), 1)
check("the quoted evidence survives a failure", third["price_evidence"], "$2.50 /mo")

# A successful refetch must clear the stale marker rather than inherit it. It
# also carries no last_verified_at at all: the read *is* this run, and build.py
# falls back to fetched_at. Keeping a stale date here would be the same bug in
# the other direction.
recovered = carry_forward(third, ok_record("2026-09-19T06:17:00Z"))
check("a later success is not stale", recovered.get("stale"), None)
check("a later success is not marked stale", recovered["status"], "ok")
check("a later success carries no inherited date",
      "last_verified_at" in recovered, False)
check("a later success dates to its own read", recovered["fetched_at"],
      "2026-09-19T06:17:00Z")

# A discount rides along with the price it was attached to. If it is no longer
# supported by the text that is its only evidence, it goes with the price rather
# than outliving it — otherwise a claim that stopped being true the moment the
# page changed would keep being published for as long as the fetch kept failing.
tied = dict(first, price=2.5, discount={
    "kind": "percent", "value": 50, "label": "50% off",
    "evidence": "…50% off all plans, limited time. Starting at $2.50/mo…"})
loose = dict(first, price=2.5, discount={
    "kind": "percent", "value": 70, "label": "70% off",
    "evidence": "…Up to 70% off VPS hosting for more power and…"})

carried = carry_forward(tied, failed_record("2026-09-18T12:33:09Z"))
check("a supported discount survives with the price",
      carried.get("discount", {}).get("label"), "50% off")
dropped = carry_forward(loose, failed_record("2026-09-18T12:33:09Z"))
check("an unsupported discount does not survive a failed refresh",
      "discount" in dropped, False)
check("dropping the discount leaves the price alone", dropped["price"], 2.5)

# The text is checked against the figure it would be printed beside, so a
# discount that names some other plan's price is dropped even though it was
# recorded on a record that did have a price.
other_plan = dict(first, price=2.5, discount={
    "kind": "percent", "value": 70, "label": "70% off",
    "evidence": "…70 % off $ 3.99 /mo $ 9.99 /mo Choose your size…"})
check("a discount naming another plan's price is dropped",
      "discount" in carry_forward(other_plan, failed_record("2026-09-18T12:33:09Z")),
      False)

print()
print("-" * 72)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for label in FAIL:
        print(f"  failed: {label}")
    raise SystemExit(1)
