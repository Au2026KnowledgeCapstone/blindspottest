"""The *readable* surface of a page: values a flow's outcome is asserted about.

`page_inspector` answers "what can a user do here" and deliberately drops
everything non-interactive — a 3000-node DOM becomes a dozen controls. That is
exactly right for driving an application and exactly wrong for checking one.
The facts a flow invariant asserts over are almost never in a form field:

    the total equals the sum of the lines shown      rendered text
    the rows are in the order that was asked for     rendered text
    the stated count matches the rows displayed      rendered text

None of that appears in a control snapshot, so a model asked to bind
`line_totals` to a selector from one would have to invent the selector. This
module exists so it does not have to.

What it returns is a *grouped* view. A page with five table rows does not
yield five entries; it yields one entry whose selector matches all five, with
a sample of their text and a count. That grouping is the point — a flow
invariant reads a collection, so the useful selector is the one that matches
the collection rather than one of its members.

Entries are ranked so the numeric, repeated, and explicitly-identified ones
come first. A model choosing bindings reads the top of this list and finds
totals and row values there, which is what it needs; the long tail of prose
paragraphs is still present but never crowds them out.

Nothing here judges correctness, and nothing here is specific to an
application. It takes an open page and returns a list of selectors with
samples.
"""

from __future__ import annotations

import json

# One pass inside the page. Per-element Playwright calls would turn this into
# hundreds of round-trips on a table of any size.
_READABLE_JS = r"""
() => {
  // Tags whose text is a *value* rather than a layout container. A <div>
  // holding three rows is not itself a readable value; the <td>s inside it
  // are. Containers still get considered when they hold no element children.
  const VALUE_TAGS = new Set([
    'TD', 'TH', 'DD', 'DT', 'LI', 'SPAN', 'P', 'STRONG', 'EM', 'B', 'SMALL',
    'CODE', 'OUTPUT', 'TIME', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'LABEL',
    'CAPTION', 'FIGCAPTION', 'BLOCKQUOTE', 'ADDRESS', 'ABBR'
  ]);

  // Controls are page_inspector's job. Their text is a label or a value the
  // user typed, neither of which is an outcome to assert about.
  const CONTROL_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A', 'OPTION']);

  const MAX_TEXT = 120;
  const MAX_SAMPLES = 4;
  const MAX_GROUPS = 60;

  const squash = (s) => (s || '').replace(/\s+/g, ' ').trim();

  function isVisible(el) {
    const style = getComputedStyle(el);
    if (style.display === 'none') return false;
    if (style.visibility === 'hidden' || style.visibility === 'collapse') return false;
    if (parseFloat(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    if (el.closest('[aria-hidden="true"]')) return false;
    return true;
  }

  // Text belonging to this element rather than to its descendants. A <tr>
  // whose <td>s hold the data would otherwise report the whole row as one
  // value and make a per-column assertion impossible.
  function ownText(el) {
    let out = '';
    for (const node of el.childNodes) {
      if (node.nodeType === 3) out += node.textContent;
    }
    return squash(out);
  }

  // Does the text look like a number? Currency symbols, thousands separators
  // and trailing units are stripped, because `$1,299.00` and `1299` are the
  // same fact rendered differently and an ordering check needs the number.
  const NUMERIC = /^[^\d\-+]{0,3}[-+]?[\d][\d,\s]*(\.\d+)?[^\d]{0,12}$/;
  const looksNumeric = (text) => NUMERIC.test(text) && /\d/.test(text);

  const cssEscape = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;

  // Classes a bundler or utility framework generated carry no meaning and do
  // not survive a rebuild, so a selector built on one is a selector that
  // breaks on the next deploy for no reason.
  const UNSTABLE_CLASS = /^(?:[a-z]+-)?[a-f0-9]{6,}$|^(?:css|sc|jsx|emotion)-|\d{4,}/i;
  const stableClasses = (el) =>
    Array.from(el.classList).filter((c) => c && !UNSTABLE_CLASS.test(c));

  function matches(selector) {
    try { return document.querySelectorAll(selector); }
    catch { return null; }
  }

  // Build selectors from most to least specific, preferring ones that match
  // the whole sibling group. Returned in preference order; the caller takes
  // the first that actually resolves to this element.
  function candidateSelectors(el) {
    const tag = el.tagName.toLowerCase();
    const out = [];

    if (el.id) out.push('#' + cssEscape(el.id));

    // A table cell's identity is its column, not its classes. `td.num` looks
    // specific and is not: a price column and a rating column both carry it,
    // so binding to it conflates two unrelated series into one list and an
    // ordering assertion over the result is meaningless. Column position is
    // checked first for exactly that reason.
    const positional = [];
    const cell = el.closest('td, th');
    if (cell && cell === el) {
      const row = cell.parentElement;
      if (row) {
        const index = Array.from(row.children).indexOf(cell) + 1;
        const table = cell.closest('table');
        const section = cell.closest('thead') ? 'thead' : 'tbody';
        if (table) {
          const base = table.id
            ? '#' + cssEscape(table.id)
            : (stableClasses(table).length ? 'table.' + cssEscape(stableClasses(table)[0]) : 'table');

          // A totals table renders its lines and its grand total as sibling
          // rows in one column, distinguished only by a class on the row.
          // Without that class in the selector, `sum(line_totals)` would
          // include the total it is being compared against and the invariant
          // could never fail. The classless rows get a :not() form for the
          // same reason — they have to exclude the row that is marked.
          const siblings = Array.from(row.parentElement ? row.parentElement.children : [])
            .filter((r) => r.tagName === 'TR');
          const own = stableClasses(row);
          const marked = new Set();
          for (const r of siblings) for (const c of stableClasses(r)) marked.add(c);

          let rowPart = 'tr';
          if (own.length) {
            rowPart = 'tr' + own.map((c) => '.' + cssEscape(c)).join('');
          } else if (marked.size) {
            rowPart = 'tr' + Array.from(marked)
              .map((c) => ':not(.' + cssEscape(c) + ')').join('');
          }

          positional.push(`${base} ${section} ${rowPart} ${tag}:nth-child(${index})`);
          positional.push(`${base} ${section} tr ${tag}:nth-child(${index})`);
          positional.push(`${base} tr ${tag}:nth-child(${index})`);
        }
      }
    }
    out.push(...positional);

    const classes = stableClasses(el);
    if (classes.length) {
      const dotted = classes.map((c) => '.' + cssEscape(c)).join('');
      out.push(tag + dotted);
      out.push(dotted);
    }

    // Scope by the nearest identified ancestor. This is what turns an
    // ambiguous `.amount` into `#order-summary .amount` on a page holding
    // several unrelated amount lists.
    let scope = null;
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      if (p.id) { scope = '#' + cssEscape(p.id); break; }
      const pc = stableClasses(p);
      if (pc.length) { scope = '.' + cssEscape(pc[0]); break; }
    }
    if (scope && classes.length) {
      out.push(scope + ' ' + tag + classes.map((c) => '.' + cssEscape(c)).join(''));
    }
    if (scope) out.push(scope + ' ' + tag);
    out.push(tag);
    return out;
  }

  const groups = new Map();

  for (const el of document.querySelectorAll('body *')) {
    if (CONTROL_TAGS.has(el.tagName)) continue;
    if (el.closest('script, style, head, noscript, template')) continue;

    const text = ownText(el);
    if (!text || text.length > MAX_TEXT) continue;
    if (!VALUE_TAGS.has(el.tagName) && el.children.length) continue;
    if (!isVisible(el)) continue;

    // Find the most specific selector that resolves to this element, and
    // record how many elements it matches so a repeated group is visible as
    // a group rather than as N near-duplicate entries.
    let chosen = null;
    let total = 0;
    for (const selector of candidateSelectors(el)) {
      const found = matches(selector);
      if (!found || !found.length) continue;
      if (!Array.prototype.includes.call(found, el)) continue;
      chosen = selector;
      total = found.length;
      break;
    }
    if (!chosen) continue;

    let group = groups.get(chosen);
    if (!group) {
      group = { selector: chosen, count: total, samples: [], numeric: true, has_id: chosen.startsWith('#') };
      groups.set(chosen, group);
    }
    if (group.samples.length < MAX_SAMPLES) group.samples.push(text);
    if (!looksNumeric(text)) group.numeric = false;
  }

  // Numeric groups first, then repeated ones, then explicitly identified
  // ones. A flow invariant asserting over a collection or a total finds what
  // it needs in the first few entries.
  const ranked = Array.from(groups.values()).sort((a, b) => {
    if (a.numeric !== b.numeric) return a.numeric ? -1 : 1;
    const aRepeat = a.count > 1, bRepeat = b.count > 1;
    if (aRepeat !== bRepeat) return aRepeat ? -1 : 1;
    if (a.has_id !== b.has_id) return a.has_id ? -1 : 1;
    return b.count - a.count;
  });

  return {
    url: location.href,
    title: document.title,
    values: ranked.slice(0, MAX_GROUPS).map((g) => ({
      selector: g.selector,
      count: g.count,
      samples: g.samples,
      numeric: g.numeric,
    })),
  };
}
"""


