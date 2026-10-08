"""Demo application for BlindSpot.

A small but complete web app — sign-in, project CRUD, a searchable catalog,
and a multi-step checkout — rather than a single form. Flows are what BlindSpot
is growing towards testing, and a flow needs somewhere to go.

Everything is mounted twice from the same code:

    /            the sound build
    /broken/...  the same application with exactly one behaviour changed

`demo_app.manifest` records, per flow, what the capability is, what has to
hold at the end of it, and what the planted defect does. The pages themselves
are byte-identical between the two builds except for the defect, so finding
one is a testing problem and not a reading problem.

Run:  python -m demo_app.app [port] [--open]
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

from . import manifest, store, views
from .flows import register_all
from .web import Request, Response, Router, html, not_found, redirect

VARIANT_PREFIXES = {"": "sound", "/broken": "broken"}


# --------------------------------------------------------------------------
# Routing table
# --------------------------------------------------------------------------


@dataclass
class Target:
    """A matched route, plus which build it belongs to."""

    handler: Callable[[Request], Response]
    variant: str
    prefix: str


class Mount:
    """Registers a flow's routes under one variant prefix."""

    def __init__(self, router: Router, prefix: str, variant: str):
        self._router = router
        self._prefix = prefix
        self._variant = variant

    def add(self, template: str, handler, methods=("GET",)) -> None:
        self._router.add(
            self._prefix + template,
            Target(handler, self._variant, self._prefix),
            methods,
        )


def build_router() -> Router:
    router = Router()

    for prefix, variant in VARIANT_PREFIXES.items():
        mount = Mount(router, prefix, variant)
        mount.add("/", overview)
        mount.add("/reset", reset, methods=("POST",))
        register_all(mount)

    # `/broken` with no trailing slash, so the sidebar's brand link works.
    router.add("/broken", Target(overview, "broken", "/broken"))

    # The URL in every existing run record and Makefile target. It serves the
    # broken profile page, which is what it has always served.
    from .flows.profile import profile

    router.add("/profile-broken", Target(profile, "broken", "/broken"),
               ("GET", "POST"))

    router.add("/static/app.css", Target(stylesheet, "sound", ""))
    router.add("/__truth", Target(truth, "sound", ""))

    return router


# --------------------------------------------------------------------------
# App-level pages
# --------------------------------------------------------------------------

FLOW_CARDS = [
    ("Projects", "/projects",
     "Create, edit and delete the things this workspace works on."),
    ("Catalog", "/catalog",
     "Search, filter and sort a product collection."),
    ("Cart", "/cart",
     "Checkout, in four steps or five depending on what you are buying."),
    ("Account", "/account",
     "Sign in, update your contact details, sign out."),
    ("Profile", "/profile",
     "A single form whose value has to survive a reload."),
    ("Project settings", "/project-settings",
     "The same invariant as Profile, in different words."),
]


def overview(req: Request) -> Response:
    cards = "".join(
        views.card(
            f'<h2><a href="{views.esc(req.url(path))}">{views.esc(name)}</a></h2>'
            f"<p>{views.esc(description)}</p>"
        )
        for name, path, description in FLOW_CARDS
    )

    builds = views.card(
        "<p>This application is served twice from the same code. "
        "<code>/</code> is the sound build; <code>/broken</code> is the same "
        "application with one behaviour changed per flow. "
        "<code>/__truth</code> lists every flow, what has to hold at the end "
        "of it, and which build breaks it.</p>"
        f'<div class="actions">'
        f'{views.link_button("/" if req.broken else "/broken", "Switch to the " + ("sound" if req.broken else "broken") + " build")}'
        f"</div>",
        title="Builds",
    )

    reset_card = views.card(
        f'<form method="POST" action="{views.esc(req.url("/reset"))}">'
        "<p>Put this session's data back to its starting state — both builds, "
        "every flow.</p>"
        f'<div class="actions">'
        f'{views.button("Reset demo data", kind="secondary")}</div></form>',
        title="Demo data",
    )

    return html(
        views.page(req, title="Overview", active="/",
                   body=f'<div class="grid">{cards}</div>' + builds + reset_card,
                   subtitle="A deliberately ordinary application, used as a "
                            "target for BlindSpot.")
    )


