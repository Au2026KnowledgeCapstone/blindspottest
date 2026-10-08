"""Checkout flow — the multi-step one the whole flow argument is about.

  /cart
    -> /checkout/contact
    -> /checkout/upsell        only when the subtotal clears $150
    -> /checkout/shipping
    -> /checkout/payment
    -> /checkout/confirmation

Two different runs take two different paths through this flow and both are
correct, which is the point: the observed path is not the specification. What
has to hold at the end is a goal condition —

  * the confirmation lists exactly what was in the cart, at the quantities
    that were in the cart;
  * the order total equals the sum of the lines it displays;
  * an order number exists.

Planted defect in the broken build: when the protection plan is added at the
upsell step it is listed on the summary and on the confirmation, but never
added to the total. Every step succeeds, the confirmation renders, the order
number is real — and the customer is charged $29.00 less than the order says.
A test that asserts "reached the confirmation page" passes happily.
"""

from __future__ import annotations

import copy

from .. import store, views
from ..web import Request, Response, html, redirect

STEP_NAMES = ["Cart", "Contact", "Upsell", "Shipping", "Payment", "Confirmation"]

SHIPPING_OPTIONS = [
    ("standard", "Standard delivery", "3–5 working days, free"),
    ("express", "Express delivery", "Next working day, $12.00"),
]


def register(router) -> None:
    router.add("/cart", cart, methods=("GET", "POST"))
    router.add("/checkout/contact", contact, methods=("GET", "POST"))
    router.add("/checkout/upsell", upsell, methods=("GET", "POST"))
    router.add("/checkout/shipping", shipping, methods=("GET", "POST"))
    router.add("/checkout/payment", payment, methods=("GET", "POST"))
    router.add("/checkout/confirmation", confirmation)
    router.add("/checkout/restart", restart, methods=("POST",))


# --------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------


def _summary(state: dict, *, broken: bool) -> dict:
    """Line items and totals for whatever is currently in the checkout."""
    lines = [
        (f"{line['product']['name']} × {line['quantity']}", line["line_cents"])
        for line in store.cart_lines(state)
    ]
    subtotal = sum(amount for _, amount in lines)

    extras = []
    protection = 0
    if state["checkout"]["protection_plan"]:
        protection = store.PROTECTION_PLAN_CENTS
        extras.append(("2-year protection plan", protection))

    ship = store.shipping_cents(state)
    extras.append(
        (
            "Express delivery" if ship else "Standard delivery",
            ship,
        )
    )

    # The broken build leaves the protection plan out of the arithmetic while
    # still showing it as a line above.
    total = subtotal + ship + (0 if broken else protection)

    return {
        "lines": lines,
        "extras": extras,
        "subtotal_cents": subtotal,
        "total_cents": total,
    }


def _summary_card(summary: dict, *, title: str = "Order summary") -> str:
    rows = [
        (label, store.money(amount))
        for label, amount in summary["lines"] + summary["extras"]
    ]
    return views.card(
        views.totals_table(rows, ("Total", store.money(summary["total_cents"]))),
        title=title,
    )


def _eligible_for_upsell(state: dict) -> bool:
    return store.subtotal_cents(state) >= store.UPSELL_THRESHOLD_CENTS


def _visible_steps(state: dict) -> list[str]:
    """The steps this particular checkout will actually show."""
    if _eligible_for_upsell(state):
        return STEP_NAMES
    return [name for name in STEP_NAMES if name != "Upsell"]


def _progress(state: dict, current: str) -> str:
    return views.steps(_visible_steps(state), current)


def _require_cart(req: Request):
    if not store.cart_lines(req.state):
        return redirect(req.url("/cart", notice="cart-empty"))
    return None


# --------------------------------------------------------------------------
# Cart
# --------------------------------------------------------------------------


