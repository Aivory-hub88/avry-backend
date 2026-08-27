"""
Canonical subscription-tier vocabulary — the one place tier names are defined.

Before this module the codebase carried five different tier vocabularies at
once (`foundation/pro/enterprise` in the gates, `foundation/acceleration/
intelligence` in the payments catalogue, `free/snapshot/blueprint/enterprise`
on the marketing site, `free/builder/operator/enterprise` in the dashboard
types, and `Operational/Business/Enterprise` in the copy shown to customers).
Every gate re-declared its own `_TIER_ORDER`, so they drifted apart and the
ladders disagreed about which plan outranked which.

The canonical ids are the 2026 pricing-rebrand names, and they are the SAME
strings used end to end: the marketing site's product id, the id avry-payments
prices the Midtrans transaction from, the id granted in
`routes/entitlements.py`, and the value stored in `identity.user_tiers.tier`.

    operational  — $39/mo
    business     — $99/mo
    enterprise   — sales-assisted, no self-serve price

Pre-rebrand ids are accepted on read (see `ALIASES`) so rows written by the
old payments flow keep resolving. Nothing new should be authored with them.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

#: Tier ids in ascending order of entitlement.
CANONICAL_TIERS: Tuple[str, ...] = ("operational", "business", "enterprise")

#: Pre-rebrand ids -> canonical id. Read-only compatibility.
ALIASES: Dict[str, str] = {
    "foundation": "operational",
    "pro": "business",
    "acceleration": "business",
    "intelligence": "enterprise",
}

#: Rank used by every `>=` gate. Higher outranks lower.
TIER_ORDER: Dict[str, int] = {name: i for i, name in enumerate(CANONICAL_TIERS)}

#: The base paid tier. `load_user_record` falls back to this when a user has no
#: entitlement row, so it is also what an unrecognised value resolves to.
BASE_TIER = "operational"

#: Customer-facing labels. Every surface that shows a plan name reads these, so
#: the invoice, the pricing page, and the dashboard cannot disagree.
DISPLAY_NAMES: Dict[str, str] = {
    "operational": "Operational",
    "business": "Business",
    "enterprise": "Enterprise",
}

#: Published monthly price in USD. `enterprise` is absent on purpose: it is
#: sales-assisted and no figure is published, so nothing may charge for it
#: self-serve. Mirrors `FIXED_PRICES_USD` in avry-payments' pricing.py.
MONTHLY_PRICE_USD: Dict[str, int] = {
    "operational": 39,
    "business": 99,
}

#: Monthly Intelligence Credit allowance per tier.
TIER_ALLOWANCES: Dict[str, int] = {
    "operational": 80,
    "business": 220,
    "enterprise": 3000,
}


#: The value used when an account holds no live subscription: it never paid,
#: or its entitlement has lapsed. Ranks BELOW every paid tier.
FREE_TIER = "free"

#: Rank returned for `FREE_TIER` and for anything unrecognised. Deliberately
#: negative: `operational` is rung 0, so a non-negative fallback would make
#: "at least operational" true for everyone. That is exactly the bug this
#: constant exists to prevent — see `rank()`.
_UNRANKED = -1


def normalise(tier: Optional[str]) -> str:
    """
    Map any tier string (canonical, legacy alias, cased, or empty) onto a
    canonical id, falling back to `BASE_TIER`.

    This preserves the pre-existing fallback semantics exactly: an empty or
    unrecognised tier resolves to the base tier rather than to a non-paid
    value. Callers that need to distinguish "genuinely unrecognised" from
    "explicitly the base tier" should use `is_known` first.
    """
    if not tier:
        return BASE_TIER
    key = str(tier).strip().lower()
    key = ALIASES.get(key, key)
    return key if key in TIER_ORDER else BASE_TIER


def is_known(tier: Optional[str]) -> bool:
    """True when `tier` names a canonical tier or a legacy alias of one."""
    if not tier:
        return False
    key = str(tier).strip().lower()
    return key in TIER_ORDER or key in ALIASES


def rank(tier: Optional[str]) -> int:
    """
    Ladder position of `tier`, resolving legacy aliases first.

    Returns `_UNRANKED` (-1) for `free`, for an empty value, and for anything
    unrecognised — deliberately NOT `normalise()`'s base-tier fallback. Routing
    unknown values to the base tier is what made `meets(tier, "operational")`
    true for every caller, since the base tier is rung 0.
    """
    if not tier:
        return _UNRANKED
    key = str(tier).strip().lower()
    key = ALIASES.get(key, key)
    return TIER_ORDER.get(key, _UNRANKED)


def account_tier(tier: Optional[str]) -> str:
    """
    The tier to gate on for an account.

    Canonical tier when the account holds a live paid plan, otherwise
    `FREE_TIER`. Loaders use this for a missing or lapsed entitlement row,
    which previously resolved to the base PAID tier and therefore handed
    non-paying and lapsed accounts the paid feature set.
    """
    position = rank(tier)
    return FREE_TIER if position < 0 else CANONICAL_TIERS[position]


def meets(tier: Optional[str], minimum: str) -> bool:
    """True when `tier` is at least `minimum` on the ladder."""
    return rank(tier) >= rank(minimum)


def display_name(tier: Optional[str]) -> str:
    """Customer-facing label for `tier`, or "Free" when there is no plan."""
    resolved = account_tier(tier)
    return "Free" if resolved == FREE_TIER else DISPLAY_NAMES[resolved]
