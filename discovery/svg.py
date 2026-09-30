"""Draw an `AppGraph` as SVG — nodes, edges, arrowheads, computed layout.

Mermaid source is text that *describes* a diagram and needs a renderer to
become one. This module produces the diagram itself: a layered layout solved
in Python and emitted as SVG, so the output opens as a local file with no
library, no CDN, and no network. That matters because the page is written to
`runs/` next to a crawl and has to still work months later.

The layout is the classic layered (Sugiyama-style) approach, minus the parts
that need a solver:

    1. layer      breadth-first distance from the entry, so the diagram reads
                  as "how far into the application is this"
    2. order      barycentre sweeps within each layer to reduce edge crossings
    3. place      x by position in layer, y by layer
    4. route      forward edges as curves; back edges bowed out to the side so
                  a cycle does not draw straight through the nodes it skips

Good enough for the few dozen states an exhaustive crawl of one region
produces, which is the size this is for. A thousand-node graph needs a real
layout engine, and also needs a human to stop looking at it as a picture.
"""

from __future__ import annotations

import html
from collections import defaultdict, deque

from discovery.graph import AppGraph

# Geometry, in user units.
NODE_H = 46
NODE_MIN_W = 130
CHAR_W = 7.3
PAD_X = 26
LAYER_GAP = 130
NODE_GAP = 34
MARGIN = 40


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


def _node_width(label: str) -> float:
    return max(NODE_MIN_W, len(label) * CHAR_W + PAD_X * 2)


def _layers(graph: AppGraph, edges: list[tuple[str, str]]) -> list[list[str]]:
    """Assign every state a depth, breadth-first from the entry.

    States the entry cannot reach still have to be drawn — an orphan is a
    finding, not something to hide — so anything unvisited is appended to a
    final layer rather than dropped.
    """
    successors: dict[str, list[str]] = defaultdict(list)
    for source, target in edges:
        successors[source].append(target)

    depth: dict[str, int] = {}
    if graph.entry in graph.states:
        depth[graph.entry] = 0
        queue = deque([graph.entry])
        while queue:
            current = queue.popleft()
            for nxt in successors.get(current, ()):
                if nxt not in depth:
                    depth[nxt] = depth[current] + 1
                    queue.append(nxt)

    unreached = [s for s in graph.states if s not in depth]
    if unreached:
        floor = max(depth.values(), default=-1) + 1
        for state_id in unreached:
            depth[state_id] = floor

    grouped: dict[int, list[str]] = defaultdict(list)
    for state_id, level in depth.items():
        grouped[level].append(state_id)
    return [sorted(grouped[level]) for level in sorted(grouped)]


def _reduce_crossings(layers: list[list[str]],
                      edges: list[tuple[str, str]],
                      passes: int = 6) -> None:
    """Barycentre sweeps, in place.

    Each node is pulled toward the average position of its neighbours in the
    adjacent layer, alternating direction. It is a heuristic with no
    guarantees, but on graphs this size a handful of passes removes most of
    the crossings a naive ordering produces.
    """
    predecessors: dict[str, list[str]] = defaultdict(list)
    successors: dict[str, list[str]] = defaultdict(list)
    for source, target in edges:
        successors[source].append(target)
        predecessors[target].append(source)

    def sweep(index: int, neighbours: dict[str, list[str]], reference: list[str]) -> None:
        position = {node: i for i, node in enumerate(reference)}
        fallback = len(reference) / 2

        def barycentre(node: str) -> float:
            seen = [position[n] for n in neighbours.get(node, ()) if n in position]
            return sum(seen) / len(seen) if seen else fallback

        layers[index].sort(key=barycentre)

    for iteration in range(passes):
        if iteration % 2 == 0:
            for i in range(1, len(layers)):
                sweep(i, predecessors, layers[i - 1])
        else:
            for i in range(len(layers) - 2, -1, -1):
                sweep(i, successors, layers[i + 1])


