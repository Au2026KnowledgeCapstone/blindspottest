"""Load a page with Playwright and describe its UI in a form small enough to hand to an LLM.

The whole point is *not* to ship the DOM. We keep interactive, value-bearing
elements and drop everything else, so a page with 3000 nodes becomes a dozen
entries.

Shape returned by both entry points:

    {
      "url": "http://localhost:3000/profile",
      "title": "Profile",
      "elements": [
        {"id": "e1", "tag": "input", "role": "textbox", "type": "text",
         "label": "Name", "value": "Stevie", "editable": true,
         "selector": "#name"},
        ...
      ]
    }

Per-element keys, all optional except `id`, `tag` and `selector`:

    id             stable handle within one snapshot ("e1", "e2", ...)
    tag            lowercase tag name
    role           explicit role=, else derived from tag/type
    type           input/button type ("text", "checkbox", "submit", ...)
    label          accessible name (aria-label > labelledby > label > ...)
    visible_label  <label> text, only when it disagrees with `label`
    placeholder    raw placeholder attribute
    text           visible text of buttons and links
    href           resolved destination, links only
    value          current value; array for multi-select
    checked        true/false/"mixed" for checkboxes, radios, switches
    nearby_text    helper/validation/hint text near the element
    editable       present only when a user can change the value
    disabled       \
    readonly       | present only when true
    required       /
    accessible_name  the name Playwright's get_by_role matches on
    locator_strategy how the runner re-finds this element — {"type": "role",
                     "role": ..., "name": ...} where possible, falling back to
                     label / placeholder / test_id / css
    selector       CSS selector; a last-resort fallback for locator_strategy

Keys that don't apply are omitted rather than set to null, which keeps the
token cost down and stops the model from reasoning about absent fields.
"""

from __future__ import annotations

import json

