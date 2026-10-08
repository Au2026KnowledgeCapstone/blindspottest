"""Account flow — AUTHENTICATE.

  /login     sign in with demo / demo123
  /account   protected: signing out must make it unreachable again
             (it also holds an ordinary persistent field, so the account page
             is a persistence target that happens to sit behind a gate)

Planted defect in the broken build: signing out renders the confirmation and
returns you to the sign-in page, but the session is never cleared. The path is
identical, every observable step succeeds, and /broken/account still serves the
account. The goal condition — the protected resource is no longer reachable —
is the only thing that fails.
"""

from __future__ import annotations

from .. import views
from ..web import Request, Response, html, redirect

USERNAME = "demo"
PASSWORD = "demo123"


def register(router) -> None:
    router.add("/login", login, methods=("GET", "POST"))
    router.add("/account", account, methods=("GET", "POST"))
    router.add("/logout", logout, methods=("POST",))


def login(req: Request) -> Response:
    account = req.state["account"]
    error = ""

    if req.method == "POST":
        username = req.get("username").strip()
        password = req.get("password")
        if username == USERNAME and password == PASSWORD:
            account["signed_in"] = True
            account["username"] = username
            return redirect(req.url("/account", notice="signed-in"))
        error = "Incorrect username or password."

    banner = (
        f'<p class="flash bad" role="alert">{views.esc(error)}</p>' if error else ""
    )
    body = banner + views.card(
        f'<form method="POST" action="{views.esc(req.path)}">'
        + views.text_field("username", "Username", req.get("username"),
                           autocomplete="username")
        + views.text_field("password", "Password", type_="password",
                           autocomplete="current-password",
                           hint="Demo credentials: demo / demo123")
        + '<div class="actions">'
        + views.button("Sign in")
        + "</div></form>"
    )
    return html(views.page(req, title="Sign in", active="/account", body=body,
                           subtitle="Sign in to manage your account."))


def account(req: Request) -> Response:
    account = req.state["account"]

    if not account["signed_in"]:
        return redirect(req.url("/login", notice="sign-in-required"))

    if req.method == "POST":
        account["display_name"] = req.get("display_name")
        account["email"] = req.get("email")
        return redirect(req.url("/account", notice="saved"))

    details = views.card(
        f'<form method="POST" action="{views.esc(req.path)}">'
        + views.text_field("display_name", "Display name", account["display_name"])
        + views.text_field("email", "Email address", account["email"],
                           type_="email", autocomplete="email")
        + '<div class="actions">'
        + views.button("Save contact details")
        + "</div></form>",
        title="Contact details",
    )

    session_card = views.card(
        f'<dl class="facts"><dt>Signed in as</dt><dd>{views.esc(account["username"])}</dd>'
        f"<dt>Session</dt><dd>Active</dd></dl>"
        f'<form method="POST" action="{views.esc(req.url("/logout"))}">'
        f'<div class="actions">{views.button("Sign out", kind="secondary")}</div>'
        f"</form>",
        title="Session",
    )

    return html(
        views.page(req, title="Account", active="/account",
                   body=details + session_card,
                   subtitle="Your sign-in and contact information.")
    )


def logout(req: Request) -> Response:
    account = req.state["account"]
    account["signed_out_notice"] = True
    if not req.broken:
        account["signed_in"] = False
        account["username"] = ""
    # Broken build: the notice is set and the redirect happens, but the session
    # flag above is never cleared. Nothing in the response says so.
    return redirect(req.url("/login", notice="signed-out"))