def _place(graph: AppGraph, layers: list[list[str]]) -> dict[str, dict]:
    """Turn an ordering into coordinates."""
    labels = {
        state_id: (graph.states[state_id].url_template or "/")
        for state_id in graph.states
    }

    widths = [
        sum(_node_width(labels[n]) for n in layer) + NODE_GAP * max(0, len(layer) - 1)
        for layer in layers
    ]
    canvas_width = max(widths, default=NODE_MIN_W)

    placed: dict[str, dict] = {}
    for level, layer in enumerate(layers):
        row_width = widths[level]
        x = MARGIN + (canvas_width - row_width) / 2
        y = MARGIN + level * LAYER_GAP
        for state_id in layer:
            width = _node_width(labels[state_id])
            placed[state_id] = {
                "x": x, "y": y, "w": width, "h": NODE_H,
                "cx": x + width / 2, "cy": y + NODE_H / 2,
                "layer": level, "label": labels[state_id],
            }
            x += width + NODE_GAP

    return placed


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _edge_path(source: dict, target: dict) -> str:
    """A cubic curve between two placed nodes."""
    if target["layer"] > source["layer"]:
        x1, y1 = source["cx"], source["y"] + source["h"]
        x2, y2 = target["cx"], target["y"]
        span = max(30.0, (y2 - y1) * 0.45)
        return f"M{x1:.1f},{y1:.1f} C{x1:.1f},{y1 + span:.1f} {x2:.1f},{y2 - span:.1f} {x2:.1f},{y2:.1f}"

    # Backward or same-layer: bow out to the side so the curve does not run
    # straight through whatever sits between the two nodes.
    x1, y1 = source["x"] + source["w"], source["cy"]
    x2, y2 = target["x"] + target["w"], target["cy"]
    bulge = 60 + abs(source["layer"] - target["layer"]) * 26
    return (f"M{x1:.1f},{y1:.1f} C{x1 + bulge:.1f},{y1:.1f} "
            f"{x2 + bulge:.1f},{y2:.1f} {x2:.1f},{y2:.1f}")


