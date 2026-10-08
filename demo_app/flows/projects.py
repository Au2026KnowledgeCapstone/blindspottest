"""Projects flow — CREATE_RESOURCE / UPDATE_RESOURCE / DELETE_RESOURCE.

  /projects              collection
  /projects/new          create  -> detail
  /projects/<id>         detail
  /projects/<id>/edit    update  -> detail
  /projects/<id>/delete  delete  -> collection

Planted defect in the broken build: delete is a soft delete. The row leaves the
list, the confirmation says "Project deleted", and every step of the path is
observably successful — but the resource is still served at its own URL, and
the collection's own count still includes it. Deleting is the one flow where
the goal condition is about something *not* existing, which is exactly the kind
of condition a path-shaped test never checks.

Deletion is a POST form, not a link with a JavaScript confirm. A modal dialog
would block a browser-driven agent entirely, and a destructive GET would let a
crawler wipe the data just by following links.
"""

from __future__ import annotations

from .. import store, views
from ..web import Request, Response, html, not_found, redirect

OWNER_OPTIONS = [(name, name) for name in store.OWNERS]
STATUS_OPTIONS = [(name, name) for name in store.STATUSES]


def register(router) -> None:
    router.add("/projects", collection)
    router.add("/projects/new", create, methods=("GET", "POST"))
    router.add("/projects/<pid>", detail)
    router.add("/projects/<pid>/edit", edit, methods=("GET", "POST"))
    router.add("/projects/<pid>/delete", delete, methods=("POST",))


def _badge(status: str) -> str:
    tone = {"Active": "active", "Paused": "paused"}.get(status, "")
    return f'<span class="badge {tone}">{views.esc(status)}</span>'


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


def collection(req: Request) -> Response:
    state = req.state
    visible = store.live_projects(state)

    # The header count is read from the stored collection. In the sound build
    # a deleted project is gone from it, so the two agree; in the broken build
    # the soft-deleted row is still counted here while the table no longer
    # shows it.
    total = len(state["projects"]) if req.broken else len(visible)

    if visible:
        rows = "".join(
            "<tr>"
            f'<td><a href="{views.esc(req.url("/projects/" + p["id"]))}">'
            f'{views.esc(p["name"])}</a></td>'
            f'<td>{views.esc(p["owner"])}</td>'
            f"<td>{_badge(p['status'])}</td>"
            f'<td>{views.esc(p["description"])}</td>'
            "</tr>"
            for p in visible
        )
        table = (
            "<table><thead><tr><th>Name</th><th>Owner</th><th>Status</th>"
            f"<th>Description</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        table = '<p class="empty">No projects yet.</p>'

    body = views.card(
        f"<p>{total} projects</p>{table}"
        f'<div class="actions">'
        f'{views.link_button(req.url("/projects/new"), "New project", kind="")}'
        f"</div>"
    )
    return html(views.page(req, title="Projects", active="/projects", body=body,
                           subtitle="Everything this workspace is working on."))


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


def create(req: Request) -> Response:
    state = req.state
    error = ""

    if req.method == "POST":
        name = req.get("name").strip()
        if not name:
            error = "Name is required."
        else:
            project_id = str(state["next_project_id"])
            state["next_project_id"] += 1
            state["projects"].append(
                {
                    "id": project_id,
                    "name": name,
                    "description": req.get("description"),
                    "owner": req.get("owner") or "Unassigned",
                    "status": "Active",
                    "archived": False,
                }
            )
            return redirect(
                req.url(f"/projects/{project_id}", notice="project-created")
            )

    banner = f'<p class="flash bad" role="alert">{views.esc(error)}</p>' if error else ""
    body = banner + views.card(
        f'<form method="POST" action="{views.esc(req.path)}">'
        + views.text_field("name", "Project name", req.get("name"), required=True)
        + views.textarea_field("description", "Description", req.get("description"),
                               hint="What is this project for?")
        + views.select_field("owner", "Owner", OWNER_OPTIONS,
                             req.get("owner", "Unassigned"))
        + '<div class="actions">'
        + views.button("Create project")
        + views.link_button(req.url("/projects"), "Cancel")
        + "</div></form>"
    )
    return html(views.page(req, title="New project", active="/projects", body=body,
                           subtitle="Projects are visible to everyone in the workspace."))


# --------------------------------------------------------------------------
# Detail
# --------------------------------------------------------------------------


def detail(req: Request) -> Response:
    project = store.find_project(req.state, req.params["pid"])
    if project is None:
        return not_found("No such project")

    facts = (
        f'<dl class="facts">'
        f"<dt>Owner</dt><dd>{views.esc(project['owner'])}</dd>"
        f"<dt>Status</dt><dd>{_badge(project['status'])}</dd>"
        f"<dt>Description</dt><dd>{views.esc(project['description'] or '—')}</dd>"
        f"</dl>"
    )
    actions = (
        '<div class="actions">'
        + views.link_button(req.url(f"/projects/{project['id']}/edit"), "Edit")
        + views.link_button(req.url("/projects"), "Back to projects")
        + "</div>"
    )
    delete_url = views.esc(req.url("/projects/" + project["id"] + "/delete"))
    danger = views.card(
        f'<form method="POST" action="{delete_url}">'
        "<p>Deleting a project removes it and everything in it. "
        "This cannot be undone.</p>"
        f'<div class="actions">{views.button("Delete project", kind="danger")}</div>'
        "</form>",
        title="Danger zone",
    )

    return html(
        views.page(req, title=project["name"], active="/projects",
                   body=views.card(facts + actions) + danger,
                   subtitle="Project details")
    )


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------


def edit(req: Request) -> Response:
    project = store.find_project(req.state, req.params["pid"])
    if project is None:
        return not_found("No such project")

    if req.method == "POST":
        project["name"] = req.get("name") or project["name"]
        project["description"] = req.get("description")
        project["owner"] = req.get("owner") or project["owner"]
        project["status"] = req.get("status") or project["status"]
        return redirect(req.url(f"/projects/{project['id']}", notice="project-updated"))

    body = views.card(
        f'<form method="POST" action="{views.esc(req.path)}">'
        + views.text_field("name", "Project name", project["name"], required=True)
        + views.textarea_field("description", "Description", project["description"])
        + views.select_field("owner", "Owner", OWNER_OPTIONS, project["owner"])
        + views.select_field("status", "Status", STATUS_OPTIONS, project["status"])
        + '<div class="actions">'
        + views.button("Save changes")
        + views.link_button(req.url(f"/projects/{project['id']}"), "Cancel")
        + "</div></form>"
    )
    return html(views.page(req, title=f"Edit {project['name']}", active="/projects",
                           body=body, subtitle="Changes apply immediately."))


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------


def delete(req: Request) -> Response:
    state = req.state
    project = store.find_project(state, req.params["pid"])
    if project is None:
        return not_found("No such project")

    if req.broken:
        # Soft delete: hidden from the collection, still stored, still served
        # at its own URL, still counted.
        project["archived"] = True
    else:
        state["projects"] = [p for p in state["projects"] if p["id"] != project["id"]]

    return redirect(req.url("/projects", notice="project-deleted"))
