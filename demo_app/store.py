"""State for the demo app.

Everything lives in memory. Two properties matter for BlindSpot:

  * State is per browser session (a cookie), so two scans running at once
    never see each other's writes, and a scan can always start from a known
    baseline by clearing its cookie or POSTing to /reset.

  * Each session holds two independent copies of the data, one per variant
    ("sound" and "broken"). Deleting a project in the broken build must not
    change what the sound build shows, or the two stop being comparable.

Money is in integer cents throughout. A demo that reports a wrong total
because of binary floating point would be a fake finding, and telling a real
one from a fake one is the whole job here.
"""

from __future__ import annotations

import copy
import secrets
import threading
import time

VARIANTS = ("sound", "broken")

SESSION_COOKIE = "bs_session"
SESSION_TTL_SECONDS = 60 * 60
MAX_SESSIONS = 200


# --------------------------------------------------------------------------
# Catalog — read-only, so it is shared rather than copied per session.
# The catalog flow only queries (search / filter / sort); nothing writes here.
# --------------------------------------------------------------------------

PRODUCTS = [
    # id, name, category, price in cents, rating, in stock
    ("p-101", "Aurora Wireless Headphones", "Audio", 24900, 4.6, True),
    ("p-102", "Aurora Earbuds Mini", "Audio", 8900, 4.1, True),
    ("p-103", "Studio Desk Microphone", "Audio", 12900, 4.4, False),
    ("p-104", "Tempo Portable Speaker", "Audio", 6400, 3.9, True),
    ("p-201", "Meridian 14 Laptop", "Computing", 129900, 4.8, True),
    ("p-202", "Meridian Dock Pro", "Computing", 19900, 4.2, True),
    ("p-203", "Mechanical Keyboard K2", "Computing", 10900, 4.5, True),
    ("p-204", "27-inch 4K Monitor", "Computing", 42900, 4.3, False),
    ("p-301", "Filter Coffee Machine", "Home", 7900, 4.0, True),
    ("p-302", "Ceramic Table Lamp", "Home", 5400, 3.7, True),
    ("p-401", "Trailhead Daypack 22L", "Outdoor", 8400, 4.7, True),
    ("p-402", "All-Weather Jacket", "Outdoor", 17900, 4.2, True),
]

CATALOG = [
    {
        "id": pid,
        "name": name,
        "category": category,
        "price_cents": price,
        "rating": rating,
        "in_stock": stock,
    }
    for pid, name, category, price, rating, stock in PRODUCTS
]

CATALOG_BY_ID = {item["id"]: item for item in CATALOG}

CATEGORIES = sorted({item["category"] for item in CATALOG})

PROTECTION_PLAN_CENTS = 2900
EXPRESS_SHIPPING_CENTS = 1200
# Above this subtotal the checkout offers the protection plan, which is what
# makes the observed path vary between two equally valid runs.
UPSELL_THRESHOLD_CENTS = 15000


# --------------------------------------------------------------------------
# Seed state — deep-copied into every session, once per variant.
# --------------------------------------------------------------------------

SEED: dict = {
    "profile": {"name": "Stevie", "bio": "Hello"},
    "project_settings": {
        "title": "My Project",
        "description": "Internal tooling.",
    },
    "account": {
        "signed_in": False,
        "username": "",
        "email": "stevie@example.com",
        "display_name": "Stevie Rajesh",
        "signed_out_notice": False,
    },
    "projects": [
        {
            "id": "1",
            "name": "Billing rewrite",
            "description": "Move invoicing off the legacy job runner.",
            "owner": "Stevie",
            "status": "Active",
            "archived": False,
        },
        {
            "id": "2",
            "name": "Onboarding checklist",
            "description": "Guided setup for new workspaces.",
            "owner": "Ravi",
            "status": "Active",
            "archived": False,
        },
        {
            "id": "3",
            "name": "Search relevance",
            "description": "Tune ranking for the catalog search box.",
            "owner": "Mei",
            "status": "Paused",
            "archived": False,
        },
    ],
    "next_project_id": 4,
    "cart": [
        {"product_id": "p-101", "quantity": 1},
        {"product_id": "p-203", "quantity": 2},
    ],
    "checkout": {
        "contact_name": "",
        "contact_email": "",
        "address": "",
        "city": "",
        "postcode": "",
        "shipping_method": "standard",
        "protection_plan": False,
        # Whether this checkout was offered the upsell step. Recorded when the
        # branch is taken, so the confirmation can show the path the order
        # actually went through rather than re-deriving it from a cart that
        # has since been emptied.
        "upsell_shown": False,
        "card_name": "",
        "card_number": "",
    },
    "last_order": None,
    "next_order_number": 1041,
}

OWNERS = ["Stevie", "Ravi", "Mei", "Unassigned"]
STATUSES = ["Active", "Paused", "Archived"]


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

_LOCK = threading.Lock()
_SESSIONS: dict[str, dict] = {}
_SEEN: dict[str, float] = {}


def new_session_id() -> str:
    return secrets.token_urlsafe(9)


def _fresh() -> dict:
    return {variant: copy.deepcopy(SEED) for variant in VARIANTS}


def _evict_locked(now: float) -> None:
    """Drop idle sessions. A long-lived demo process shouldn't leak memory."""
    stale = [sid for sid, seen in _SEEN.items() if now - seen > SESSION_TTL_SECONDS]
    for sid in stale:
        _SESSIONS.pop(sid, None)
        _SEEN.pop(sid, None)
    while len(_SESSIONS) > MAX_SESSIONS:
        oldest = min(_SEEN, key=_SEEN.get)
        _SESSIONS.pop(oldest, None)
        _SEEN.pop(oldest, None)


def state_for(session_id: str, variant: str) -> dict:
    """The data this session sees in this variant, created on first touch."""
    now = time.time()
    with _LOCK:
        _evict_locked(now)
        session = _SESSIONS.get(session_id)
        if session is None:
            session = _SESSIONS[session_id] = _fresh()
        _SEEN[session_id] = now
    return session[variant]


def reset(session_id: str) -> None:
    """Return one session to the seed, in both variants."""
    with _LOCK:
        _SESSIONS[session_id] = _fresh()
        _SEEN[session_id] = time.time()


def reset_all() -> None:
    with _LOCK:
        _SESSIONS.clear()
        _SEEN.clear()


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def cart_lines(state: dict) -> list[dict]:
    """Cart entries joined to the catalog, with per-line totals."""
    lines = []
    for entry in state["cart"]:
        product = CATALOG_BY_ID.get(entry["product_id"])
        if product is None:
            continue
        quantity = max(0, int(entry["quantity"]))
        lines.append(
            {
                "product": product,
                "quantity": quantity,
                "line_cents": product["price_cents"] * quantity,
            }
        )
    return lines


def subtotal_cents(state: dict) -> int:
    return sum(line["line_cents"] for line in cart_lines(state))


def shipping_cents(state: dict) -> int:
    method = state["checkout"].get("shipping_method", "standard")
    return EXPRESS_SHIPPING_CENTS if method == "express" else 0


def live_projects(state: dict) -> list[dict]:
    return [p for p in state["projects"] if not p["archived"]]


def find_project(state: dict, project_id: str) -> dict | None:
    for project in state["projects"]:
        if project["id"] == project_id:
            return project
    return None