def cart(req: Request) -> Response:
    state = req.state

    if req.method == "POST":
        removed = req.get("remove")
        if removed:
            state["cart"] = [e for e in state["cart"] if e["product_id"] != removed]
            return redirect(req.url("/cart", notice="cart-updated"))

        for entry in state["cart"]:
            raw = req.get(f"qty-{entry['product_id']}")
            if raw:
                try:
                    entry["quantity"] = max(0, int(raw))
                except ValueError:
                    pass
        state["cart"] = [e for e in state["cart"] if e["quantity"] > 0]

        if req.get("action") == "checkout" and store.cart_lines(state):
            return redirect(req.url("/checkout/contact"))
        return redirect(req.url("/cart", notice="cart-updated"))

    lines = store.cart_lines(state)
    if not lines:
        body = views.card(
            '<p class="empty">Your cart is empty.</p>'
            + views.link_button(req.url("/catalog"), "Browse the catalog", kind="")
        )
        return html(views.page(req, title="Cart", active="/cart", body=body))

    rows = "".join(
        "<tr>"
        f'<td>{views.esc(line["product"]["name"])}</td>'
        f'<td class="num">{views.esc(store.money(line["product"]["price_cents"]))}</td>'
        f'<td><input type="number" min="0" step="1"'
        f' id="qty-{views.esc(line["product"]["id"])}"'
        f' name="qty-{views.esc(line["product"]["id"])}"'
        f' value="{line["quantity"]}"'
        f' aria-label="Quantity for {views.esc(line["product"]["name"])}"></td>'
        f'<td class="num">{views.esc(store.money(line["line_cents"]))}</td>'
        # Named per row: two buttons both called "Remove" are ambiguous to
        # anything locating by accessible name, screen readers included.
        f'<td><button type="submit" class="secondary" name="remove"'
        f' value="{views.esc(line["product"]["id"])}" style="margin:0"'
        f' aria-label="Remove {views.esc(line["product"]["name"])}">'
        f"Remove</button></td>"
        "</tr>"
        for line in lines
    )

    form = (
        f'<form method="POST" action="{views.esc(req.url("/cart"))}">'
        "<table><thead><tr><th>Item</th><th class='num'>Price</th>"
        "<th>Quantity</th><th class='num'>Line total</th><th></th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p>Subtotal: <strong>"
        f"{views.esc(store.money(store.subtotal_cents(state)))}</strong></p>"
        '<div class="actions">'
        + views.button("Update cart", kind="secondary", name="action", value="update")
        + views.button("Continue to checkout", name="action", value="checkout")
        + "</div></form>"
    )

    return html(
        views.page(req, title="Cart", active="/cart",
                   body=_progress(state, "Cart") + views.card(form),
                   subtitle="Review your items before checking out.")
    )


# --------------------------------------------------------------------------
# Contact
# --------------------------------------------------------------------------


def contact(req: Request) -> Response:
    guard = _require_cart(req)
    if guard:
        return guard
    state = req.state
    checkout = state["checkout"]

    if req.method == "POST":
        checkout["contact_name"] = req.get("contact_name")
        checkout["contact_email"] = req.get("contact_email")
        # The branch that makes this flow interesting: the same application
        # produces a five-step path or a six-step one depending on the cart.
        checkout["upsell_shown"] = _eligible_for_upsell(state)
        if checkout["upsell_shown"]:
            return redirect(req.url("/checkout/upsell"))
        return redirect(req.url("/checkout/shipping"))

    body = views.card(
        f'<form method="POST" action="{views.esc(req.path)}">'
        + views.text_field("contact_name", "Full name", checkout["contact_name"],
                           required=True, autocomplete="name")
        + views.text_field("contact_email", "Email address",
                           checkout["contact_email"], type_="email",
                           required=True, autocomplete="email",
                           hint="Your receipt goes here.")
        + '<div class="actions">'
        + views.button("Continue")
        + views.link_button(req.url("/cart"), "Back to cart")
        + "</div></form>"
    )
    return html(
        views.page(req, title="Contact details", active="/cart",
                   body=_progress(state, "Contact") + body,
                   subtitle="Step 2 of checkout.")
    )


# --------------------------------------------------------------------------
# Upsell — the conditional step
# --------------------------------------------------------------------------


def upsell(req: Request) -> Response:
    guard = _require_cart(req)
    if guard:
        return guard
    state = req.state

    if req.method == "POST":
        state["checkout"]["protection_plan"] = req.get("action") == "add"
        return redirect(req.url("/checkout/shipping"))

    price = store.money(store.PROTECTION_PLAN_CENTS)
    body = views.card(
        f'<form method="POST" action="{views.esc(req.path)}">'
        f"<p>Add a 2-year protection plan for {views.esc(price)}. "
        "Accidental damage, wear and tear, and a replacement if we can't "
        "repair it.</p>"
        '<div class="actions">'
        + views.button("Add to order", name="action", value="add")
        + views.button("No thanks", kind="secondary", name="action", value="skip")
        + "</div></form>",
        title=f"Protect your order — {price}",
    )
    return html(
        views.page(req, title="Protection plan", active="/cart",
                   body=_progress(state, "Upsell") + body,
                   subtitle="Optional — you can skip this step.")
    )


# --------------------------------------------------------------------------
# Shipping
# --------------------------------------------------------------------------


