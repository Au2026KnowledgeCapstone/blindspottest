"""Catalog flow — SEARCH / FILTER_COLLECTION / SORT_COLLECTION.

  /catalog?q=&category=&sort=&in_stock=

A read-only flow, which makes it the cleanest place to ask what a flow-level
invariant even is. Nothing is written, so "did it save" is meaningless; the
conditions that hold are about the *result set*:

  * every row matches the active query and filters
  * the row count and the stated count agree
  * the chosen sort order actually holds across the rows
  * clearing the filters returns the full collection

Planted defect in the broken build: the price sort orders by the rendered
price string rather than the number, so "$1,299.00" sorts before "$89.00".
The results are complete and correctly filtered; only the order is wrong.
This one is invisible unless you check a relation *between* rows, which is
why it is here.
"""

from __future__ import annotations

from .. import store, views
from ..web import Request, Response, html

SORTS = [
    ("name-asc", "Name (A–Z)"),
    ("price-asc", "Price (low to high)"),
    ("price-desc", "Price (high to low)"),
    ("rating-desc", "Rating (best first)"),
]
SORT_KEYS = {key for key, _ in SORTS}

CATEGORY_OPTIONS = [("", "All categories")] + [(c, c) for c in store.CATEGORIES]


def register(router) -> None:
    router.add("/catalog", catalog)


def _filtered(items, query: str, category: str, in_stock_only: bool):
    needle = query.strip().lower()
    out = []
    for item in items:
        if needle and needle not in item["name"].lower():
            continue
        if category and item["category"] != category:
            continue
        if in_stock_only and not item["in_stock"]:
            continue
        out.append(item)
    return out


def _sorted(items, sort: str, *, broken: bool):
    if sort == "rating-desc":
        return sorted(items, key=lambda i: -i["rating"])
    if sort in ("price-asc", "price-desc"):
        if broken:
            # Sorting the formatted value instead of the number. Every row is
            # correct; the order is not.
            key = lambda i: store.money(i["price_cents"])  # noqa: E731
        else:
            key = lambda i: i["price_cents"]  # noqa: E731
        return sorted(items, key=key, reverse=(sort == "price-desc"))
    return sorted(items, key=lambda i: i["name"].lower())


def catalog(req: Request) -> Response:
    query = req.get("q")
    category = req.get("category")
    sort = req.get("sort", "name-asc")
    if sort not in SORT_KEYS:
        sort = "name-asc"
    in_stock_only = bool(req.query.get("in_stock"))

    results = _sorted(
        _filtered(store.CATALOG, query, category, in_stock_only),
        sort,
        broken=req.broken,
    )

    filters = (
        f'<form method="GET" action="{views.esc(req.url("/catalog"))}">'
        '<div class="filters">'
        "<div>"
        + views.text_field("q", "Search products", query)
        + "</div><div>"
        + views.select_field("category", "Category", CATEGORY_OPTIONS, category)
        + "</div><div>"
        + views.select_field("sort", "Sort by", SORTS, sort)
        + "</div></div>"
        '<div class="actions">'
        f'<label for="in_stock" style="font-weight:400;margin:0">'
        f'<input type="checkbox" id="in_stock" name="in_stock" value="1"'
        f'{" checked" if in_stock_only else ""}> In stock only</label>'
        "</div>"
        '<div class="actions">'
        + views.button("Apply filters")
        + views.link_button(req.url("/catalog"), "Clear filters")
        + "</div></form>"
    )

    if results:
        rows = "".join(
            "<tr>"
            f'<td>{views.esc(item["name"])}</td>'
            f'<td>{views.esc(item["category"])}</td>'
            f'<td class="num">{views.esc(store.money(item["price_cents"]))}</td>'
            f'<td class="num">{item["rating"]:.1f}</td>'
            f'<td>{"In stock" if item["in_stock"] else "Out of stock"}</td>'
            "</tr>"
            for item in results
        )
        table = (
            "<table><thead><tr><th>Product</th><th>Category</th>"
            '<th class="num">Price</th><th class="num">Rating</th>'
            "<th>Availability</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        table = '<p class="empty">No products match those filters.</p>'

    count = (
        f"<p>Showing {len(results)} of {len(store.CATALOG)} products</p>"
    )

    return html(
        views.page(req, title="Catalog", active="/catalog",
                   body=views.card(filters) + views.card(count + table),
                   subtitle="Search, filter and sort the product collection.")
    )
