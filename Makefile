PY   := .venv/bin/python
PIP  := .venv/bin/pip
PORT ?= 3000
BASE := http://127.0.0.1:$(PORT)

.DEFAULT_GOAL := help
.PHONY: help install demo scan edit run dash rules inspect truth values clean \
        crawl crawl-broken crawl-stable view draw graph graph-diff \
        baseline baseline-cached regression regression-clean

GRAPHS   := runs/graphs
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
		/^[a-z][a-z-]*:.*?## / { printf "  \033[36m%-17s\033[0m %s\n", $$1, $$2 }' \
		$(MAKEFILE_LIST)
	@echo
	@echo "$(BOLD)Variables$(OFF)   (flags must arrive as VAR=value — make eats a bare --flag)"
	@echo "  HEADED=1             show the browser instead of running headless"
	@echo "  SLOW=400             pause 400ms before each action (300-500 reads well)"
	@echo "  V=1                  print the crawler's trace of every click"
	@echo "  URL=/broken/profile  a path relative to the demo app, or a full URL"
	@echo "  VIEW=mermaid         for 'make view': summary | mermaid | json"
	@echo "  OPEN=1               for 'make run': open a browser as well"
	@echo "  PORT=3000            port the demo app is served on"
	@echo "  ARGS='--max-states 8'  anything else, passed straight through"
	@echo
	@echo "$(BOLD)Watching the crawler$(OFF)"
	@echo "  It runs in crawl / crawl-broken / crawl-stable / graph / baseline /"
	@echo "  regression — NOT in demo or scan, which load a single page."
	@echo "    make crawl HEADED=1 SLOW=400        watch every click"
	@echo "    make crawl                          trace is on by default"
	@echo "    make baseline HEADED=1 SLOW=300 V=1 trace and watch"
	@echo
	@echo "$(BOLD)Other ARGS worth knowing$(OFF)"
	@echo "  --max-states 10      cap the crawl; handy for a quick look"
	@echo "  --safe               skip controls whose text looks destructive"
	@echo "  --no-interpret       skip the LLM reading of a regression diff"
	@echo "  --flow-candidates F  reuse cached capabilities instead of the LLM"
	@echo
	@echo "$(BOLD)Start here$(OFF)"
	@echo "  make demo        scan one page — the original persistence check"
	@echo "  make baseline    map the app and record a flow baseline"
	@echo "  make regression  replay it against the broken build"
	@echo
	@echo "  Every target starts and stops the demo app for you."

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
	  --exclude /broken --out $(GRAPHS)/graph_sound.json $(ARGS) $(CRAWL_WATCH))

crawl-broken:  ## Map the broken build -> runs/graphs/graph_broken.json
	$(call with_app, $(PY) -u -m discovery.crawler $(BASE) --entry /broken \
	  --stay-under /broken --out $(GRAPHS)/graph_broken.json $(ARGS) $(CRAWL_WATCH))

crawl-stable:  ## Crawl twice and diff — proves state identity does not drift
	$(call with_app, $(PY) -u -m discovery.crawler $(BASE) \
	  --exclude /broken --repeat 2 --out $(GRAPHS)/graph_sound.json $(ARGS) $(CRAWL_WATCH))

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
	  --baseline-dir $(BASELINE) \
	  --save-flow-candidates runs/flow-candidates.json $(BASE) $(ARGS) $(WATCH))

baseline-cached:  ## Re-record the baseline reusing saved flow candidates (no LLM)
	@test -f runs/flow-candidates.json \
	  || { echo "no cached candidates — run 'make baseline' first"; exit 2; }
	$(call with_app, $(PY) -u main.py --baseline --exclude /broken \
	  --baseline-dir $(BASELINE) \
	  --flow-candidates runs/flow-candidates.json $(BASE) $(ARGS) $(WATCH))

regression:  ## Replay the baseline against the broken build — expect regressions
	@test -d $(BASELINE) \
	  || { echo "no baseline yet — run 'make baseline' first"; exit 2; }
	$(call with_app, $(PY) -u main.py --regression \
	  --baseline-dir $(BASELINE) $(BASE)/broken $(ARGS) $(WATCH))

regression-clean:  ## Replay the baseline against the sound build — expect nothing
	@test -d $(BASELINE) \
	  || { echo "no baseline yet — run 'make baseline' first"; exit 2; }
	$(call with_app, $(PY) -u main.py --regression \
	  --baseline-dir $(BASELINE) --exclude /broken $(BASE) $(ARGS) $(WATCH))

##@ Housekeeping
clean:  ## Remove caches and run records
	rm -rf runs __pycache__ */__pycache__
