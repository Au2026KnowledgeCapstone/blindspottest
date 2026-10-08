PY   := .venv/bin/python
PIP  := .venv/bin/pip
PORT ?= 3000
BASE := http://127.0.0.1:$(PORT)

.DEFAULT_GOAL := help
.PHONY: help install demo scan edit run dash rules inspect truth values clean \
        crawl crawl-broken crawl-stable view draw graph graph-diff \
        baseline baseline-cached regression regression-clean \
        remote-map remote-draw remote-view remote-baseline remote-regression

GRAPHS   := runs/graphs
# The demo app's own login. These used to be hardcoded inside the crawler,
# which meant pointing it at any real login form typed them into it.
DEMO_CREDS := --credential username=demo --credential password=demo123
BASELINE := runs/baseline-sound

# Real escape bytes, resolved once. `echo "\033[1m"` only renders under a
# shell whose echo interprets escapes — dash does, bash does not — and make
# picks /bin/sh, so writing them inline makes the help legible on one machine
# and full of `\033[1m` on the next.
BOLD := $(shell printf '\033[1m')
OFF  := $(shell printf '\033[0m')

# Shorthands for the flags people actually reach for, so the common case
# needs no quoting: `make crawl HEADED=1 SLOW=400` rather than
# `make crawl ARGS='--headed --slow-mo 400'`.
#
# A bare `make crawl --headed` cannot work and never will: make parses
# leading `--` arguments as its own options before it ever looks at the
# target, so the flag has to arrive as a variable either way. These just make
# the variable spelling pleasant.
#
# Two forms because the two CLIs differ: `discovery.crawler` prints its click
# trace by default and has no -v, while main.py needs -v to switch it on.
WATCH       := $(if $(HEADED),--headed,) $(if $(SLOW),--slow-mo $(SLOW),) $(if $(V),-v,)
CRAWL_WATCH := $(if $(HEADED),--headed,) $(if $(SLOW),--slow-mo $(SLOW),)

# Is something already listening on $(PORT)?
UP := $(PY) -c "import socket,sys; sys.exit(0 if socket.socket().connect_ex(('127.0.0.1',$(PORT)))==0 else 1)"

# Shell prologue that normalizes `$$u`: a bare path is taken as relative to
# the demo app, a full URL is used as given. This is what lets one `scan`
# target replace a per-page target for every route — the Makefile was
# accumulating one bookmark per URL, which is what made it unreadable.
ABS = case "$$u" in http*) ;; *) u="$(BASE)$$u";; esac

# Run a command with the demo app available. If the app is already running
# (someone left `make run` going) it is reused and left alone; otherwise it is
# started, waited for, and stopped again on the way out — including on Ctrl-C
# or failure, so a stray server is never left behind.
define with_app
	@set -e; \
	if $(UP) 2>/dev/null; then \
	  echo "  using the demo app already on :$(PORT)"; \
	else \
	  $(PY) -m demo_app.app $(PORT) >/dev/null 2>&1 & \
	  pid=$$!; \
	  trap "kill $$pid 2>/dev/null || true" EXIT INT TERM; \
	  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do \
	    $(UP) 2>/dev/null && break; sleep 0.2; \
	  done; \
	  $(UP) 2>/dev/null || { echo "demo app failed to start on :$(PORT)"; exit 1; }; \
	fi; \
	$(1)
endef

