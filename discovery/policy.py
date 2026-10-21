"""Explicit identity grouping and capability-first scheduling."""
import re
from urllib.parse import urlsplit, urlunsplit


def identity_snapshot(snapshot, patterns, include_inputs=False):
    if include_inputs:
        choices = [(e.get('selector'), e.get('value'), e.get('checked'))
                   for e in snapshot.get('elements', [])
                   if e.get('tag') == 'select' or e.get('type') in ('radio', 'checkbox')]
        snapshot = {**snapshot, 'input_state': choices}
    if not patterns:
        return snapshot
    parts = urlsplit(snapshot.get("url", ""))
    segments = ["<id>" if any(re.fullmatch(pattern, segment) for pattern in patterns) else segment
                for segment in parts.path.split("/")]
    return {**snapshot, "identity_url": urlunsplit((parts.scheme, parts.netloc, "/".join(segments), parts.query, ""))}


def priority(item, graph):
    state = graph.states.get(item.state_id)
    affordance = state.affordances.get(item.affordance_key) if state else None
    if not affordance:
        return (9, item.depth)
    label = affordance.label.lower().strip()
    if affordance.kind == "submit" or re.search(r"checkout|continue|create|save|update|sign in|register", label):
        rank = 0
    elif affordance.kind == "link" and affordance.href and not any(
            s.example_url == affordance.href for s in graph.states.values()):
        rank = 1
    elif label.isdigit() or label in ("next", "previous"):
        rank = 4
    elif affordance.kind == "link":
        rank = 3
    else:
        rank = 2
    return rank, item.depth


def navigation_key(affordance, state):
    """Only infer navigation when the exposed authentication context agrees."""
    if affordance.kind != "link":
        return None
    elements = (state.snapshot or {}).get("elements", [])
    if any(re.search(r"sign\s*out|log\s*out", e.get("text") or "", re.I) for e in elements):
        context = ('signed-in',tuple(sorted({urlsplit(e.get('href') or '').path for e in elements
            if re.search(r'/(admin|profile|account|productlist|orderhistory)(/|$)',urlsplit(e.get('href') or '').path)})))
    elif any(re.search(r"/(signin|login)(/|$|\?)", urlsplit(e.get("href") or "").path) for e in elements):
        context = "anonymous"
    else:
        return None
    return repr((affordance.key, affordance.href, context))
