"""HTML for the demo app: one page shell plus the handful of components the
flows are built from.

Two rules hold everywhere in this file, because BlindSpot's discovery stage
reads pages the way a screen reader does:

  * every control has a real accessible name — a <label for>, or aria-label
    where no visible label exists — so `get_by_role(..., name=...)` can find it
    again after a reload;

  * the sound and broken builds render through exactly the same code. If the
    broken build ever said something different, finding the bug would be a
    reading-comprehension exercise instead of a testing one.
"""

from __future__ import annotations

from html import escape

CSS = """
:root {
  --bg: #f6f7f9; --panel: #fff; --line: #e3e6ea; --ink: #16191d;
  --muted: #61686f; --accent: #2563eb; --accent-ink: #fff;
  --ok-bg: #dcfce7; --ok-ink: #14532d; --warn-bg: #fef3c7; --warn-ink: #713f12;
  --bad-bg: #fee2e2; --bad-ink: #7f1d1d;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
       font: 15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; }
a { color: var(--accent); }
.shell { display: grid; grid-template-columns: 15rem 1fr; min-height: 100vh; }
.sidebar { background: #10131a; color: #cbd2da; padding: 1.4rem 1rem; }
.brand { color: #fff; font-weight: 700; font-size: 1.05rem; letter-spacing: .01em;
         display: block; text-decoration: none; margin-bottom: 1.4rem; }
.brand span { display: block; font-weight: 400; font-size: .78rem; color: #8b949e; }
.sidebar nav a { display: block; padding: .45rem .6rem; margin-bottom: .15rem;
                 border-radius: 6px; color: #cbd2da; text-decoration: none; font-size: .92rem; }
.sidebar nav a:hover { background: #1c212b; color: #fff; }
.sidebar nav a[aria-current="page"] { background: #232a36; color: #fff; font-weight: 600; }
.sidebar .group { margin: 1.2rem 0 .4rem; font-size: .7rem; text-transform: uppercase;
                  letter-spacing: .08em; color: #6e7781; }
.main { padding: 2rem 2.4rem 4rem; max-width: 62rem; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 0 0 .8rem; }
.sub { color: var(--muted); margin: 0 0 1.6rem; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
        padding: 1.4rem 1.5rem; margin-bottom: 1.2rem; }
.card > :last-child { margin-bottom: 0; }
label { display: block; font-weight: 600; margin: 1rem 0 .3rem; font-size: .9rem; }
input[type=text], input[type=email], input[type=password], input[type=number],
textarea, select {
  width: 100%; padding: .5rem .6rem; font: inherit; color: inherit;
  background: #fff; border: 1px solid #c4c9d0; border-radius: 6px;
}
textarea { min-height: 5.5rem; resize: vertical; }
input[type=number] { width: 5.5rem; }
.hint { color: var(--muted); font-size: .82rem; margin: .3rem 0 0; }
button, .btn { display: inline-block; margin-top: 1.2rem; padding: .55rem 1.1rem;
       font: inherit; font-weight: 600; border: 1px solid transparent; border-radius: 6px;
       background: var(--accent); color: var(--accent-ink); text-decoration: none;
       cursor: pointer; }
button.secondary, .btn.secondary { background: #fff; color: var(--ink); border-color: #c4c9d0; }
button.danger { background: #fff; color: #b42318; border-color: #f0c2bd; }
.actions { display: flex; gap: .6rem; align-items: center; flex-wrap: wrap; }
.flash { margin: 0 0 1.2rem; padding: .65rem .9rem; border-radius: 8px; font-size: .92rem; }
.flash.ok { background: var(--ok-bg); color: var(--ok-ink); }
.flash.warn { background: var(--warn-bg); color: var(--warn-ink); }
.flash.bad { background: var(--bad-bg); color: var(--bad-ink); }
table { width: 100%; border-collapse: collapse; font-size: .93rem; }
th, td { text-align: left; padding: .6rem .5rem; border-bottom: 1px solid var(--line); }
th { font-size: .76rem; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
tbody tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; }
.badge { display: inline-block; padding: .1rem .5rem; border-radius: 99px;
         font-size: .75rem; font-weight: 600; background: #eef1f4; color: #3b434b; }
.badge.active { background: var(--ok-bg); color: var(--ok-ink); }
.badge.paused { background: var(--warn-bg); color: var(--warn-ink); }
.steps { display: flex; gap: .4rem; list-style: none; padding: 0; margin: 0 0 1.4rem;
         flex-wrap: wrap; font-size: .82rem; }
.steps li { padding: .25rem .7rem; border-radius: 99px; background: #eceff3; color: var(--muted); }
.steps li[aria-current="step"] { background: var(--accent); color: #fff; font-weight: 600; }
.steps li.done { background: var(--ok-bg); color: var(--ok-ink); }
.totals { width: 100%; max-width: 22rem; margin-left: auto; }
.totals td { border: 0; padding: .25rem 0; }
.totals tr.grand td { border-top: 1px solid var(--line); padding-top: .6rem;
                      font-weight: 700; font-size: 1.05rem; }
.filters { display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-end; }
.filters > div { flex: 1 1 12rem; }
.filters label { margin-top: 0; }
.filters button { margin-top: 0; }
.empty { color: var(--muted); padding: 1.2rem 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: 1rem; }
.grid .card { margin: 0; }
.grid .card p { color: var(--muted); font-size: .9rem; }
dl.facts { display: grid; grid-template-columns: 9rem 1fr; gap: .5rem 1rem; margin: 0; }
dl.facts dt { color: var(--muted); font-size: .88rem; }
dl.facts dd { margin: 0; }
"""


