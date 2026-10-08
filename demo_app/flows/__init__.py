"""Flow modules.

Each module exposes `register(router)` and describes one user-facing
capability of the demo application. A flow is registered once and mounted
twice — at `/` for the sound build and at `/broken` for the build carrying a
planted defect — so the two are the same code reading the same templates, and
the only difference is the one behaviour named in `demo_app.manifest`.
"""

from __future__ import annotations

from . import auth, catalog, checkout, profile, projects

MODULES = (profile, auth, projects, catalog, checkout)


def register_all(router) -> None:
    for module in MODULES:
        module.register(router)
