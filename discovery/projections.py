"""Lossy views of an `AppGraph`, each sized for one consumer.

There is no single digestible format, because the questions asked of a graph
have wildly different information needs. Labelling one state needs that
state's controls in full; finding capabilities needs every state's *name* and
nothing else. Sending the second consumer what the first needs is how a
context window gets spent on noise:

    whole graph, every snapshot      ~150k tokens   unusable
    whole graph, summary projection  ~3k tokens     the model holds the map

So the graph is stored once and projected many times. `summary()` is the view
that makes whole-application reasoning affordable: the model reads the map,
then asks for `state_detail()` on the two or three states it actually cares
about.

`mermaid()` and `html()` are the same idea aimed at a person rather than a
model. A graph you cannot eyeball is a graph you cannot debug, and the first
thing anyone wants to know about a crawl is whether it found the shape they
expected.
"""

from __future__ import annotations

import html as html_escape
import json
from collections import defaultdict

from discovery.graph import Action, AppGraph, State
from discovery.svg import SCRIPT as GRAPH_SCRIPT
from discovery.svg import STYLE as GRAPH_STYLE
from discovery.svg import render as render_svg

# --------------------------------------------------------------------------
# For the model
# --------------------------------------------------------------------------


def _short(state: State) -> str:
    """A readable handle for a state, preferring the route over the hash."""
    return state.url_template


def _kind_counts(state: State) -> str:
    counts: dict[str, int] = defaultdict(int)
    for affordance in state.affordances.values():
        counts[affordance.kind] += 1
    if not counts:
        return "-"
    return " ".join(f"{kind}:{n}" for kind, n in sorted(counts.items()))


def summary(graph: AppGraph, *, include_self_loops: bool = False,
            hoist_navigation: bool = True) -> str:
    """The whole application as a compact map, ~15 tokens per state.

    This is what a model is shown when the question spans the application:
    which capabilities exist, which states look like a confirmation, where a
    flow begins. Element-level detail is deliberately absent — it is
    available on request, and including it here would defeat the purpose.

    Site-wide navigation is stated once rather than repeated under every
    state. A sidebar of eight links across twenty states is 160 lines saying
    the same thing, which buries the handful of transitions that actually
    describe a flow — the exact opposite of what this projection is for.
    """
    lines: list[str] = [
        f"# application map  ({len(graph.states)} states, {len(graph.actions)} actions)",
        f"# entry: {graph.entry}",
    ]

    by_source: dict[str, list[Action]] = defaultdict(list)
    navigation: dict[tuple[str, str], None] = {}

    for action in graph.actions:
        if action.is_self_loop and not include_self_loops:
            continue
        if hoist_navigation and action.inferred:
            navigation[(action.label, action.target)] = None
            continue
        by_source[action.source].append(action)

    if navigation:
        lines += ["", "# available from every state (site-wide navigation):"]
        for label, target in sorted(navigation):
            target_state = graph.states.get(target)
            lines.append(
                f"#   {label} -> {target} {_short(target_state) if target_state else ''}"
            )

    lines.append("")

    for state_id, state in sorted(graph.states.items(), key=lambda kv: kv[1].url_template):
        marker = "*" if state_id == graph.entry else " "
        lines.append(f"{marker}{state_id}  {_short(state)}  [{_kind_counts(state)}]")

        # Two controls with the same text going to the same place (a header
        # link and a sidebar link) are one fact about the application.
        seen: set[tuple[str, str]] = set()
        shown = 0
        for action in by_source.get(state_id, []):
            pair = (action.label, action.target)
            if pair in seen:
                continue
            seen.add(pair)
            target = graph.states.get(action.target)
            target_name = _short(target) if target else action.target
            lines.append(f"      --{action.label}--> {action.target} {target_name}")
            shown += 1
        if not shown:
            lines.append("      (no transitions beyond site-wide navigation)")

    if not graph.is_closed():
        pending = sum(len(v) for v in graph.unexplored.values())
        lines += ["", f"# WARNING: {pending} affordances unexplored -- graph is not closed,",
                  "# so 'no path exists' cannot be concluded from it."]

    return "\n".join(lines)


