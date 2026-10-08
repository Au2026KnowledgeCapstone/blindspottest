"""Render run records as a single self-contained HTML dashboard.

    python -m reporting.dashboard            # all runs -> runs/dashboard.html
    python -m reporting.dashboard --open     # and open it

No server, no JavaScript, no build step. Data is inlined at generation time
and progressive disclosure uses <details>, so the output is one file you can
open from disk, email, or drop in a PR.

It is a dashboard, not a document: what needs attention is surfaced first, and
status is encoded in shape and colour as well as in words, so a wall of runs
is scannable without reading.
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime
from pathlib import Path

RUNS_DIR = Path("runs")

_ORDER = {"violation": 0, "inconclusive": 1, "holds": 2}
_LABEL = {"violation": "Regression", "inconclusive": "Inconclusive", "holds": "Pass"}


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _repr(value) -> str:
    """Show values the way the terminal does, so quotes and blanks are visible."""
    return _esc(repr(value))


def _when(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).strftime(
            "%d %b %Y · %H:%M UTC"
        )
    except (ValueError, AttributeError):
        return _esc(stamp)


def load_runs(directory: Path) -> list[dict]:
    """Newest first. A malformed file is skipped rather than killing the render."""
    runs = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        record["_file"] = path.name
        runs.append(record)
    return runs


# --------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------


def _steps(test: dict) -> str:
    steps = test.get("steps") or []
    if not steps:
        return ""
    rows = []
    for step in steps:
        cls = "s-ok" if step["ok"] else "s-fail"
        if step.get("teardown"):
            cls += " s-td"
        value = ""
        if step.get("records"):
            value = f'<span class="sv">{_repr(step.get("value"))}</span>'
        detail = (
            f'<span class="sd">{_esc(step["detail"])}</span>'
            if step.get("detail")
            else ""
        )
        mark = "●" if step["ok"] else "✕"
        rows.append(
            f'<li class="{cls}"><span class="sm">{mark}</span>'
            f'<span class="sn">{_esc(step["step"])}</span>{value}{detail}</li>'
        )
    n_fail = sum(1 for s in steps if not s["ok"])
    caption = f"{len(steps)} steps" + (f", {n_fail} failed" if n_fail else "")
    return (
        f'<details class="trace"><summary>{caption}</summary>'
        f'<ol class="steps">{"".join(rows)}</ol></details>'
    )


def _test(test: dict) -> str:
    result = test.get("result", "inconclusive")
    label = _LABEL.get(result, result)

    facts = [
        ("Invariant", f'<code>{_esc(test.get("invariant"))}</code>'),
        ("Commit", _esc(test.get("commit_action") or "— autosave")),
        ("Applicability", f'{test.get("applicability_confidence", 0):.2f}'),
        ("Original", _repr(test.get("original"))),
        ("Committed", _repr(test.get("committed"))),
        ("After reload", _repr(test.get("observed_after_reload"))),
    ]
    fact_html = "".join(
        f'<div class="fact"><dt>{k}</dt><dd>{v}</dd></div>' for k, v in facts
    )

    warn = ""
    if test.get("restored") is False:
        warn = (
            '<p class="warn">Original value was not restored — '
            "test data may remain in the application.</p>"
        )

    reasoning = (
        f'<p class="why"><span>Model’s reading</span>'
        f'{_esc(test.get("applicability_reasoning"))}</p>'
        if test.get("applicability_reasoning")
        else ""
    )

    return f"""
    <article class="test t-{result}">
      <header>
        <span class="pill p-{result}">{label}</span>
        <h4>{_esc(test.get("target") or "—")}</h4>
        <span class="dur">{test.get("duration_ms", 0)} ms</span>
      </header>
      <p class="detail">{_esc(test.get("detail"))}</p>
      <dl class="facts">{fact_html}</dl>
      {reasoning}{warn}{_steps(test)}
    </article>"""


def _discards(run: dict) -> str:
    rejected = run.get("rejected_candidates") or []
    skipped = run.get("skipped") or []
    if not rejected and not skipped:
        return ""

    rows = []
    for r in rejected:
        rows.append(
            f'<li><span class="tag tag-rej">rejected</span>'
            f'<code>{_esc(r.get("field_id"))}</code>'
            f'<span class="rsn">{_esc(r.get("reason"))}</span></li>'
        )
    for s in skipped:
        rows.append(
            f'<li><span class="tag tag-skip">skipped</span>'
            f'<code>{_esc(s.get("field_id"))}</code>'
            f'<span class="rsn">{_esc(s.get("invariant"))} — unmet: '
            f'{_esc(", ".join(s.get("unmet_requirements", [])))}</span></li>'
        )

    n = len(rejected) + len(skipped)
    return (
        f'<details class="discards"><summary>{n} candidate(s) not tested</summary>'
        f'<ul>{"".join(rows)}</ul>'
        f'<p class="hint">Rejected: the model referenced the page incorrectly. '
        f'Skipped: a real candidate no invariant could be applied to.</p>'
        f"</details>"
    )


def _run(run: dict, index: int) -> str:
    summary = run.get("summary", {})
    tests = sorted(
        run.get("tests", []), key=lambda t: _ORDER.get(t.get("result"), 9)
    )
    counts = "".join(
        f'<span class="c c-{key}">{summary.get(key, 0)}</span>'
        for key in ("violations", "inconclusive", "passed")
    )
    worst = (
        "violation"
        if summary.get("violations")
        else "inconclusive"
        if summary.get("inconclusive")
        else "holds"
    )
    return f"""
    <section class="run r-{worst}" id="run-{index}">
      <header class="run-head">
        <div>
          <h3>{_esc(run.get("url"))}</h3>
          <p class="meta">{_when(run.get("started_at"))}
            · <code>{_esc(run.get("model"))}</code>
            · <span class="file">{_esc(run.get("_file"))}</span></p>
        </div>
        <div class="counts">{counts}</div>
      </header>
      {_discards(run)}
      <div class="tests">{"".join(_test(t) for t in tests)}</div>
    </section>"""


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

_CSS = """
:root{
  color-scheme:light;
  --ground:#f5f7f8; --panel:#fff; --sunk:#eef1f3;
  --ink:#0f1a1f; --ink-2:#44545c; --ink-3:#72828b;
  --rule:#dde4e8; --accent:#0e7490;
  --ok:#15803d; --ok-bg:#e6f4ea;
  --bad:#b4181c; --bad-bg:#fbe8e8;
  --meh:#96620a; --meh-bg:#faf1de;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  color-scheme:dark;
  --ground:#0c1316; --panel:#131d21; --sunk:#0e171a;
  --ink:#e9eff1; --ink-2:#a9b8bf; --ink-3:#76868e;
  --rule:#233238; --accent:#3cc0da;
  --ok:#4ade80; --ok-bg:#122a1b;
  --bad:#f87171; --bad-bg:#2d1416;
  --meh:#fbbf24; --meh-bg:#2a2009;
}}
:root[data-theme="dark"]{
  color-scheme:dark;
  --ground:#0c1316; --panel:#131d21; --sunk:#0e171a;
  --ink:#e9eff1; --ink-2:#a9b8bf; --ink-3:#76868e;
  --rule:#233238; --accent:#3cc0da;
  --ok:#4ade80; --ok-bg:#122a1b;
  --bad:#f87171; --bad-bg:#2d1416;
  --meh:#fbbf24; --meh-bg:#2a2009;
}
:root[data-theme="light"]{
  color-scheme:light;
  --ground:#f5f7f8; --panel:#fff; --sunk:#eef1f3;
  --ink:#0f1a1f; --ink-2:#44545c; --ink-3:#72828b;
  --rule:#dde4e8; --accent:#0e7490;
  --ok:#15803d; --ok-bg:#e6f4ea;
  --bad:#b4181c; --bad-bg:#fbe8e8;
  --meh:#96620a; --meh-bg:#faf1de;
}

