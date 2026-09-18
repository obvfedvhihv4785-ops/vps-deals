"""Tests for the two extractions that decide whether this site is honest.

Run with:  python tests/test_extraction.py

Every string below is a verbatim excerpt from a live provider page, kept as it
appeared (including the odd spacing around currency symbols, which is how these
pages render). The point of the suite is not coverage for its own sake. It is
that two specific claims on the published site are claims about money:

  * the *term*  — "this rate requires a 24-month prepay"
  * the *renewal* — "this rate becomes $4.68/mo later"

Both are read out of prose. A regex that is one character too loose turns a
marketing sentence into a fabricated price, which is worse than the omission it
replaced. So the suite is weighted towards the cases that must stay *silent*.

The extraction is deliberately conservative: when a page does not state a term
plainly, the result is "not stated", never a plausible guess.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import extract_billing_term, extract_renewal  # noqa: E402


def at(text: str, needle: str) -> tuple[int, int]:
    """Locate a price in the page text the way the scraper does."""
    i = text.index(needle)
    return i, i + len(needle)


PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        PASS.append(label)
        print(f"  ok   {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


def check_term(label: str, text: str, price: str, kind, months) -> None:
    s, e = at(text, price)
    t = extract_billing_term(text, s, e)
    check(f"{label} [kind]", t["kind"], kind)
    check(f"{label} [months]", t["months"], months)


def check_renewal(label: str, text: str, price: str, value, currency, months=None) -> None:
    s, e = at(text, price)
    amount = float(re.sub(r"[^\d.]", "", price))
    r = extract_renewal(text, s, e, amount, currency)
    check(f"{label} [renewal]", r.get("price") if r else None, value)
    if value is not None:
        check(f"{label} [renewal months]", r.get("months"), months)


print("billing term — providers whose '/mo' figure is not month to month")
print("-" * 72)

# Two-year prepay, stated right after the figure.
check_term(
    "HostGator",
    "NVMe 4 NVMe 8 NVMe 16 $ 2.09 /mo For 24 month term Renews at $ 4.68 /mo $ 4.18 /mo",
    "$ 2.09", "term", 24,
)
check_term(
    "InMotion",
    "VPS 4 vCPU You Save 41% $9.99 /mo For 24 month term Renews at $16.99 /mo",
    "$9.99", "term", 24,
)
check_term(
    "Bluehost",
    "Root SSH &#43; API Choose your size $ 4.69 /mo For 24 month term Renews at $ 5.69 /mo",
    "$ 4.69", "term", 24,
)
# A term stated before the figure, phrased as a billing period.
check_term(
    "Verpex",
    "VPS-D4 starts at $10/mo on the 12-month intro rate, then renews at $19.99/mo.",
    "$10", "term", 12,
)
# A promotional period and a commitment in one sentence: both are true at once.
check_term(
    "IONOS",
    "VPS Linux XL $2 /month for 3 months with a 1-year term then $12 /month",
    "$2", "intro", 12,
)
# A rate that only holds for the first three months, with no commitment.
check_term(
    "DreamHost",
    "On Sale First 3 months at $8.99 /mo SAVE 43% Sign Up Now Auto-renews at $15.99 /mo",
    "$8.99", "intro", None,
)
# Three term toggles beside one price: which one the price belongs to is not in
# the static HTML, so the honest answer is "unclear", not one of the three.
check_term(
    "ScalaHosting",
    "Build #1 Turbo-fast NVMe SSD 36 M 12 M 1 M $ 29.95 /mo INTRO OFFER - SAVE 45 %",
    "$ 29.95", "ambiguous_toggle", None,
)
# A page-wide rule: every "/mo" figure is a prepay divided by the plan length.
check_term(
    "Hostinger",
    "Choose your VPS hosting plan 67% off KVM 1 $ 19.49 $ 6.49 /mo Choose plan "
    "Renews at $11.99/mo for 2 years. Free domain for 1 year All plans are paid "
    "upfront. The monthly rate reflects the total plan price divided by the number "
    "of months in your plan.",
    "$ 6.49", "prepay", None,
)

# The prepay claim applies to every price on the page, so it carries its own
# quoted sentence — a reader has to be able to see the words it rests on.
_host = ("KVM 1 $ 6.49 /mo All plans are paid upfront. The monthly rate reflects "
         "the total plan price divided by the number of months in your plan.")
_s, _e = at(_host, "$ 6.49")
_t = extract_billing_term(_host, _s, _e)
check("prepay [evidence quoted]", bool(_t.get("evidence")), True)
check("prepay [evidence says upfront]",
      bool(re.search(r"paid\s+upfront", _t.get("evidence", ""), re.I)), True)

# "Paid upfront" alone is not enough: some pages say it about a setup fee. Both
# halves of the rule must be present before every price on the page is relabelled.
check_term(
    "trap upfront-only",
    "VPS 2 $ 12.00 /mo Setup fee paid upfront, then monthly billing",
    "$ 12.00", "none", None,
)
check_term(
    "trap amortise-only",
    "VPS 2 $ 12.00 /mo The monthly rate reflects the total plan price divided by "
    "the number of months in your plan.",
    "$ 12.00", "none", None,
)

print()
print("billing term — these must stay silent")
print("-" * 72)

# A plain monthly price. Nothing here says otherwise, so nothing may be claimed.
check_term("Vultr", "Cloud Compute High Frequency $2.50 /mo Deploy Now", "$2.50", "none", None)
check_term("Civo", "Extra Large $5.43 /mo 8 GB RAM 40 GB disk", "$5.43", "none", None)
check_term("netcup", "VPS 1000 G11 €5.91 /month 4 GB RAM", "€5.91", "none", None)
# Traps: a duration that belongs to a freebie, not to the price. Reading any of
# these as the price's term would invent a commitment that does not exist.
check_term(
    "trap free domain",
    "Plans from $3.95 /mo Free domain for the first 24 months",
    "$3.95", "none", None,
)
check_term(
    "trap free SSL",
    "Plans from $4.50 /mo Free SSL for the first 12 months. Only $4.50/mo",
    "$4.50", "none", None,
)
check_term(
    "trap snapshots",
    "Snapshots retained for the first 6 months $6.00 /mo",
    "$6.00", "none", None,
)
# "For 24 month term" must not be read as a promotional period just because it
# starts with "for N months".
check_term(
    "trap for-N-months",
    "VPS 2 $ 13.99 /mo For 24 month term You Save 48%",
    "$ 13.99", "term", 24,
)

print()
print("renewal price — the figure the advertised rate becomes later")
print("-" * 72)

check_renewal("HostGator", "$ 2.09 /mo For 24 month term Renews at $ 4.68 /mo", "$ 2.09", 4.68, "USD")
check_renewal(
    "Hostinger",
    "$ 6.49 /mo Choose plan Renews at $11.99/mo for 2 years. Cancel anytime.",
    "$ 6.49", 11.99, "USD", months=24,
)
check_renewal(
    "DreamHost",
    "First 3 months at $8.99 /mo SAVE 43% Sign Up Now Auto-renews at $15.99 /mo after 3 months.",
    "$8.99", 15.99, "USD",
)
check_renewal(
    "MilesWeb",
    "Managed VPS KVM 4GB 22% OFF $ 89.99 $ 69.99 /mo Choose Plan Renews at $ 89.99/mo.",
    "$ 69.99", 89.99, "USD",
)
check_renewal(
    "Verpex",
    "VPS-D4 starts at $10/mo on the 12-month intro rate, then renews at $19.99/mo.",
    "$10", 19.99, "USD",
)
check_renewal("Bluehost", "$ 4.69 /mo For 24 month term Renews at $ 5.69 /mo", "$ 4.69", 5.69, "USD")
check_renewal(
    "ScalaHosting",
    "$ 29.95 /mo INTRO OFFER - SAVE 45 % Get Started $ 54.95 /mo when you renew",
    "$ 29.95", 54.95, "USD",
)

print()
print("renewal price — these must stay silent")
print("-" * 72)

check_renewal("Vultr", "Cloud Compute High Frequency $2.50 /mo Deploy Now", "$2.50", None, "USD")
check_renewal("Civo", "Extra Large $5.43 /mo 8 GB RAM 40 GB disk", "$5.43", None, "USD")
check_renewal("netcup", "VPS 1000 G11 €5.91 /month 4 GB RAM", "€5.91", None, "EUR")

# The nearest following sentence in a plan table often belongs to the next plan
# down. Here $2.09 is the entry plan and the "Renews at" claim sits two plans
# later, so attributing it to $2.09 would be wrong.
check_renewal(
    "trap next-plan-down",
    "$ 2.09 /mo For 24 month term $ 4.18 /mo For 24 month term Renews at $ 9.35 /mo",
    "$ 2.09", None, "USD",
)
# A renewal below the advertised figure is not a caveat, and the site does not
# invent a warning where there is nothing to warn about.
check_renewal(
    "trap lower renewal",
    "$ 9.99 /mo Special price Renews at $ 7.50 /mo",
    "$ 9.99", None, "USD",
)
# Never compare across currencies, not even to warn.
check_renewal(
    "trap cross-currency",
    "$ 9.99 /mo Renews at €12.99 /mo",
    "$ 9.99", None, "USD",
)

print()
print("-" * 72)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for label in FAIL:
        print(f"  failed: {label}")
    raise SystemExit(1)
