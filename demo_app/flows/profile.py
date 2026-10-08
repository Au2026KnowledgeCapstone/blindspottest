"""Settings flow — PERSISTENT_MUTATION.

The original three-page demo, kept intact:

  /profile           persistence works
  /broken/profile    same UI, same "Saved successfully", Bio is never written
  /project-settings  different vocabulary ("Project title", "Update Project"),
                     same underlying invariant

Page C is the one that matters for the argument. Without it a reasonable
reader concludes BlindSpot is a hardcoded Bio test; with it the system has to
recognize a persistent mutation it was never told about.

`/profile-broken` still resolves — it is the URL in every existing run record
and Makefile target — and serves exactly what `/broken/profile` serves.
"""

from __future__ import annotations

from .. import views
from ..web import Request, Response, html, redirect


def register(router) -> None:
    router.add("/profile", profile, methods=("GET", "POST"))
    router.add("/project-settings", project_settings, methods=("GET", "POST"))


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------


def profile(req: Request) -> Response:
    data = req.state["profile"]

    if req.method == "POST":
        data["name"] = req.get("name")
        if not req.broken:
            data["bio"] = req.get("bio")
        # Broken build: a refactor moved the persistence calls around and the
        # `bio` assignment was dropped. The handler still completes, still
        # redirects, and the page still renders "Saved successfully". Name
        # saves fine, which is what makes this hard to spot by hand — the
        # feature looks like it works.
        return redirect(f"{req.path}?saved=1")

    body = views.card(
        f'<form method="POST" action="{views.esc(req.path)}">'
        + views.text_field("name", "Name", data["name"])
        + views.textarea_field("bio", "Bio", data["bio"],
                               hint="Shown on your public profile.")
        + '<div class="actions">'
        + views.button("Save")
        + "</div></form>"
    )
    return html(views.page(req, title="Profile", active="/profile", body=body,
                           subtitle="How you appear to other people in this workspace."))


# --------------------------------------------------------------------------
# Project settings — same invariant, none of the same words
# --------------------------------------------------------------------------


def project_settings(req: Request) -> Response:
    data = req.state["project_settings"]

    if req.method == "POST":
        data["title"] = req.get("title")
        data["description"] = req.get("description")
        return redirect(f"{req.path}?saved=1")

    body = views.card(
        f'<form method="POST" action="{views.esc(req.path)}">'
        + views.text_field("title", "Project title", data["title"])
        + views.textarea_field("description", "Description", data["description"])
        + '<div class="actions">'
        + views.button("Update Project")
        + "</div></form>"
    )
    return html(
        views.page(req, title="Project Settings", active="/project-settings",
                   body=body, subtitle="Details shown on the project overview.")
    )