def render(graph: AppGraph, *, include_navigation: bool = False,
           include_self_loops: bool = False) -> str:
    """Draw the graph. Returns a complete `<svg>` element.

    Site-wide navigation is excluded by default: with every state linking to
    every other, the picture is a mesh and the flows — the only thing anyone
    opens this to see — vanish into it.
    """
    merged: dict[tuple[str, str], list[str]] = defaultdict(list)
    for action in graph.actions:
        if action.is_self_loop and not include_self_loops:
            continue
        if action.inferred and not include_navigation:
            continue
        if action.source not in graph.states or action.target not in graph.states:
            continue
        merged[(action.source, action.target)].append(action.label)

    pairs = list(merged)
    layers = _layers(graph, pairs)
    _reduce_crossings(layers, pairs)
    placed = _place(graph, layers)

    width = max((p["x"] + p["w"] for p in placed.values()), default=400) + MARGIN + 120
    height = max((p["y"] + p["h"] for p in placed.values()), default=200) + MARGIN

    terminals = {s.id for s in graph.terminals()}

    out: list[str] = [
        f'<svg id="appgraph" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="{width:.0f}" height="{height:.0f}" '
        'xmlns="http://www.w3.org/2000/svg" role="img" '
        'aria-label="Application state graph">',
        '<defs>'
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" class="arrowhead"/></marker>'
        '</defs>',
        '<g id="viewport">',
    ]

    # Edges first, so nodes paint over them.
    out.append('<g class="edges">')
    for (source_id, target_id), labels in merged.items():
        source, target = placed[source_id], placed[target_id]
        unique = list(dict.fromkeys(labels))
        caption = " / ".join(unique[:2]) + ("…" if len(unique) > 2 else "")
        path = _edge_path(source, target)
        back = target["layer"] <= source["layer"]
        out.append(
            f'<g class="edge{" back" if back else ""}" '
            f'data-from="{html.escape(source_id)}" data-to="{html.escape(target_id)}">'
            f'<title>{html.escape(source["label"])} → {html.escape(target["label"])}: '
            f'{html.escape(", ".join(unique))}</title>'
            f'<path d="{path}" marker-end="url(#arrow)"/>'
            f'</g>'
        )
    out.append('</g>')

    # Edge labels in their own layer, so no edge is drawn over a caption.
    out.append('<g class="edge-labels">')
    for (source_id, target_id), labels in merged.items():
        source, target = placed[source_id], placed[target_id]
        unique = list(dict.fromkeys(labels))
        caption = " / ".join(unique[:2]) + ("…" if len(unique) > 2 else "")
        if target["layer"] > source["layer"]:
            mx = (source["cx"] + target["cx"]) / 2
            my = (source["y"] + source["h"] + target["y"]) / 2
        else:
            mx = max(source["x"] + source["w"], target["x"] + target["w"]) + 46
            my = (source["cy"] + target["cy"]) / 2
        text = html.escape(caption)
        out.append(
            f'<text x="{mx:.1f}" y="{my:.1f}" text-anchor="middle" '
            f'dominant-baseline="middle">{text}</text>'
        )
    out.append('</g>')

    # Nodes.
    out.append('<g class="nodes">')
    for state_id, box in placed.items():
        state = graph.states[state_id]
        kind = ("entry" if state_id == graph.entry
                else "terminal" if state_id in terminals else "normal")
        pending = len(graph.unexplored.get(state_id, []))
        label = html.escape(box["label"])
        title = html.escape(state.title or "")
        out.append(
            f'<g class="node {kind}" data-id="{html.escape(state_id)}">'
            f'<title>{label} — {title} ({len(state.affordances)} affordances'
            f'{f", {pending} unexplored" if pending else ""})</title>'
            f'<rect x="{box["x"]:.1f}" y="{box["y"]:.1f}" '
            f'width="{box["w"]:.1f}" height="{box["h"]:.1f}" rx="9"/>'
            f'<text x="{box["cx"]:.1f}" y="{box["cy"] - 5:.1f}" '
            f'text-anchor="middle" dominant-baseline="middle" class="route">{label}</text>'
            f'<text x="{box["cx"]:.1f}" y="{box["cy"] + 11:.1f}" '
            f'text-anchor="middle" dominant-baseline="middle" class="meta">'
            f'{len(state.affordances)} affordances</text>'
            f'</g>'
        )
    out.append('</g>')

    out.append('</g></svg>')
    return "".join(out)


# The styling and the pan/zoom behaviour the rendered graph expects. Kept
# next to `render` so the two cannot drift apart.
STYLE = """
  #graphwrap { position:relative; overflow:hidden; border:1px solid var(--line);
               border-radius:12px; background:var(--card); cursor:grab; }
  #graphwrap.dragging { cursor:grabbing; }
  #graphwrap svg { display:block; max-width:none; touch-action:none; }
  .edge path { fill:none; stroke:var(--edge); stroke-width:1.6; }
  .edge.back path { stroke-dasharray:5 4; opacity:.75; }
  .arrowhead { fill:var(--edge); }
  .edge-labels text { font-size:10.5px; fill:var(--mut);
                      paint-order:stroke; stroke:var(--card); stroke-width:3.5px;
                      stroke-linejoin:round; }
  .node rect { fill:var(--nodebg); stroke:var(--nodeline); stroke-width:1.4; }
  .node .route { font-size:12.5px; font-weight:600; fill:var(--fg);
                 font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .node .meta { font-size:9.5px; fill:var(--mut); }
  .node.entry rect { fill:var(--entrybg); stroke:var(--entryline); stroke-width:2; }
  .node.terminal rect { fill:var(--termbg); stroke:var(--termline); }
  .node:hover rect { stroke:var(--accent); stroke-width:2.4; }
  .dim { opacity:.12; }
  #graphbar { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap;
              margin:.75rem 0; font-size:.8rem; color:var(--mut); }
  #graphbar button { font:inherit; padding:.3rem .7rem; border-radius:7px;
                     border:1px solid var(--line); background:var(--card);
                     color:var(--fg); cursor:pointer; }
  #graphbar button:hover { border-color:var(--accent); }
"""

