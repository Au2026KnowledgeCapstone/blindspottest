"""The tiny bit of web framework the demo app needs.

Request, Response, a routing table with `<param>` placeholders, and the notice
registry the flows redirect through. Kept separate from `app.py` so flow
modules can import these types without importing the server that imports them.

Deliberately stdlib-only: BlindSpot is the thing under study, and the demo it
is pointed at should not drag in a web framework whose behaviour a reader then
has to account for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import store


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------


@dataclass
class Response:
    body: bytes = b""
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)


def html(markup: str, status: int = 200) -> Response:
    return Response(body=markup.encode("utf-8"), status=status)


def redirect(location: str, status: int = 303) -> Response:
    """Post/Redirect/Get. A reload after a commit must re-read stored state
    rather than re-submit the form, or every persistence check is meaningless."""
    return Response(status=status, headers=[("Location", location)])


def not_found(message: str = "Not found") -> Response:
    return Response(body=message.encode(), status=404, content_type="text/plain")


# --------------------------------------------------------------------------
# Notices
#
# Flows redirect to `?notice=<key>`; the wording lives here so the sound and
# broken builds cannot drift apart in what they say.
# --------------------------------------------------------------------------

NOTICES: dict[str, tuple[str, str]] = {
    "saved": ("ok", "Saved successfully"),
    "project-created": ("ok", "Project created"),
    "project-updated": ("ok", "Project updated"),
    "project-deleted": ("ok", "Project deleted"),
    "cart-updated": ("ok", "Cart updated"),
    "cart-empty": ("warn", "Your cart is empty."),
    "signed-in": ("ok", "Signed in successfully"),
    "signed-out": ("ok", "You have been signed out"),
    "sign-in-required": ("warn", "Please sign in to continue."),
    "reset": ("ok", "Demo data reset"),
    "order-placed": ("ok", "Order placed"),
    "checkout-expired": ("warn", "Your checkout session has expired."),
}


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


@dataclass
class Request:
    method: str
    path: str                     # as requested, including any /broken prefix
    route: str                    # path with the variant prefix removed
    prefix: str                   # "" for the sound build, "/broken" otherwise
    variant: str                  # "sound" | "broken"
    session_id: str
    query: dict[str, list[str]] = field(default_factory=dict)
    form: dict[str, list[str]] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)

    @property
    def broken(self) -> bool:
        return self.variant == "broken"

    @property
    def state(self) -> dict:
        return store.state_for(self.session_id, self.variant)

    def url(self, path: str, **query: str) -> str:
        """Build a URL inside the current variant."""
        target = self.prefix + path if path != "/" else (self.prefix or "/")
        if query:
            parts = "&".join(f"{k}={v}" for k, v in query.items() if v is not None)
            if parts:
                target = f"{target}?{parts}"
        return target

    def get(self, name: str, default: str = "") -> str:
        """A submitted form value, falling back to the query string."""
        if name in self.form:
            return self.form[name][0]
        if name in self.query:
            return self.query[name][0]
        return default

    @property
    def flashes(self) -> list[tuple[str, str]]:
        out = []
        for key in self.query.get("notice", []):
            if key in NOTICES:
                out.append(NOTICES[key])
        # The original demo pages redirect to ?saved=1; keep that working so
        # existing scan records and Makefile targets stay comparable.
        if "saved" in self.query and not out:
            out.append(NOTICES["saved"])
        return out


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

Handler = Callable[[Request], Response]

_PARAM = re.compile(r"<([a-z_]+)>")


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[re.Pattern, frozenset[str], Handler]] = []

    def add(self, template: str, handler: Handler, methods=("GET",)) -> None:
        pattern = "^" + _PARAM.sub(r"(?P<\1>[^/]+)", template) + "$"
        self._routes.append(
            (re.compile(pattern), frozenset(m.upper() for m in methods), handler)
        )

    def match(self, method: str, path: str):
        """Return (handler, params), or (None, None) / (None, 405)."""
        method_mismatch = False
        for pattern, methods, handler in self._routes:
            found = pattern.match(path)
            if not found:
                continue
            if method.upper() not in methods:
                method_mismatch = True
                continue
            return handler, found.groupdict()
        return None, (405 if method_mismatch else None)
