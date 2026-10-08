"""Ground truth for the demo app.

What each flow *is*, stated in the vocabulary the flow-discovery work uses:
capability, goal conditions, invariants, and the observed path that one run
happens to take. The planted defect for each flow is recorded here too.

This exists so flow discovery can be scored instead of eyeballed: point the
inspector at the app, and compare what it produces against this file. Nothing
in the application reads it, and it is not linked from any page — it is served
at /__truth for tooling, and printed by `python -m demo_app.manifest`.

`path` is written as "a path that a run may take", never as the specification.
The checkout flow has two valid paths and both are listed, which is the
distinction the whole exercise turns on.
"""

from __future__ import annotations

import json

FLOWS = [
    {
        "id": "settings.profile",
        "name": "Update profile",
        "capability": "PERSISTENT_MUTATION",
        "entry": "/profile",
        "paths": [["/profile", "edit Name / Bio", "Save", "/profile (reload)"]],
        "goal_conditions": [
            "every edited field holds the submitted value after a reload",
        ],
        "invariants": ["PERSISTENCE_001"],
        "defect": {
            "url": "/broken/profile",
            "summary": "Bio is never written. The save succeeds and the page "
                       "still says 'Saved successfully'.",
            "violates": "PERSISTENCE_001",
        },
    },
    {
        "id": "settings.project",
        "name": "Update project settings",
        "capability": "PERSISTENT_MUTATION",
        "entry": "/project-settings",
        "paths": [["/project-settings", "edit Project title / Description",
                   "Update Project", "/project-settings (reload)"]],
        "goal_conditions": [
            "every edited field holds the submitted value after a reload",
        ],
        "invariants": ["PERSISTENCE_001"],
        "defect": None,
        "note": "Same invariant as settings.profile, none of the same words. "
                "Included to show the system is not matching on 'Bio'.",
    },
    {
        "id": "account.authenticate",
        "name": "Sign in and sign out",
        "capability": "AUTHENTICATE",
        "entry": "/login",
        "paths": [
            ["/login", "submit demo / demo123", "/account"],
            ["/account", "Sign out", "/login", "/account is no longer reachable"],
        ],
        "goal_conditions": [
            "valid credentials grant access to the protected resource",
            "invalid credentials do not",
            "after signing out the protected resource is unreachable again",
        ],
        "invariants": ["SESSION_TERMINATION (not yet in the rule base)"],
        "defect": {
            "url": "/broken/account",
            "summary": "Sign out shows the confirmation and returns to the "
                       "sign-in page, but never clears the session; "
                       "/broken/account still serves the account.",
            "violates": "after signing out the protected resource is unreachable",
        },
    },
    {
        "id": "projects.create",
        "name": "Create a project",
        "capability": "CREATE_RESOURCE",
        "entry": "/projects/new",
        "paths": [["/projects", "New project", "/projects/new", "fill Name",
                   "Create project", "/projects/<id>"]],
        "goal_conditions": [
            "the new resource exists and is reachable at its own URL",
            "it appears in the collection",
            "its fields hold what was submitted",
        ],
        "invariants": ["CREATION_VISIBILITY (not yet in the rule base)"],
        "defect": None,
    },
    {
        "id": "projects.delete",
        "name": "Delete a project",
        "capability": "DELETE_RESOURCE",
        "entry": "/projects/<id>",
        "paths": [["/projects/<id>", "Delete project", "/projects"]],
        "goal_conditions": [
            "the resource is gone from the collection",
            "the resource is no longer reachable at its own URL",
            "the collection's stated count matches the rows it shows",
        ],
        "invariants": ["DELETION_COMPLETENESS (not yet in the rule base)"],
        "defect": {
            "url": "/broken/projects",
            "summary": "Delete is a soft delete: the row disappears and the "
                       "page says 'Project deleted', but the resource is still "
                       "served at its URL and still included in the count.",
            "violates": "the resource is no longer reachable at its own URL",
        },
    },
    {
        "id": "catalog.query",
        "name": "Search, filter and sort the catalog",
        "capability": "SEARCH / FILTER_COLLECTION / SORT_COLLECTION",
        "entry": "/catalog",
        "paths": [["/catalog", "set Search / Category / Sort by",
                   "Apply filters", "/catalog?q=…&category=…&sort=…"]],
        "goal_conditions": [
            "every row matches the active query and filters",
            "the stated count matches the number of rows",
            "the chosen sort order holds across consecutive rows",
            "clearing the filters returns the full collection",
        ],
        "invariants": ["ORDERING_CONSISTENCY (not yet in the rule base)"],
        "defect": {
            "url": "/broken/catalog?sort=price-asc",
            "summary": "The price sort orders by the rendered string, so "
                       "$1,299.00 sorts before $89.00. Rows are complete and "
                       "correctly filtered; only the order is wrong.",
            "violates": "the chosen sort order holds across consecutive rows",
        },
    },
    {
        "id": "checkout.purchase",
        "name": "Buy what is in the cart",
        "capability": "MULTI_STEP_TRANSACTION",
        "entry": "/cart",
        "paths": [
            ["/cart", "/checkout/contact", "/checkout/shipping",
             "/checkout/payment", "/checkout/confirmation"],
            ["/cart", "/checkout/contact", "/checkout/upsell",
             "/checkout/shipping", "/checkout/payment",
             "/checkout/confirmation"],
        ],
        "goal_conditions": [
            "the confirmation lists the cart's items at the cart's quantities",
            "the total equals the sum of the lines shown",
            "an order number is issued",
        ],
        "invariants": ["TOTAL_CONSISTENCY (not yet in the rule base)"],
        "defect": {
            "url": "/broken/cart",
            "summary": "A protection plan added at the upsell step is listed "
                       "on the summary and the confirmation but never added to "
                       "the total. Reachable only on the six-step path.",
            "violates": "the total equals the sum of the lines shown",
        },
        "note": "Two valid paths. The upsell step appears only when the "
                "subtotal clears $150, so the path varies between runs of the "
                "same correct application.",
    },
]


def as_dict() -> dict:
    return {"schema_version": "1.0", "flows": FLOWS}


if __name__ == "__main__":
    print(json.dumps(as_dict(), indent=2))