body{background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:60rem;margin:0 auto;padding:2.5rem 1.25rem 5rem}

/* masthead + tiles */
.top{display:flex;flex-wrap:wrap;gap:1rem;justify-content:space-between;
  align-items:flex-end;border-bottom:2px solid var(--ink);padding-bottom:1.1rem}
.top h1{font-size:1.5rem;font-weight:650;letter-spacing:-.015em}
.top .sub{font-family:var(--mono);font-size:.74rem;color:var(--ink-3);
  letter-spacing:.06em;text-transform:uppercase;margin-top:.2rem}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(8rem,1fr));
  gap:.7rem;margin:1.6rem 0 2.4rem}
.tile{background:var(--panel);border:1px solid var(--rule);border-radius:7px;
  padding:.85rem 1rem;border-top:3px solid var(--rule)}
.tile.v{border-top-color:var(--bad)} .tile.i{border-top-color:var(--meh)}
.tile.p{border-top-color:var(--ok)}  .tile.n{border-top-color:var(--accent)}
.tile b{display:block;font-size:1.9rem;line-height:1.1;font-weight:650;
  font-variant-numeric:tabular-nums}
.tile.v b{color:var(--bad)} .tile.i b{color:var(--meh)} .tile.p b{color:var(--ok)}
.tile span{font-family:var(--mono);font-size:.68rem;letter-spacing:.09em;
  text-transform:uppercase;color:var(--ink-3)}