def state_detail(graph: AppGraph, state_id: str) -> str:
    """One state's controls in full, for labelling or test instantiation."""
    state = graph.states.get(state_id)
    if state is None:
        return f"# unknown state {state_id}"

    lines = [
        f"# {state_id}  {state.url_template}",
        f"# title: {state.title}",
        f"# example url: {state.example_url}",
        "",
    ]
    for affordance in state.affordances.values():
        lines.append(f"{affordance.kind:7} {affordance.label!r}")
    return "\n".join(lines)


def edge_detail(graph: AppGraph, action: Action) -> str:
    """One transition, as its effect rather than two snapshots.

    Roughly fifty tokens where the two endpoint snapshots would be four
    thousand, and it carries the part a model actually needs: what changed.
    """
    lines = [f"{action.source} --{action.label}--> {action.target}"]
    if action.inputs:
        lines.append("  inputs: " + ", ".join(f"{k}={v!r}" for k, v in action.inputs))
    for effect in action.effects:
        lines.append(f"  {effect}")
    return "\n".join(lines)


def token_estimate(text: str) -> int:
    """Rough token count, for budgeting a projection. ~4 chars per token."""
    return len(text) // 4


# --------------------------------------------------------------------------
# For a person
# --------------------------------------------------------------------------


def _mermaid_text(text: str) -> str:
    """Make text safe inside a quoted mermaid label.

    Mermaid parses labels as HTML, so a route template like `/projects/<id>`
    has its `<id>` swallowed as an unknown tag and the node renders as
    `/projects/`. Escaping the angle brackets is what keeps the diagram
    saying the same thing the graph does.
    """
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("|", "/"))


def _node_label(state: State, *, disambiguate: bool = False) -> str:
    route = _mermaid_text(state.url_template or "/")
    title = _mermaid_text((state.title or "").split("—")[0].split("|")[0].strip())
    # Four distinct cart states all render as "/cart" and become impossible
    # to tell apart, so collisions get a fragment of the fingerprint.
    if disambiguate:
        route = f"{route} <small>({state.id[2:6]})</small>"
    return f"{route}<br/><small>{title}</small>" if title else route


def mermaid(graph: AppGraph, *, include_self_loops: bool = False,
            include_navigation: bool = False) -> str:
    """Mermaid flowchart source for the graph.

    Site-wide navigation is omitted by default. Every state linking to every
    other produces a diagram that is technically complete and visually
    useless — the flows worth seeing disappear into a mesh of sidebar links.

    Parallel edges between the same pair are merged into one labelled with
    each action, because a purchase flow rendered with six separate arrows
    between the same two boxes is harder to read than the text it came from.
    """
    lines = ["flowchart TD"]
    ids = {state_id: f"n{i}" for i, state_id in enumerate(sorted(graph.states))}

    route_counts: dict[str, int] = defaultdict(int)
    for state in graph.states.values():
        route_counts[state.url_template] += 1

    for state_id, state in sorted(graph.states.items()):
        node = ids[state_id]
        label = _node_label(state, disambiguate=route_counts[state.url_template] > 1)
        shape = f'{node}(["{label}"])' if state_id == graph.entry else f'{node}["{label}"]'
        lines.append(f"    {shape}")

    merged: dict[tuple[str, str], list[str]] = defaultdict(list)
    for action in graph.actions:
        if action.is_self_loop and not include_self_loops:
            continue
        if action.inferred and not include_navigation:
            continue
        merged[(action.source, action.target)].append(action.label)

    for (source, target), labels in merged.items():
        if source not in ids or target not in ids:
            continue
        unique = list(dict.fromkeys(labels))
        shown = _mermaid_text(" / ".join(unique[:3])) + ("…" if len(unique) > 3 else "")
        lines.append(f'    {ids[source]} -->|"{shown}"| {ids[target]}')

    entry_node = ids.get(graph.entry)
    if entry_node:
        lines.append(f"    style {entry_node} fill:#2d6a4f,stroke:#95d5b2,color:#fff")
    for state in graph.terminals():
        node = ids.get(state.id)
        if node and state.id != graph.entry:
            lines.append(f"    style {node} fill:#6a2d4f,stroke:#d595b2,color:#fff")

    return "\n".join(lines)


