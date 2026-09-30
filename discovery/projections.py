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


def summary(graph: AppGraph, *, include_self_loops: bool = False) -> str:
    """The whole application as a compact map, ~15 tokens per state.

    This is what a model is shown when the question spans the application:
    which capabilities exist, which states look like a confirmation, where a
    flow begins. Element-level detail is deliberately absent — it is
    available on request, and including it here would defeat the purpose.
    """
    lines: list[str] = [
        f"# application map  ({len(graph.states)} states, {len(graph.actions)} actions)",
        f"# entry: {graph.entry}",
        "",
    ]

    by_source: dict[str, list[Action]] = defaultdict(list)
    for action in graph.actions:
        if action.is_self_loop and not include_self_loops:
            continue
        by_source[action.source].append(action)

    for state_id, state in sorted(graph.states.items(), key=lambda kv: kv[1].url_template):
        marker = "*" if state_id == graph.entry else " "
        lines.append(f"{marker}{state_id}  {_short(state)}  [{_kind_counts(state)}]")
        for action in by_source.get(state_id, []):
            target = graph.states.get(action.target)
            target_name = _short(target) if target else action.target
            lines.append(f"      --{action.label}--> {action.target} {target_name}")
        if not by_source.get(state_id):
            lines.append("      (terminal)")

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


def _node_label(state: State) -> str:
    route = state.url_template or "/"
    title = (state.title or "").split("—")[0].split("|")[0].strip()
    return f"{route}<br/><small>{title}</small>" if title else route


def mermaid(graph: AppGraph, *, include_self_loops: bool = False) -> str:
    """Mermaid flowchart source for the graph.

    Parallel edges between the same pair are merged into one labelled with
    each action, because a purchase flow rendered with six separate arrows
    between the same two boxes is harder to read than the text it came from.
    """
    lines = ["flowchart TD"]
    ids = {state_id: f"n{i}" for i, state_id in enumerate(sorted(graph.states))}

    for state_id, state in sorted(graph.states.items()):
        node = ids[state_id]
        label = _node_label(state).replace('"', "'")
        shape = f'{node}(["{label}"])' if state_id == graph.entry else f'{node}["{label}"]'
        lines.append(f"    {shape}")

    merged: dict[tuple[str, str], list[str]] = defaultdict(list)
    for action in graph.actions:
        if action.is_self_loop and not include_self_loops:
            continue
        merged[(action.source, action.target)].append(action.label)

    for (source, target), labels in merged.items():
        if source not in ids or target not in ids:
            continue
        unique = list(dict.fromkeys(labels))
        shown = " / ".join(unique[:3]) + ("…" if len(unique) > 3 else "")
        shown = shown.replace('"', "'").replace("|", "/")
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
    """A standalone page: the diagram, plus the tables behind it.

    Self-contained by design — the mermaid runtime is the only thing it needs
    and Artifacts provide it, so the file works offline and can be committed
    next to a run record.
    """
    esc = html_escape.escape
    diagram = mermaid(graph)

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
           --ok:#2d6a4f; --warn:#b45309; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#111214; --fg:#e8e8ea; --mut:#9a9aa2; --line:#2a2b30; --card:#17181c;
             --ok:#95d5b2; --warn:#fbbf24; }}
  }}
  :root[data-theme="dark"] {{ --bg:#111214; --fg:#e8e8ea; --mut:#9a9aa2; --line:#2a2b30;
                              --card:#17181c; --ok:#95d5b2; --warn:#fbbf24; }}
  :root[data-theme="light"] {{ --bg:#fff; --fg:#1a1a1a; --mut:#666; --line:#e4e4e7;
                               --card:#fafafa; --ok:#2d6a4f; --warn:#b45309; }}
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
  <div class="diagram"><pre class="mermaid">{esc(diagram)}</pre></div>

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