/* run */
.run{background:var(--panel);border:1px solid var(--rule);border-radius:9px;
  margin-bottom:1.5rem;overflow:hidden;border-left:4px solid var(--rule)}
.run.r-violation{border-left-color:var(--bad)}
.run.r-inconclusive{border-left-color:var(--meh)}
.run.r-holds{border-left-color:var(--ok)}
.run-head{display:flex;flex-wrap:wrap;gap:.8rem;justify-content:space-between;
  align-items:center;padding:1rem 1.2rem;border-bottom:1px solid var(--rule)}
.run-head h3{font-family:var(--mono);font-size:.92rem;font-weight:600;
  word-break:break-all}
.meta{font-family:var(--mono);font-size:.71rem;color:var(--ink-3);margin-top:.25rem}
.meta code{background:none;padding:0;color:var(--accent)}
.file{opacity:.65}
.counts{display:flex;gap:.35rem}
.c{font-family:var(--mono);font-size:.78rem;font-weight:700;min-width:1.9rem;
  text-align:center;padding:.18rem .45rem;border-radius:4px;
  font-variant-numeric:tabular-nums}
.c-violations{background:var(--bad-bg);color:var(--bad)}
.c-inconclusive{background:var(--meh-bg);color:var(--meh)}
.c-passed{background:var(--ok-bg);color:var(--ok)}

/* test */
.tests{display:flex;flex-direction:column}
.test{padding:1.05rem 1.2rem;border-bottom:1px solid var(--rule)}
.test:last-child{border-bottom:0}
.test header{display:flex;align-items:center;gap:.65rem;margin-bottom:.45rem}
.test h4{font-size:1rem;font-weight:620;flex:1}
.dur{font-family:var(--mono);font-size:.7rem;color:var(--ink-3);
  font-variant-numeric:tabular-nums}
.pill{font-family:var(--mono);font-size:.64rem;letter-spacing:.08em;
  text-transform:uppercase;padding:.16rem .5rem;border-radius:3px;font-weight:700}
.p-violation{background:var(--bad-bg);color:var(--bad);
  box-shadow:inset 0 0 0 1px var(--bad)}
.p-inconclusive{background:var(--meh-bg);color:var(--meh);
  box-shadow:inset 0 0 0 1px var(--meh)}
.p-holds{background:var(--ok-bg);color:var(--ok);
  box-shadow:inset 0 0 0 1px var(--ok)}
.detail{font-size:.86rem;color:var(--ink-2);margin-bottom:.75rem}

.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));
  gap:.55rem;background:var(--sunk);border-radius:6px;padding:.7rem .85rem}
.fact dt{font-family:var(--mono);font-size:.64rem;letter-spacing:.07em;
  text-transform:uppercase;color:var(--ink-3);margin-bottom:.12rem}
.fact dd{font-family:var(--mono);font-size:.78rem;word-break:break-all}
.t-violation .fact:nth-child(6) dd{color:var(--bad);font-weight:700}
.t-violation .fact:nth-child(5) dd{color:var(--ink)}

.why{font-size:.82rem;color:var(--ink-2);margin-top:.7rem;
  padding-left:.7rem;border-left:2px solid var(--rule)}
.why span{display:block;font-family:var(--mono);font-size:.63rem;
  letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);
  margin-bottom:.12rem}
.warn{margin-top:.7rem;font-size:.8rem;color:var(--meh);
  background:var(--meh-bg);padding:.45rem .65rem;border-radius:5px}

/* traces + discards */
details{margin-top:.75rem}
summary{cursor:pointer;font-family:var(--mono);font-size:.71rem;color:var(--accent);
  letter-spacing:.05em;list-style:none;display:inline-flex;gap:.35rem;
  align-items:center;padding:.15rem 0}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸";display:inline-block;transition:transform .12s}