def esc(value) -> str:
    return escape(str(value), quote=True)


# --------------------------------------------------------------------------
# Page shell
# --------------------------------------------------------------------------

NAV = [
    ("group", "Workspace"),
    ("/", "Overview"),
    ("/projects", "Projects"),
    ("/catalog", "Catalog"),
    ("group", "Order"),
    ("/cart", "Cart"),
    ("group", "Settings"),
    ("/profile", "Profile"),
    ("/project-settings", "Project settings"),
    ("/account", "Account"),
]


def _nav(prefix: str, active: str) -> str:
    out = ['<nav aria-label="Main">']
    for path, label in NAV:
        if path == "group":
            out.append(f'<div class="group">{esc(label)}</div>')
            continue
        href = prefix + path if path != "/" else (prefix or "/")
        current = ' aria-current="page"' if path == active else ""
        out.append(f'<a href="{esc(href)}"{current}>{esc(label)}</a>')
    out.append("</nav>")
    return "".join(out)


def page(req, *, title: str, body: str, active: str = "", subtitle: str = "") -> str:
    """The application shell. Identical for both variants by construction."""
    flash = "".join(
        f'<p class="flash {tone}" role="status">{esc(text)}</p>'
        for tone, text in req.flashes
    )
    sub = f'<p class="sub">{esc(subtitle)}</p>' if subtitle else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(title)} · Acme Console</title>"
        "<link rel='stylesheet' href='/static/app.css'>"
        "</head><body><div class='shell'>"
        f"<aside class='sidebar'>"
        f"<a class='brand' href='{esc(req.prefix or '/')}'>Acme Console"
        f"<span>demo workspace</span></a>{_nav(req.prefix, active)}</aside>"
        f"<main class='main'><h1>{esc(title)}</h1>{sub}{flash}{body}</main>"
        "</div></body></html>"
    )


# --------------------------------------------------------------------------
# Form controls
# --------------------------------------------------------------------------


def text_field(name: str, label: str, value: str = "", *, hint: str = "",
               type_: str = "text", required: bool = False,
               autocomplete: str | None = None) -> str:
    extra = ' required' if required else ""
    if autocomplete:
        extra += f' autocomplete="{esc(autocomplete)}"'
    note = f'<p class="hint" id="{esc(name)}-hint">{esc(hint)}</p>' if hint else ""
    described = f' aria-describedby="{esc(name)}-hint"' if hint else ""
    return (
        f'<label for="{esc(name)}">{esc(label)}</label>'
        f'<input type="{esc(type_)}" id="{esc(name)}" name="{esc(name)}"'
        f' value="{esc(value)}"{described}{extra}>{note}'
    )