# Extraction runs inside the page in one pass. Doing this in JS rather than
# through per-element Playwright calls turns N round-trips into one.
_EXTRACT_JS = r"""
() => {
  // Roles we care about when an author has put role= on a non-native element.
  // Without this allowlist, [role] drags in every banner/main/presentation node.
  const INTERACTIVE_ROLES = new Set([
    'textbox', 'searchbox', 'combobox', 'listbox', 'button', 'link',
    'checkbox', 'radio', 'switch', 'slider', 'spinbutton', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'option', 'tab', 'treeitem'
  ]);

  const INPUT_ROLES = {
    text: 'textbox', search: 'searchbox', email: 'textbox', url: 'textbox',
    tel: 'textbox', password: 'textbox', number: 'spinbutton',
    checkbox: 'checkbox', radio: 'radio', range: 'slider', color: 'textbox',
    date: 'textbox', 'datetime-local': 'textbox', month: 'textbox',
    time: 'textbox', week: 'textbox', file: 'button', submit: 'button',
    button: 'button', reset: 'button', image: 'button'
  };

  // Input types that are controls, not fields: they hold no user value.
  const VALUELESS_INPUTS = new Set(['submit', 'button', 'reset', 'image', 'hidden']);

  // Walking past one of these means we've left the field and entered the page.
  const LAYOUT_BOUNDARIES = new Set([
    'FORM', 'FIELDSET', 'SECTION', 'MAIN', 'ARTICLE', 'NAV', 'ASIDE',
    'HEADER', 'FOOTER', 'BODY', 'HTML', 'TABLE', 'UL', 'OL'
  ]);

  const inputType = (el) => (el.getAttribute('type') || 'text').toLowerCase();
  const squash = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = (el) => squash(el.innerText || el.textContent);

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

  // Text of an associated <label> element only — not the full name chain.
  // Tracked separately so we can spot a visible label that disagrees with the
  // accessible name, which is itself a finding.
  function labelElementText(el) {
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      const t = lab && textOf(lab);
      if (t) return t;
    }
    // <label>Name <input></label> — strip controls so we don't echo the value.
    const wrapping = el.closest('label');
    if (wrapping) {
      const clone = wrapping.cloneNode(true);
      clone.querySelectorAll('input, textarea, select, button').forEach((n) => n.remove());
      const t = squash(clone.textContent);
      if (t) return t;
    }
    return null;
  }

  // Accessible name, roughly in spec priority order. Element *content* is
  // deliberately excluded: button/link text is reported as `text` instead, so
  // the two never duplicate each other.
  function accessibleName(el) {
    const tag = el.tagName.toLowerCase();
    const aria = squash(el.getAttribute('aria-label'));
    if (aria) return aria;

    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const parts = labelledby.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map(textOf)
        .filter(Boolean);
      if (parts.length) return parts.join(' ');
    }

    const labelled = labelElementText(el)
        || squash(el.getAttribute('placeholder'))
        || squash(el.getAttribute('title'));
    if (labelled) return labelled;

    // `name` is a form submission key, not an accessible name. It is a decent
    // last resort for an unlabelled field, but never for a button: a button's
    // name comes from its content, and reporting name="action" here would hand
    // the runner a locator that matches nothing.
    const type = tag === 'input' ? inputType(el) : null;
    const isButton = tag === 'button' || VALUELESS_INPUTS.has(type);
    return isButton ? null : (el.getAttribute('name') || null);
  }

  // Context that isn't the label: helper text, character limits, validation
  // messages, "saves automatically" notices. Often where a blindspot hides.
  const NEARBY_LIMIT = 160;

  function nearbyText(el, exclude) {
    const seen = new Set(exclude.filter(Boolean).map((s) => s.toLowerCase()));

    // An explicit description always wins over anything we infer by proximity.
    const describedby = el.getAttribute('aria-describedby');
    if (describedby) {
      const parts = describedby.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map(textOf)
        .filter(Boolean);
      if (parts.length) return parts.join(' ').slice(0, NEARBY_LIMIT);
    }

    // Otherwise walk up looking for this element's *field wrapper*. The test
    // is how many controls an ancestor holds, not how much text: a wrapper
    // holds this one control, whereas a <form> holds all of them and would
    // hand back the entire page.
    let node = el.parentElement;
    for (let depth = 0; node && depth < 3; depth++, node = node.parentElement) {
      if (LAYOUT_BOUNDARIES.has(node.tagName)) break;
      if (node.querySelectorAll(CANDIDATES).length > 2) break;

      const clone = node.cloneNode(true);
      clone.querySelectorAll(CANDIDATES.concat(', label')).forEach((n) => n.remove());
      const text = squash(clone.textContent);
      if (!text) continue;
      if (text.length > 400) break;

      const residue = text.split(/\s*[|•·]\s*|\s{2,}/)
        .map(squash)
        .filter((part) => part && !seen.has(part.toLowerCase()))
        .join(' ');
      if (residue) return residue.slice(0, NEARBY_LIMIT);
    }
    return null;
  }

  function roleFor(el) {
    const explicit = squash(el.getAttribute('role'));
    if (explicit) return explicit.split(' ')[0];

    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
    if (tag === 'button') return 'button';
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (tag === 'input') return INPUT_ROLES[inputType(el)] || 'textbox';
    if (el.isContentEditable) return 'textbox';
    return null;
  }

  const isCheckable = (el) =>
    (el.tagName === 'INPUT' && ['checkbox', 'radio'].includes(inputType(el)))
    || el.hasAttribute('aria-checked');

  // Checked state is reported on its own rather than squeezed into `value`,
  // since a checkbox's `value` attribute is the submitted payload ("on"),
  // which is a different fact from whether the box is ticked.
  function checkedFor(el) {
    if (el.tagName === 'INPUT' && ['checkbox', 'radio'].includes(inputType(el))) {
      return el.indeterminate ? 'mixed' : el.checked;
    }
    const aria = el.getAttribute('aria-checked');
    if (aria === 'mixed') return 'mixed';
    if (aria === 'true' || aria === 'false') return aria === 'true';
    return null;
  }

  // Current user-visible value. Arrays for multi-select.
  function valueFor(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const type = inputType(el);
      if (VALUELESS_INPUTS.has(type)) return null;
      // For checkables the payload value only matters when it isn't the
      // default, so we don't emit "on" for every checkbox on the page.
      if (type === 'checkbox' || type === 'radio') {
        const v = el.getAttribute('value');
        return v && v !== 'on' ? v : null;
      }
      if (type === 'file') return Array.from(el.files || []).map((f) => f.name).join(', ');
      return el.value;
    }
    if (tag === 'textarea') return el.value;
    if (tag === 'select') {
      const selected = Array.from(el.selectedOptions).map((o) => o.value);
      return el.multiple ? selected : (selected.length ? selected[0] : '');
    }
    if (el.isContentEditable) return textOf(el);
    return null;
  }

  const isDisabled = (el) =>
    !!el.disabled || el.getAttribute('aria-disabled') === 'true';

  const isReadOnly = (el) =>
    !!el.readOnly || el.getAttribute('aria-readonly') === 'true';

  // Can a user change this element's value? Used by the runner to decide what
  // to perturb, so disabled/readonly are folded in rather than reported alone.
  function isEditable(el) {
    if (isDisabled(el)) return false;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      if (VALUELESS_INPUTS.has(inputType(el))) return false;
      return !isReadOnly(el);
    }
    if (tag === 'textarea') return !isReadOnly(el);
    if (tag === 'select') return true;
    if (isCheckable(el)) return !isReadOnly(el);
    return !!el.isContentEditable;
  }

  // How the runner should re-find this element after a reload. Semantic
  // locators come first: role + accessible name describes what an element IS,
  // so it survives re-renders, reordering, and changed markup in a way a CSS
  // path does not. The `e2`-style id only connects the model's answer back to
  // this snapshot; it is never used to drive the browser.
  function locatorStrategy(el, role, accessibleName, placeholder) {
    if (role && accessibleName) {
      return { type: 'role', role: role, name: accessibleName };
    }
    const labelText = labelElementText(el);
    if (labelText) return { type: 'label', name: labelText };
    if (placeholder) return { type: 'placeholder', name: placeholder };
    for (const attr of ['data-testid', 'data-test-id', 'data-test']) {
      const val = el.getAttribute(attr);
      if (val) return { type: 'test_id', value: val };
    }
    return { type: 'css', selector: selectorFor(el) };
  }

  // Prefer selectors an author chose over positional ones, since the runner
  // re-resolves these after a reload and nth-of-type paths drift.
  function selectorFor(el) {
    const unique = (sel) => {
      try { return document.querySelectorAll(sel).length === 1 ? sel : null; }
      catch { return null; }
    };

    if (el.id) {
      const hit = unique(`#${CSS.escape(el.id)}`);
      if (hit) return hit;
    }
    for (const attr of ['data-testid', 'data-test-id', 'data-test', 'name']) {
      const val = el.getAttribute(attr);
      if (val) {
        const hit = unique(`${el.tagName.toLowerCase()}[${attr}="${CSS.escape(val)}"]`);
        if (hit) return hit;
      }
    }

    const parts = [];
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      if (node.id) {
        const hit = unique(`#${CSS.escape(node.id)}`);
        if (hit) { parts.unshift(hit); break; }
      }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      if (node.tagName === 'HTML') break;
    }
    return parts.join(' > ');
  }

  const CANDIDATES = 'input, textarea, select, button, a[href], [contenteditable=""], [contenteditable="true"], [role]';
  const NATIVE = new Set(['input', 'textarea', 'select', 'button', 'a']);

  const elements = [];
  let n = 0;

  for (const el of document.querySelectorAll(CANDIDATES)) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && inputType(el) === 'hidden') continue;

    const role = roleFor(el);
    // Keep native controls outright; keep custom widgets only if they claim an
    // interactive role. Everything else is layout noise.
    const interesting = NATIVE.has(tag) || el.isContentEditable || (role && INTERACTIVE_ROLES.has(role));
    if (!interesting) continue;
    if (!isVisible(el)) continue;

    const entry = { id: `e${++n}`, tag };
    if (role) entry.role = role;
    if (tag === 'input' || tag === 'button') entry.type = inputType(el);

    const label = accessibleName(el);
    if (label) entry.label = label;

    // Surfaced only when the visible label disagrees with the accessible name
    // (e.g. aria-label="Email" over a <label>Username</label>). Identical text
    // would just be tokens spent twice.
    const visible = labelElementText(el);
    if (visible && visible !== label) entry.visible_label = visible;

    const placeholder = squash(el.getAttribute('placeholder'));
    if (placeholder) entry.placeholder = placeholder;

    // Buttons and links are identified by what they say, not what they hold.
    let text = null;
    if (tag === 'button' || role === 'button' || role === 'link') {
      text = textOf(el) || squash(el.getAttribute('value'));
      if (text) entry.text = text;
    }

    // Where a link goes, resolved against the document. A crawler needs this
    // to decide whether following it leaves the region under test, which it
    // has to know *before* clicking rather than after.
    if (tag === 'a' && el.hasAttribute('href')) {
      const raw = el.getAttribute('href');
      if (raw && !raw.startsWith('javascript:')) entry.href = el.href;
    }

    const value = valueFor(el);
    if (value !== null && value !== undefined) entry.value = value;

    const checked = checkedFor(el);
    if (checked !== null) entry.checked = checked;

    if (isEditable(el)) entry.editable = true;
    if (isDisabled(el)) entry.disabled = true;
    if (isReadOnly(el)) entry.readonly = true;
    if (el.required || el.getAttribute('aria-required') === 'true') entry.required = true;

    const nearby = nearbyText(el, [label, visible, placeholder, text]);
    if (nearby) entry.nearby_text = nearby;

    // For a control, the accessible name is its label; for a button or link
    // it is its own text. This is what Playwright matches on in get_by_role.
    const accName = label || text || null;
    if (accName) entry.accessible_name = accName;

    entry.locator_strategy = locatorStrategy(el, role, accName, placeholder);
    entry.selector = selectorFor(el);
    elements.push(entry);
  }

  return { url: location.href, title: document.title, elements };
}
"""


def inspect_with_page(page) -> dict:
    """Describe whatever an already-open Playwright page is currently showing.

    This is the primitive the runner wants: it owns the page across a
    fill -> save -> reload cycle and snapshots at each step.
    """
    page.wait_for_load_state("domcontentloaded")
    return page.evaluate(_EXTRACT_JS)


def inspect_page(
    url: str,
    *,
    headless: bool = True,
    timeout: int = 15_000,
    wait_until: str = "networkidle",
) -> dict:
    """Open `url` in a throwaway browser and describe it. Convenience wrapper."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            # Client-rendered pages need a beat; a quiet network is the best
            # available signal, but never let it fail the whole inspection.
            try:
                page.wait_for_load_state(wait_until, timeout=timeout)
            except Exception:
                pass
            return inspect_with_page(page)
        finally:
            browser.close()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        sys.exit("usage: python -m discovery.page_inspector <url> [--headed]")
    print(
        json.dumps(
            inspect_page(sys.argv[1], headless="--headed" not in sys.argv),
            indent=2,
        )
    )