details[open]>summary::before{transform:rotate(90deg)}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.steps{list-style:none;margin:.5rem 0 0;font-family:var(--mono);font-size:.74rem;
  border-left:1px solid var(--rule);padding-left:.8rem;
  display:flex;flex-direction:column;gap:.18rem}
.steps li{display:flex;flex-wrap:wrap;gap:.45rem;align-items:baseline}
.sm{color:var(--ok);font-size:.6rem}
.s-fail .sm{color:var(--bad)} .s-fail .sn{color:var(--bad);font-weight:700}
.s-td{opacity:.6} .s-td .sm{color:var(--ink-3)}
.sn{color:var(--ink-2)}
.sv{color:var(--accent);word-break:break-all}
.sd{color:var(--bad);flex-basis:100%;padding-left:1.05rem}

.discards{padding:.7rem 1.2rem;background:var(--sunk);
  border-bottom:1px solid var(--rule);margin:0}
.discards ul{list-style:none;margin:.55rem 0 0;display:flex;
  flex-direction:column;gap:.35rem;font-size:.78rem}
.discards li{display:flex;flex-wrap:wrap;gap:.45rem;align-items:baseline}
.tag{font-family:var(--mono);font-size:.6rem;letter-spacing:.07em;
  text-transform:uppercase;padding:.1rem .35rem;border-radius:3px}
.tag-rej{background:var(--bad-bg);color:var(--bad)}
.tag-skip{background:var(--sunk);color:var(--ink-3);
  box-shadow:inset 0 0 0 1px var(--rule)}
.rsn{color:var(--ink-2);font-size:.76rem}
.hint{font-size:.72rem;color:var(--ink-3);margin-top:.55rem}

code{font-family:var(--mono);font-size:.88em;background:var(--sunk);
  padding:.06em .3em;border-radius:3px}
.empty{background:var(--panel);border:1px dashed var(--rule);border-radius:9px;
  padding:3rem 1.5rem;text-align:center;color:var(--ink-3)}
.empty code{display:inline-block;margin-top:.7rem}
footer{margin-top:2.5rem;padding-top:1.1rem;border-top:1px solid var(--rule);
  font-family:var(--mono);font-size:.7rem;color:var(--ink-3);
  display:flex;flex-wrap:wrap;gap:.8rem;justify-content:space-between}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def render(runs: list[dict]) -> str:
    totals = {"violations": 0, "inconclusive": 0, "passed": 0}
    for run in runs:
        for key in totals:
            totals[key] += run.get("summary", {}).get(key, 0)
    n_tests = sum(totals.values())

    if runs:
        body = "".join(_run(run, i) for i, run in enumerate(runs))
    else:
        body = (
            '<div class="empty"><p>No runs recorded yet.</p>'
            "<code>make demo</code></div>"
        )

    latest = _when(runs[0].get("started_at")) if runs else "—"

    return f"""<title>BlindSpot — Runs</title>
<style>{_CSS}</style>
<div class="wrap">
  <div class="top">
    <div>
      <h1>BlindSpot</h1>
      <p class="sub">Run dashboard</p>
    </div>
    <p class="sub">Latest: {latest}</p>
  </div>

  <div class="tiles">
    <div class="tile v"><b>{totals['violations']}</b><span>Regressions</span></div>
    <div class="tile i"><b>{totals['inconclusive']}</b><span>Inconclusive</span></div>
    <div class="tile p"><b>{totals['passed']}</b><span>Passed</span></div>
    <div class="tile n"><b>{len(runs)}</b><span>Runs</span></div>
  </div>

  {body}

  <footer>
    <span>{n_tests} tests across {len(runs)} run(s)</span>
    <span>Generated {datetime.now().strftime('%d %b %Y · %H:%M')}</span>
  </footer>
</div>
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--out", type=Path, help="default: <runs-dir>/dashboard.html")
    parser.add_argument("--limit", type=int, default=25, help="most recent N runs")
    parser.add_argument("--open", action="store_true", help="open in a browser")
    args = parser.parse_args(argv)

    runs = load_runs(args.runs_dir)[: args.limit]
    out = args.out or args.runs_dir / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(runs))

    print(f"{len(runs)} run(s) -> {out}")
    if args.open:
        import webbrowser

        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