##@ Getting started
help:  ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} \
		/^##@ / { printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next } \
		/^[a-z][a-z-]*:.*?## / { printf "  \033[36m%-19s\033[0m %s\n", $$1, $$2 }' \
		$(MAKEFILE_LIST)
	@echo
	@echo "$(BOLD)Start here$(OFF)"
	@echo "  make demo        scan one page — the original persistence check"
	@echo "  make baseline    map the demo app and record a flow baseline"
	@echo "  make regression  replay it against the broken build"
	@echo
	@echo "$(BOLD)Variables$(OFF)   (flags must arrive as VAR=value — make eats a bare --flag)"
	@echo "  HEADED=1               show the browser instead of running headless"
	@echo "  SLOW=400               pause 400ms before each action (300-500 reads well)"
	@echo "  V=1                    print the crawler's trace of every click"
	@echo "  URL=/broken/profile    a path relative to the demo app, or a full URL"
	@echo "  VIEW=mermaid           for view / remote-view: summary | mermaid | json"
	@echo "  OPEN=1                 for 'make run': open a browser as well"
	@echo "  PORT=3000              port the demo app is served on"
	@echo "  TARGET=https://you.com the real site, for the remote-* targets"
	@echo "  CREDS='--credential email=me@you.com --credential password=hunter2'"
	@echo "  SETTLE=500             throttle a remote crawl, ms (default 250)"
	@echo "  ALLOW=1                acknowledge that a remote run will WRITE"
	@echo "  ARGS='--max-states 8'  anything else, passed straight through"
	@echo
	@echo "$(BOLD)Watching the crawler$(OFF)"
	@echo "  It runs in crawl / crawl-broken / crawl-stable / graph / baseline /"
	@echo "  regression / remote-* — NOT in demo or scan, which load one page."
	@echo "    make crawl HEADED=1 SLOW=400        watch every click"
	@echo "    make crawl                          trace is on by default"
	@echo "    make baseline HEADED=1 SLOW=300 V=1 trace and watch"
	@echo
	@echo "$(BOLD)Other ARGS worth knowing$(OFF)"
	@echo "  --read-only          follow links only; never fire a button or submit"
	@echo "  --credential L=V     type V into fields labelled L (repeatable)"
	@echo "  --settle 250         wait after each transition; throttles the crawl"
	@echo "  --allow-writes       permit a writing crawl against a non-local host"
	@echo "  --max-states 10      cap the crawl; handy for a quick look"
	@echo "  --safe               skip controls whose text looks destructive"
	@echo "  --no-interpret       skip the LLM reading of a regression diff"
	@echo "  --flow-candidates F  reuse cached capabilities instead of the LLM"
	@echo
	@echo "$(BOLD)A real site you own$(OFF)"
	@echo "  Start read-only. It issues GETs and nothing else, so it cannot write"
	@echo "  whatever the buttons are called:"
	@echo "    make remote-map TARGET=https://you.com"
	@echo "    make remote-draw                     open the map it built"
	@echo "  Read-only cannot sign in (a login is a submit), runs no flow tests,"
	@echo "  and leaves the graph OPEN — every unfired button is listed as"
	@echo "  unexplored, so a partial map is never mistaken for a complete one."
	@echo
	@echo "  A writing run fires EVERY button: real deletions, real submissions,"
	@echo "  real outbound email. It also types placeholder text into the forms"
	@echo "  it submits. Only against something you can restore:"
	@echo "    make remote-baseline TARGET=https://you.com ALLOW=1 CREDS='...'"
	@echo "    make remote-regression TARGET=https://you.com ALLOW=1"
	@echo
	@echo "  The demo targets start and stop the demo app for you."
	@echo "  The remote-* targets never do — they only touch TARGET."

install:  ## Create the venv and install dependencies
	python3 -m venv .venv
	$(PIP) -q install -r requirements.txt
	$(PY) -m playwright install chromium

##@ Scan one page (the original MVP pipeline)
demo:  ## Start here — scan the page with the planted regression
	$(call with_app, $(PY) main.py $(BASE)/broken/profile $(ARGS) $(WATCH))

scan:  ## Scan one page: make scan URL=/broken/profile [ARGS=--headed]
	@test -n "$(URL)" || { echo "usage: make scan URL=/broken/profile"; exit 2; }
	$(call with_app, u="$(URL)"; $(ABS); $(PY) main.py "$$u" $(ARGS) $(WATCH))

edit:  ## Scan a project edit form — expect inconclusive: the commit navigates away
	$(call with_app, $(PY) main.py $(BASE)/projects/1/edit $(ARGS) $(WATCH))

##@ Serve, report, inspect
run:  ## Serve the demo app in the foreground; OPEN=1 also opens a browser
	$(PY) -m demo_app.app $(PORT) $(if $(OPEN),--open,)

dash:  ## Build runs/dashboard.html from recorded runs and open it
	$(PY) -m reporting.dashboard --open

rules:  ## Print both rule bases — page-level and flow-level (no browser)
	@$(PY) -m knowledge.engine
	@$(PY) -m knowledge.flows

inspect:  ## Print a page's control snapshot: make inspect URL=/catalog (no LLM)
	$(call with_app, u="$(or $(URL),/profile)"; $(ABS); \
	  $(PY) -m discovery.page_inspector "$$u")