def readable_with_page(page) -> dict:
    """Describe the value-bearing text of an already-open page.

    Shape:

        {
          "url": "...",
          "title": "...",
          "values": [
            {"selector": "#order-total", "count": 1,
             "samples": ["$29.99"], "numeric": true},
            {"selector": ".line .amount", "count": 3,
             "samples": ["$12.80", "$7.20", "$9.99"], "numeric": true},
          ]
        }

    `count` is how many elements the selector matches, which is what makes a
    collection selector distinguishable from a single-value one.
    """
    page.wait_for_load_state("domcontentloaded")
    return page.evaluate(_READABLE_JS)


def summarize(readable: dict, *, limit: int = 24) -> str:
    """Render the readable surface compactly, for an LLM prompt.

    The full structure is mostly punctuation by volume. This is the form a
    model is shown: one line per selector, with what it matches and a sample,
    so choosing a binding is reading a list rather than parsing JSON.
    """
    lines = [f"# readable values at {readable.get('url', '')}"]
    for entry in readable.get("values", [])[:limit]:
        samples = ", ".join(repr(s) for s in entry.get("samples", [])[:3])
        kind = "number" if entry.get("numeric") else "text"
        lines.append(
            f"  {entry['selector']}  (matches {entry['count']}, {kind})  {samples}"
        )
    if not readable.get("values"):
        lines.append("  (no readable values found)")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    from discovery.page_inspector import inspect_page  # noqa: F401  (parity)

    if len(sys.argv) < 2:
        sys.exit("usage: python -m discovery.readable <url> [--headed] [--text]")

    from playwright.sync_api import sync_playwright

    url = sys.argv[1]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless="--headed" not in sys.argv)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            found = readable_with_page(page)
        finally:
            browser.close()

    if "--text" in sys.argv:
        print(summarize(found, limit=60))
    else:
        print(json.dumps(found, indent=2))