def html(graph: AppGraph, *, title: str = "Application graph") -> str:
    """A standalone page: the drawn graph, plus the tables behind it.

    The diagram is real SVG with a layout solved in `discovery.svg`, not
    mermaid source awaiting a renderer. That keeps the page working as a
    local `file://` with no library and no network, which is what it needs to
    be when it is sitting in `runs/` next to the crawl that produced it.
    """
    esc = html_escape.escape
    diagram = render_svg(graph)

    rows = []
    for state_id, state in sorted(graph.states.items(), key=lambda kv: kv[1].url_template):
        out = len([a for a in graph.out_edges(state_id) if not a.is_self_loop])
        pending = len(graph.unexplored.get(state_id, []))
        rows.append(
            f"<tr><td><code>{esc(state.url_template)}</code></td>"
            f"<td>{esc(state.title)}</td>"
            f"<td class='n'>{len(state.affordances)}</td>"
            f"<td class='n'>{out}</td>"
            f"<td class='n{' warn' if pending else ''}'>{pending or ''}</td></tr>"
        )

    edges = []
    for action in graph.actions:
        source = graph.states.get(action.source)
        target = graph.states.get(action.target)
        edges.append(
            f"<tr><td><code>{esc(source.url_template if source else action.source)}</code></td>"
            f"<td>{esc(action.label)}</td>"
            f"<td><code>{esc(target.url_template if target else action.target)}</code></td>"
            f"<td class='eff'>{esc(', '.join(action.effects))}</td></tr>"
        )

    closed = graph.is_closed()
    status = (
        "<span class='ok'>closed</span> — every affordance was fired, so "
        "&ldquo;no path exists&rdquo; is a conclusion this graph supports."
        if closed else
        f"<span class='warn'>open</span> — {sum(len(v) for v in graph.unexplored.values())} "
        "affordances unexplored, so absence of a path proves nothing."
    )

    return f"""<title>{esc(title)}</title>
<style>
  :root {{ --bg:#fff; --fg:#1a1a1a; --mut:#666; --line:#e4e4e7; --card:#fafafa;
           --ok:#2d6a4f; --warn:#b45309; --accent:#2563eb;
           --edge:#b4b4bb; --nodebg:#fff; --nodeline:#d4d4d8;
           --entrybg:#e7f5ec; --entryline:#2d6a4f;
           --termbg:#fdf0f5; --termline:#a34a72; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#111214; --fg:#e8e8ea; --mut:#9a9aa2; --line:#2a2b30; --card:#17181c;
             --ok:#95d5b2; --warn:#fbbf24; --accent:#7aa2f7;
             --edge:#4a4b54; --nodebg:#1d1e24; --nodeline:#3a3b44;
             --entrybg:#16342a; --entryline:#95d5b2;
             --termbg:#331d2a; --termline:#d595b2; }}
  }}
  :root[data-theme="dark"] {{ --bg:#111214; --fg:#e8e8ea; --mut:#9a9aa2; --line:#2a2b30;
                              --card:#17181c; --ok:#95d5b2; --warn:#fbbf24;
                              --accent:#7aa2f7; --edge:#4a4b54; --nodebg:#1d1e24;
                              --nodeline:#3a3b44; --entrybg:#16342a; --entryline:#95d5b2;
                              --termbg:#331d2a; --termline:#d595b2; }}
  :root[data-theme="light"] {{ --bg:#fff; --fg:#1a1a1a; --mut:#666; --line:#e4e4e7;
                               --card:#fafafa; --ok:#2d6a4f; --warn:#b45309;
                               --accent:#2563eb; --edge:#b4b4bb; --nodebg:#fff;
                               --nodeline:#d4d4d8; --entrybg:#e7f5ec; --entryline:#2d6a4f;
                               --termbg:#fdf0f5; --termline:#a34a72; }}
  body {{ background:var(--bg); color:var(--fg); margin:0; padding:2rem 1.5rem;
          font:15px/1.55 ui-sans-serif,system-ui,-apple-system,sans-serif; }}
  main {{ max-width:1100px; margin:0 auto; }}
  h1 {{ font-size:1.5rem; margin:0 0 .25rem; }}
  h2 {{ font-size:1.05rem; margin:2.5rem 0 .75rem; font-weight:600; }}
  .sub {{ color:var(--mut); margin:0 0 1.5rem; }}
  .stats {{ display:flex; gap:.75rem; flex-wrap:wrap; margin:1.25rem 0; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:.7rem 1rem; min-width:100px; }}
  .stat b {{ display:block; font-size:1.4rem; font-variant-numeric:tabular-nums; }}
  .stat span {{ color:var(--mut); font-size:.8rem; }}
  .diagram {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
              padding:1rem; overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:.85rem; }}
  th {{ text-align:left; color:var(--mut); font-weight:600; padding:.45rem .6rem;
        border-bottom:1px solid var(--line); }}
  td {{ padding:.45rem .6rem; border-bottom:1px solid var(--line); vertical-align:top; }}
  td.n {{ text-align:right; font-variant-numeric:tabular-nums; }}
  code {{ font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .eff {{ color:var(--mut); font-size:.78rem; }}
  .ok {{ color:var(--ok); font-weight:600; }}
  .warn {{ color:var(--warn); font-weight:600; }}
  .scroll {{ overflow-x:auto; }}
  .key {{ display:inline-flex; align-items:center; gap:.35rem; margin-right:.9rem; }}
  .swatch {{ width:13px; height:13px; border-radius:3px; display:inline-block; }}
{GRAPH_STYLE}
</style>
<main>
  <h1>{esc(title)}</h1>
  <p class="sub">{esc(graph.base_url)} &middot; entry <code>{esc(graph.entry)}</code></p>

  <div class="stats">
    <div class="stat"><b>{len(graph.states)}</b><span>states</span></div>
    <div class="stat"><b>{len(graph.actions)}</b><span>actions</span></div>
    <div class="stat"><b>{len(graph.terminals())}</b><span>terminal</span></div>
    <div class="stat"><b>{len(graph.orphans())}</b><span>orphaned</span></div>
  </div>

  <p>Graph is {status}</p>

  <h2>Structure</h2>
  <div id="graphbar">
    <button id="fit">Fit</button>
    <button id="zoomin">+</button>
    <button id="zoomout">&minus;</button>
    <span class="key"><span class="swatch" style="background:var(--entrybg);
      border:2px solid var(--entryline)"></span>entry</span>
    <span class="key"><span class="swatch" style="background:var(--termbg);
      border:1px solid var(--termline)"></span>terminal</span>
    <span>drag to pan &middot; scroll to zoom &middot; hover a state to isolate it</span>
  </div>
  <div id="graphwrap" style="height:min(70vh,640px)">{diagram}</div>
  <p class="sub" style="margin-top:.6rem">Site-wide navigation is omitted —
  every state links to every other, and drawing that hides the flows.
  Dashed edges go backward or sideways.</p>

  <h2>States</h2>
  <div class="scroll"><table>
    <tr><th>route</th><th>title</th><th>affordances</th><th>out</th><th>unexplored</th></tr>
    {''.join(rows)}
  </table></div>

  <h2>Transitions</h2>
  <div class="scroll"><table>
    <tr><th>from</th><th>action</th><th>to</th><th>effect</th></tr>
    {''.join(edges)}
  </table></div>
</main>
<script>{GRAPH_SCRIPT}</script>
"""


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="blindspot-project")
    parser.add_argument("graph", nargs="?", default="runs/graphs/graph.json")
    parser.add_argument("--view", choices=["summary", "mermaid", "html", "json"],
                        default="summary")
    parser.add_argument("--state", help="state id, for the detail view")
    parser.add_argument("--out", help="write to this file instead of stdout")
    args = parser.parse_args()

    loaded = AppGraph.load(args.graph)

    if args.state:
        output = state_detail(loaded, args.state)
    elif args.view == "mermaid":
        output = mermaid(loaded)
    elif args.view == "html":
        output = html(loaded)
    elif args.view == "json":
        output = json.dumps(loaded.to_dict(), indent=2)
    else:
        output = summary(loaded)

    if args.out:
        Path(args.out).write_text(output)
        print(f"wrote {args.out}")
    else:
        print(output)
        if args.view == "summary":
            print(f"\n# ~{token_estimate(output)} tokens")