values:  ## Print a page's readable value surface: make values URL=/catalog
	$(call with_app, u="$(or $(URL),/catalog)"; $(ABS); \
	  $(PY) -m discovery.readable "$$u" --text)

truth:  ## Print what each demo flow is and where its defect is (no browser)
	$(PY) -m demo_app.manifest

# --------------------------------------------------------------------------
# Discovery — map the whole application instead of scanning one page
#
# The two builds are crawled separately and their graphs compared. Crawling
# them together would merge two applications into one graph and destroy the
# comparison they exist to support.
# --------------------------------------------------------------------------

##@ Discovery — map the whole application
crawl:  ## Map the sound build -> runs/graphs/graph_sound.json
	$(call with_app, $(PY) -u -m discovery.crawler $(BASE) \
	  --exclude /broken $(DEMO_CREDS) \
	  --out $(GRAPHS)/graph_sound.json $(ARGS) $(CRAWL_WATCH))

crawl-broken:  ## Map the broken build -> runs/graphs/graph_broken.json
	$(call with_app, $(PY) -u -m discovery.crawler $(BASE) --entry /broken \
	  --stay-under /broken $(DEMO_CREDS) \
	  --out $(GRAPHS)/graph_broken.json $(ARGS) $(CRAWL_WATCH))

crawl-stable:  ## Crawl twice and diff — proves state identity does not drift
	$(call with_app, $(PY) -u -m discovery.crawler $(BASE) \
	  --exclude /broken --repeat 2 $(DEMO_CREDS) \
	  --out $(GRAPHS)/graph_sound.json $(ARGS) $(CRAWL_WATCH))

view:  ## Print a graph view: make view VIEW=summary|mermaid|json
	@test -f $(GRAPHS)/graph_sound.json \
	  || { echo "no graph yet — run 'make crawl' first"; exit 2; }
	@$(PY) -m discovery.projections $(GRAPHS)/graph_sound.json \
	  --view $(or $(VIEW),summary)

draw:  ## Render the crawled graph as HTML and open it (no re-crawl)
	@test -f $(GRAPHS)/graph_sound.json \
	  || { echo "no graph yet — run 'make crawl' first"; exit 2; }
	@$(PY) -m discovery.projections $(GRAPHS)/graph_sound.json \
	  --view html --out $(GRAPHS)/graph.html
	@$(PY) -c "import webbrowser,pathlib; \
	  p=pathlib.Path('$(GRAPHS)/graph.html').resolve(); \
	  print('opening', p); webbrowser.open(p.as_uri())"

graph: crawl draw  ## Map the app, draw it, and open the diagram

graph-diff:  ## Compare the sound and broken graphs (needs both crawls)
	@$(PY) -m discovery.compare $(GRAPHS)/graph_sound.json $(GRAPHS)/graph_broken.json

# --------------------------------------------------------------------------
# Baseline and regression — test the whole application, then test the diff
#
# The two builds are the two "versions": the sound build is the baseline, and
# the broken build stands in for a release that changed behaviour. Recording
# one and replaying it against the other is the same operation you would run
# against yesterday's deploy and today's.
# --------------------------------------------------------------------------

##@ Baseline and regression — record a build, replay it against the next
baseline:  ## Record a baseline of the sound build (crawl + flows, uses the LLM)
	$(call with_app, $(PY) -u main.py --baseline --exclude /broken \
	  --baseline-dir $(BASELINE) $(DEMO_CREDS) \
	  --save-flow-candidates runs/flow-candidates.json $(BASE) $(ARGS) $(WATCH))

baseline-cached:  ## Re-record the baseline reusing saved flow candidates (no LLM)
	@test -f runs/flow-candidates.json \
	  || { echo "no cached candidates — run 'make baseline' first"; exit 2; }
	$(call with_app, $(PY) -u main.py --baseline --exclude /broken \
	  --baseline-dir $(BASELINE) $(DEMO_CREDS) \
	  --flow-candidates runs/flow-candidates.json $(BASE) $(ARGS) $(WATCH))

regression:  ## Replay the baseline against the broken build — expect regressions
	@test -d $(BASELINE) \
	  || { echo "no baseline yet — run 'make baseline' first"; exit 2; }
	$(call with_app, $(PY) -u main.py --regression \
	  --baseline-dir $(BASELINE) $(DEMO_CREDS) $(BASE)/broken $(ARGS) $(WATCH))