def reset(req: Request) -> Response:
    store.reset(req.session_id)
    return redirect(req.url("/", notice="reset"))


def stylesheet(req: Request) -> Response:
    return Response(body=views.CSS.encode(), content_type="text/css; charset=utf-8")


def truth(req: Request) -> Response:
    return Response(
        body=json.dumps(manifest.as_dict(), indent=2).encode(),
        content_type="application/json",
    )


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

ROUTER = build_router()


class Handler(BaseHTTPRequestHandler):
    server_version = "AcmeConsole/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # -- plumbing ----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        target, params = ROUTER.match(method, path)
        if target is None:
            if params == 405:
                self._send(Response(b"Method not allowed", status=405,
                                    content_type="text/plain"))
            else:
                self._send(not_found())
            return

        session_id, is_new = self._session()
        form: dict[str, list[str]] = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                form = parse_qs(self.rfile.read(length).decode("utf-8"))

        request = Request(
            method=method,
            path=path,
            route=path[len(target.prefix):] or "/",
            prefix=target.prefix,
            variant=target.variant,
            session_id=session_id,
            query=parse_qs(parsed.query),
            form=form,
            params=params or {},
        )

        try:
            response = target.handler(request)
        except Exception as exc:  # pragma: no cover - demo convenience
            response = Response(
                body=f"500: {exc}".encode(), status=500,
                content_type="text/plain",
            )

        if is_new:
            response.headers.append(
                (
                    "Set-Cookie",
                    f"{store.SESSION_COOKIE}={session_id}; Path=/; "
                    "HttpOnly; SameSite=Lax",
                )
            )
        self._send(response)

    def _session(self) -> tuple[str, bool]:
        """The session this request belongs to; minted on first contact.

        State is per session so that two scans running at once never see each
        other's writes.
        """
        raw = self.headers.get("Cookie")
        if raw:
            jar = SimpleCookie()
            try:
                jar.load(raw)
            except Exception:
                jar = SimpleCookie()
            morsel = jar.get(store.SESSION_COOKIE)
            if morsel and morsel.value:
                return morsel.value, False
        return store.new_session_id(), True

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        if response.body:
            self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        # Every page is state-dependent; a cached one would make a reload stop
        # re-reading the server, which is the one thing a persistence check
        # depends on.
        self.send_header("Cache-Control", "no-store")
        for key, value in response.headers:
            self.send_header(key, value)
        self.end_headers()
        if response.body and self.command != "HEAD":
            self.wfile.write(response.body)


def serve(port: int = 3000, background: bool = False) -> ThreadingHTTPServer:
    # Threaded: a browser opens several connections at once, and a
    # single-threaded server makes every page load wait on the one before it.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if background:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

LABELS = {
    "/": "Overview — every flow, both builds",
    "/profile": "Persistence works",
    "/broken/profile": "Says 'Saved successfully', drops Bio",
    "/project-settings": "Different wording, same invariant",
    "/projects": "Create / update / delete a resource",
    "/broken/projects": "Delete leaves the resource reachable",
    "/catalog": "Search, filter, sort",
    "/broken/catalog?sort=price-asc": "Price sorts as text",
    "/cart": "Multi-step checkout (four or five steps)",
    "/broken/cart": "Protection plan is listed but not charged",
    "/login": "Sign in — demo / demo123",
    "/broken/account": "Sign out does not end the session",
    "/__truth": "Ground truth for every flow (JSON)",
}


def main(argv=None) -> int:
    import sys

    argv = sys.argv[1:] if argv is None else argv
    positional = [a for a in argv if not a.startswith("-")]
    port = int(positional[0]) if positional else 3000
    open_browser = "--open" in argv

    server = serve(port, background=open_browser)
    base = f"http://127.0.0.1:{port}"

    print(f"\nBlindSpot demo app — {base}\n")
    width = max(len(path) for path in LABELS)
    for path, label in LABELS.items():
        print(f"  {base}{path:<{width}}  {label}")
    print("\nCtrl-C to stop.\n")

    if open_browser:
        import webbrowser

        webbrowser.open(base + "/")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    else:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
