"""Draw a box around the element an automated run is about to touch.

`--headed --slow-mo` shows the pages a crawl walks through but not *why* it
walked there: the browser jumps from one state to the next and the control
that caused it is never visibly distinguished from the dozens around it. At
700ms a step you can see the application moving and still have no idea which
of nine links was clicked.

This module paints a ring over the resolved element and holds it there for a
moment before the action fires, so the click is announced rather than merely
witnessed after the fact. It is a debugging aid and is not meant to be on
during a real run.

Two properties matter and both are load-bearing:

* The overlay must never be clicked. `pointer-events: none` throughout, so
  the ring cannot intercept the very action it is advertising.
* The overlay must never be *read*. Everything downstream fingerprints the
  page by inspecting it, and an injected element carrying visible text would
  land in that snapshot and change the state's identity — a highlighted crawl
  would then produce a different graph from a plain one, which would make the
  flag worse than useless. Hence `aria-hidden`, and hence `clear` being
  called before the page is next inspected rather than left to the next
  navigation.
"""

from __future__ import annotations

# Deliberately improbable, because it shares a document with the application
# under test and a collision would have us removing the page's own element.
OVERLAY_ID = "__blindspot_highlight__"

# How long the ring is held before the action fires, when `--highlight` is
# given without a number. Long enough to find the box with your eyes; short
# enough that a sixty-state crawl still finishes.
DEFAULT_DWELL_MS = 600

# Ring colour per kind of action, chosen to read against both light and dark
# pages: amber for "about to click", blue for "about to type".
_COLOURS = {
    "click": "#f59e0b",
    "fill": "#3b82f6",
}

# Field labels whose value must not be painted onto the screen. A headed run
# is often a shared screen or a recording, and `--credential password=...`
# exists precisely so real credentials can be used.
_SECRET_HINTS = ("password", "passwd", "secret", "token", "api key", "apikey",
                 "card", "cvv", "cvc", "ssn", "pin")

_SHOW_JS = """
(spec) => {
  const existing = document.getElementById(spec.id);
  if (existing) existing.remove();

  const root = document.createElement('div');
  root.id = spec.id;
  root.setAttribute('aria-hidden', 'true');
  root.setAttribute('data-blindspot', 'overlay');
  root.style.cssText = 'all:initial;position:fixed;left:0;top:0;width:0;height:0;'
    + 'z-index:2147483647;pointer-events:none;';

  const b = spec.box;
  const ring = document.createElement('div');
  ring.style.cssText =
      'position:fixed;pointer-events:none;box-sizing:border-box;'
    + `left:${b.x - 4}px;top:${b.y - 4}px;`
    + `width:${b.width + 8}px;height:${b.height + 8}px;`
    + `border:3px solid ${spec.colour};border-radius:5px;`
    + `box-shadow:0 0 0 2px rgba(0,0,0,.45), 0 0 14px 2px ${spec.colour};`
    + `background:${spec.colour}1f;`;
  root.appendChild(ring);

  if (spec.caption) {
    const tag = document.createElement('div');
    tag.textContent = spec.caption;
    // Above the element normally, below it when the element is at the very
    // top of the viewport and there is no room.
    const above = b.y > 30;
    tag.style.cssText =
        'position:fixed;pointer-events:none;box-sizing:border-box;'
      + `left:${Math.max(2, b.x - 4)}px;`
      + (above ? `top:${b.y - 28}px;` : `top:${b.y + b.height + 8}px;`)
      + `background:${spec.colour};color:#111;`
      + 'font:700 12px/1.8 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
      + 'padding:0 7px;border-radius:4px;white-space:nowrap;'
      + 'max-width:96vw;overflow:hidden;text-overflow:ellipsis;'
      + 'box-shadow:0 1px 4px rgba(0,0,0,.4);';
    root.appendChild(tag);
  }

  (document.body || document.documentElement).appendChild(root);
}
"""

_CLEAR_JS = """
(id) => { const el = document.getElementById(id); if (el) el.remove(); }
"""


def caption_for(kind: str, label: str, value: str | None = None) -> str:
    """The one-line description painted next to the ring.

    A field's value is shown because "it typed something here" is rarely the
    question — "it typed *what*" is — except when the value is a credential,
    which is masked.
    """
    label = (label or "?").strip()
    verb = "fill" if kind == "fill" else "click"
    if value is None:
        return f"{verb}  {label}"

    lowered = label.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        shown = "•" * 8
    else:
        shown = value if len(value) <= 24 else value[:23] + "…"
    return f"{verb}  {label} = {shown}"


def highlight(page, locator, caption: str, *, kind: str = "click",
              dwell_ms: int = DEFAULT_DWELL_MS) -> None:
    """Ring the element `locator` resolves to, then pause.

    Never raises. An element that scrolled away, a frame that navigated
    underneath us, a page that died — none of that should fail a run, because
    the run was going to happen with or without the decoration. The worst
    outcome of a failure here is an action you did not get to see.
    """
    if dwell_ms <= 0:
        return
    try:
        locator.scroll_into_view_if_needed(timeout=2_000)
    except Exception:
        pass
    try:
        box = locator.bounding_box(timeout=2_000)
        if not box:
            return
        page.evaluate(_SHOW_JS, {
            "id": OVERLAY_ID,
            "box": box,
            "caption": caption,
            "colour": _COLOURS.get(kind, _COLOURS["click"]),
        })
        page.wait_for_timeout(dwell_ms)
    except Exception:
        pass


def clear(page) -> None:
    """Remove the overlay.

    Called before the page is next inspected — see the module docstring. Also
    never raises: after a click that navigated, the context the overlay lived
    in is already gone, which is success by another name.
    """
    try:
        page.evaluate(_CLEAR_JS, OVERLAY_ID)
    except Exception:
        pass