regression-clean:  ## Replay the baseline against the sound build — expect nothing
	@test -d $(BASELINE) \
	  || { echo "no baseline yet — run 'make baseline' first"; exit 2; }
	$(call with_app, $(PY) -u main.py --regression \
	  --baseline-dir $(BASELINE) $(DEMO_CREDS) --exclude /broken $(BASE) $(ARGS) $(WATCH))

# --------------------------------------------------------------------------
# A real site you own
#
# These deliberately do NOT go through `with_app`: that helper boots a local
# demo app whenever :$(PORT) is quiet, which is exactly wrong when the target
# is somewhere else.
#
# `remote-map` is read-only — it issues GETs and nothing else, so it cannot
# write to the target whatever the buttons are called. `remote-baseline` can
# write, and main.py refuses to run it against a non-local host until
# --allow-writes is passed, because a crawl fires every button it finds.
#
# CREDS= passes credentials, e.g.
#   make remote-map TARGET=https://you.com CREDS='--credential email=me@you.com'
# --------------------------------------------------------------------------

REMOTE_BASELINE := runs/baseline-remote

##@ A real site you own
remote-map:  ## Read-only crawl of a site you own: make remote-map TARGET=https://you.com
	@test -n "$(TARGET)" || { echo "usage: make remote-map TARGET=https://example.com"; exit 2; }
	$(PY) -u main.py --baseline --read-only --settle $(or $(SETTLE),250) \
	  --baseline-dir $(REMOTE_BASELINE) $(CREDS) $(TARGET) $(ARGS) $(WATCH)

remote-draw:  ## Draw the graph remote-map produced and open it
	@test -f $(REMOTE_BASELINE)/graph.json \
	  || { echo "no remote graph yet — run 'make remote-map TARGET=...' first"; exit 2; }
	@$(PY) -m discovery.projections $(REMOTE_BASELINE)/graph.json \
	  --view html --out $(REMOTE_BASELINE)/graph.html
	@$(PY) -c "import webbrowser,pathlib; \
	  p=pathlib.Path('$(REMOTE_BASELINE)/graph.html').resolve(); \
	  print('opening', p); webbrowser.open(p.as_uri())"

remote-view:  ## Print the remote map: make remote-view [VIEW=summary|mermaid|json]
	@test -f $(REMOTE_BASELINE)/graph.json \
	  || { echo "no remote graph yet — run 'make remote-map TARGET=...' first"; exit 2; }
	@$(PY) -m discovery.projections $(REMOTE_BASELINE)/graph.json \
	  --view $(or $(VIEW),summary)

remote-baseline:  ## WRITES to the target. Full crawl + flow tests: make remote-baseline TARGET=... ALLOW=1
	@test -n "$(TARGET)" || { echo "usage: make remote-baseline TARGET=https://example.com ALLOW=1"; exit 2; }
	@test -n "$(ALLOW)" || { \
	  echo "This fires every button on $(TARGET) — deletions, submissions, outbound email."; \
	  echo "Re-run with ALLOW=1 once you are sure the target is restorable."; exit 2; }
	$(PY) -u main.py --baseline --allow-writes --safe --settle $(or $(SETTLE),250) \
	  --baseline-dir $(REMOTE_BASELINE) $(CREDS) $(TARGET) $(ARGS) $(WATCH)

remote-regression:  ## Replay the remote baseline: make remote-regression TARGET=... ALLOW=1
	@test -n "$(TARGET)" || { echo "usage: make remote-regression TARGET=https://example.com ALLOW=1"; exit 2; }
	@test -d $(REMOTE_BASELINE) \
	  || { echo "no remote baseline yet — run 'make remote-baseline' first"; exit 2; }
	@test -n "$(ALLOW)" || { \
	  echo "Replaying writes to $(TARGET). Re-run with ALLOW=1 when ready."; exit 2; }
	$(PY) -u main.py --regression --allow-writes --settle $(or $(SETTLE),250) \
	  --baseline-dir $(REMOTE_BASELINE) $(CREDS) $(TARGET) $(ARGS) $(WATCH)

##@ Housekeeping
clean:  ## Remove caches and run records
	rm -rf runs __pycache__ */__pycache__