SCRIPT = """
(function () {
  var svg = document.getElementById('appgraph');
  if (!svg) return;
  var viewport = document.getElementById('viewport');
  var wrap = document.getElementById('graphwrap');
  var scale = 1, tx = 0, ty = 0, dragging = false, lastX = 0, lastY = 0;

  function apply() {
    viewport.setAttribute('transform',
      'translate(' + tx + ',' + ty + ') scale(' + scale + ')');
  }
  function fit() {
    var box = wrap.getBoundingClientRect();
    var w = svg.viewBox.baseVal.width, h = svg.viewBox.baseVal.height;
    scale = Math.min(box.width / w, box.height / h, 1) * 0.96;
    tx = (box.width - w * scale) / 2;
    ty = (box.height - h * scale) / 2;
    apply();
  }

  wrap.addEventListener('wheel', function (e) {
    e.preventDefault();
    var rect = wrap.getBoundingClientRect();
    var mx = e.clientX - rect.left, my = e.clientY - rect.top;
    var factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    var next = Math.min(4, Math.max(0.15, scale * factor));
    tx = mx - (mx - tx) * (next / scale);
    ty = my - (my - ty) * (next / scale);
    scale = next;
    apply();
  }, { passive: false });

  wrap.addEventListener('pointerdown', function (e) {
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    wrap.classList.add('dragging'); wrap.setPointerCapture(e.pointerId);
  });
  wrap.addEventListener('pointermove', function (e) {
    if (!dragging) return;
    tx += e.clientX - lastX; ty += e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY; apply();
  });
  wrap.addEventListener('pointerup', function (e) {
    dragging = false; wrap.classList.remove('dragging');
  });

  // Hovering a node fades everything it does not touch, which is the only
  // affordable way to read a dense region.
  var nodes = [].slice.call(svg.querySelectorAll('.node'));
  var edges = [].slice.call(svg.querySelectorAll('.edge'));
  var labels = [].slice.call(svg.querySelectorAll('.edge-labels text'));
  nodes.forEach(function (node, i) {
    node.addEventListener('mouseenter', function () {
      var id = node.getAttribute('data-id');
      var keep = {};
      keep[id] = true;
      edges.forEach(function (edge, j) {
        var from = edge.getAttribute('data-from'), to = edge.getAttribute('data-to');
        var touches = from === id || to === id;
        edge.classList.toggle('dim', !touches);
        if (labels[j]) labels[j].classList.toggle('dim', !touches);
        if (touches) { keep[from] = true; keep[to] = true; }
      });
      nodes.forEach(function (other) {
        other.classList.toggle('dim', !keep[other.getAttribute('data-id')]);
      });
    });
    node.addEventListener('mouseleave', function () {
      nodes.concat(edges).concat(labels).forEach(function (el) {
        el.classList.remove('dim');
      });
    });
  });

  var fitBtn = document.getElementById('fit');
  if (fitBtn) fitBtn.addEventListener('click', fit);
  var inBtn = document.getElementById('zoomin');
  if (inBtn) inBtn.addEventListener('click', function () {
    scale = Math.min(4, scale * 1.25); apply();
  });
  var outBtn = document.getElementById('zoomout');
  if (outBtn) outBtn.addEventListener('click', function () {
    scale = Math.max(0.15, scale / 1.25); apply();
  });

  fit();
  window.addEventListener('resize', fit);
})();
"""