def textarea_field(name: str, label: str, value: str = "", *, hint: str = "") -> str:
    note = f'<p class="hint" id="{esc(name)}-hint">{esc(hint)}</p>' if hint else ""
    described = f' aria-describedby="{esc(name)}-hint"' if hint else ""
    return (
        f'<label for="{esc(name)}">{esc(label)}</label>'
        f'<textarea id="{esc(name)}" name="{esc(name)}"{described}>'
        f'{esc(value)}</textarea>{note}'
    )


def select_field(name: str, label: str, options, value: str = "",
                 *, hint: str = "") -> str:
    """`options` is an iterable of (value, text) pairs."""
    opts = "".join(
        f'<option value="{esc(v)}"{" selected" if str(v) == str(value) else ""}>'
        f"{esc(t)}</option>"
        for v, t in options
    )
    note = f'<p class="hint" id="{esc(name)}-hint">{esc(hint)}</p>' if hint else ""
    described = f' aria-describedby="{esc(name)}-hint"' if hint else ""
    return (
        f'<label for="{esc(name)}">{esc(label)}</label>'
        f'<select id="{esc(name)}" name="{esc(name)}"{described}>{opts}</select>{note}'
    )


def radio_group(name: str, legend: str, options, value: str) -> str:
    """`options` is an iterable of (value, label, description) triples."""
    rows = []
    for v, label, description in options:
        checked = " checked" if str(v) == str(value) else ""
        note = f' <span class="hint">{esc(description)}</span>' if description else ""
        rows.append(
            f'<div><label for="{esc(name)}-{esc(v)}" style="font-weight:400">'
            f'<input type="radio" id="{esc(name)}-{esc(v)}" name="{esc(name)}"'
            f' value="{esc(v)}"{checked}> {esc(label)}{note}</label></div>'
        )
    return (
        f'<fieldset style="border:0;padding:0;margin:1rem 0 0">'
        f'<legend style="font-weight:600;font-size:.9rem">{esc(legend)}</legend>'
        + "".join(rows)
        + "</fieldset>"
    )


def button(text: str, *, kind: str = "", name: str = "", value: str = "") -> str:
    cls = f' class="{esc(kind)}"' if kind else ""
    attrs = f' name="{esc(name)}" value="{esc(value)}"' if name else ""
    return f'<button type="submit"{cls}{attrs}>{esc(text)}</button>'


def link_button(href: str, text: str, *, kind: str = "secondary") -> str:
    return f'<a class="btn {esc(kind)}" href="{esc(href)}">{esc(text)}</a>'


def card(body: str, *, title: str = "") -> str:
    heading = f"<h2>{esc(title)}</h2>" if title else ""
    return f'<section class="card">{heading}{body}</section>'


def steps(names, current: str) -> str:
    """Progress indicator. Steps before the current one are marked done."""
    index = names.index(current) if current in names else -1
    items = []
    for position, name in enumerate(names):
        if position == index:
            items.append(f'<li aria-current="step">{esc(name)}</li>')
        elif position < index:
            items.append(f'<li class="done">{esc(name)}</li>')
        else:
            items.append(f"<li>{esc(name)}</li>")
    return f'<ol class="steps">{"".join(items)}</ol>'


def totals_table(rows, grand) -> str:
    """`rows` is (label, formatted amount); `grand` is the final pair."""
    body = "".join(
        f"<tr><td>{esc(label)}</td><td class='num'>{esc(amount)}</td></tr>"
        for label, amount in rows
    )
    label, amount = grand
    body += (
        f"<tr class='grand'><td>{esc(label)}</td>"
        f"<td class='num'>{esc(amount)}</td></tr>"
    )
    return f"<table class='totals'><tbody>{body}</tbody></table>"