def shipping(req: Request) -> Response:
    guard = _require_cart(req)
    if guard:
        return guard
    state = req.state
    checkout = state["checkout"]

    if req.method == "POST":
        checkout["address"] = req.get("address")
        checkout["city"] = req.get("city")
        checkout["postcode"] = req.get("postcode")
        checkout["shipping_method"] = req.get("shipping_method", "standard")
        return redirect(req.url("/checkout/payment"))

    body = views.card(
        f'<form method="POST" action="{views.esc(req.path)}">'
        + views.text_field("address", "Street address", checkout["address"],
                           required=True, autocomplete="street-address")
        + views.text_field("city", "City", checkout["city"], required=True)
        + views.text_field("postcode", "Postcode", checkout["postcode"],
                           required=True)
        + views.radio_group("shipping_method", "Delivery speed",
                            SHIPPING_OPTIONS, checkout["shipping_method"])
        + '<div class="actions">'
        + views.button("Continue to payment")
        + "</div></form>"
    )
    return html(
        views.page(req, title="Shipping", active="/cart",
                   body=_progress(state, "Shipping") + body,
                   subtitle="Where should this go?")
    )


# --------------------------------------------------------------------------
# Payment
# --------------------------------------------------------------------------


def payment(req: Request) -> Response:
    guard = _require_cart(req)
    if guard:
        return guard
    state = req.state
    checkout = state["checkout"]

    if req.method == "POST":
        checkout["card_name"] = req.get("card_name")
        checkout["card_number"] = req.get("card_number")

        summary = _summary(state, broken=req.broken)
        number = f"AC-{state['next_order_number']}"
        state["next_order_number"] += 1
        state["last_order"] = {
            "number": number,
            "lines": summary["lines"],
            "extras": summary["extras"],
            "subtotal_cents": summary["subtotal_cents"],
            "total_cents": summary["total_cents"],
            "contact_name": checkout["contact_name"],
            "contact_email": checkout["contact_email"],
            "address": ", ".join(
                part for part in
                (checkout["address"], checkout["city"], checkout["postcode"])
                if part
            ),
            "protection_plan": checkout["protection_plan"],
            "upsell_shown": checkout["upsell_shown"],
        }
        # The cart empties on purchase, as it should. /checkout/restart puts a
        # fresh one back, so the flow can be walked again without restarting
        # the server.
        state["cart"] = []
        return redirect(req.url("/checkout/confirmation", notice="order-placed"))

    body = views.card(
        f'<form method="POST" action="{views.esc(req.path)}">'
        + views.text_field("card_name", "Name on card", checkout["card_name"],
                           required=True, autocomplete="cc-name")
        + views.text_field("card_number", "Card number", checkout["card_number"],
                           required=True, autocomplete="cc-number",
                           hint="Test mode — no card is charged.")
        + '<div class="actions">'
        + views.button("Place order")
        + "</div></form>"
    )
    return html(
        views.page(req, title="Payment", active="/cart",
                   body=_progress(state, "Payment")
                   + _summary_card(_summary(state, broken=req.broken))
                   + body,
                   subtitle="Final step — review and pay.")
    )


# --------------------------------------------------------------------------
# Confirmation
# --------------------------------------------------------------------------


def confirmation(req: Request) -> Response:
    state = req.state
    order = state["last_order"]
    if order is None:
        return redirect(req.url("/cart", notice="checkout-expired"))

    rows = [
        (label, store.money(amount))
        for label, amount in order["lines"] + order["extras"]
    ]
    receipt = views.card(
        f"<dl class='facts'><dt>Order number</dt>"
        f"<dd>{views.esc(order['number'])}</dd>"
        f"<dt>Confirmation sent to</dt>"
        f"<dd>{views.esc(order['contact_email'] or '—')}</dd>"
        f"<dt>Delivering to</dt><dd>{views.esc(order['address'] or '—')}</dd></dl>",
        title="Thank you — your order is confirmed",
    )
    totals = views.card(
        views.totals_table(rows, ("Total charged",
                                  store.money(order["total_cents"]))),
        title="What you paid",
    )
    again = views.card(
        f'<form method="POST" action="{views.esc(req.url("/checkout/restart"))}">'
        "<p>Finished with this order.</p>"
        f'<div class="actions">{views.button("Start a new order")}</div></form>'
    )
    # The cart is empty by now, so the steps come from the order rather than
    # from the current state: this shows the path this order actually took.
    walked = STEP_NAMES if order.get("upsell_shown") else [
        name for name in STEP_NAMES if name != "Upsell"
    ]
    return html(
        views.page(req, title="Order confirmed", active="/cart",
                   body=views.steps(walked, "Confirmation")
                   + receipt + totals + again,
                   subtitle="A copy of this receipt has been emailed to you.")
    )


def restart(req: Request) -> Response:
    """Refill the cart and clear the checkout draft, without touching anything
    else in the session. Lets the flow be exercised repeatedly."""
    state = req.state
    state["cart"] = copy.deepcopy(store.SEED["cart"])
    state["checkout"] = copy.deepcopy(store.SEED["checkout"])
    state["last_order"] = None
    return redirect(req.url("/cart", notice="cart-updated"))
