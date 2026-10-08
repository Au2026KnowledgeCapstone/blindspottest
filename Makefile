PY   := .venv/bin/python
PIP  := .venv/bin/pip
PORT ?= 3000
BASE := http://127.0.0.1:$(PORT)

.DEFAULT_GOAL := help
.PHONY: help install demo broken profile project edit all scan run pages watch dash rules inspect truth clean \
        crawl crawl-broken crawl-stable map mermaid draw graph graph-diff \
        baseline baseline-cached regression regression-clean flow-rules values

GRAPHS := runs/graphs

# Is something already listening on $(PORT)?
UP := $(PY) -c "import socket,sys; sys.exit(0 if socket.socket().connect_ex(('127.0.0.1',$(PORT)))==0 else 1)"

# Run a scan with the demo app available. If the app is already running
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

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk -F':.*?## ' '{printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Start here:  make demo        (scan one page for a regression)"
	@echo "               make graph       (map the whole app and draw it)"
	@echo "  Each target starts and stops the demo app for you."

install:  ## Create the venv and install dependencies
	python3 -m venv .venv
	$(PIP) -q install -r requirements.txt
	$(PY) -m playwright install chromium

demo: broken  ## Start here — scan the page with the planted regression

broken:  ## Scan the broken profile page — expect a violation
	$(call with_app, $(PY) main.py $(BASE)/broken/profile $(ARGS))

profile:  ## Scan /profile — expect all pass
	$(call with_app, $(PY) main.py $(BASE)/profile $(ARGS))

project:  ## Scan /project-settings — expect all pass
	$(call with_app, $(PY) main.py $(BASE)/project-settings $(ARGS))

edit:  ## Scan a project edit form — expect inconclusive: the commit navigates away
	$(call with_app, $(PY) main.py $(BASE)/projects/1/edit $(ARGS))

all:  ## Scan the three single-page flows, then build the dashboard
	$(call with_app, \
	  $(PY) main.py $(BASE)/profile          $(ARGS) || true; \
	  $(PY) main.py $(BASE)/broken/profile   $(ARGS) || true; \
	  $(PY) main.py $(BASE)/project-settings $(ARGS) || true)
	@$(PY) -m reporting.dashboard

scan:  ## Scan any URL: make scan URL=http://localhost:8080/settings
	@test -n "$(URL)" || { echo "usage: make scan URL=<url>"; exit 2; }
	$(PY) main.py $(URL) $(ARGS)

pages:  ## Open the demo app in your browser and keep it running
	$(PY) -m demo_app.app $(PORT) --open

watch:  ## Scan the broken page with the browser visible
	$(call with_app, $(PY) main.py $(BASE)/broken/profile --headed $(ARGS))

run:  ## Serve the demo app in the foreground (no browser)
	$(PY) -m demo_app.app $(PORT)

dash:  ## Build runs/dashboard.html from recorded runs and open it
	$(PY) -m reporting.dashboard --open

rules:  ## Print the loaded rule base (no browser, no network)
	$(PY) -m knowledge.engine

inspect:  ## Print a page snapshot: make inspect URL=... (no LLM)
	$(call with_app, $(PY) -m discovery.page_inspector $(or $(URL),$(BASE)/profile))

truth:  ## Print what each demo flow is and where its defect is (no browser)
	$(PY) -m demo_app.manifest

# --------------------------------------------------------------------------
# Discovery — map the whole application instead of scanning one page
#
# The two builds are crawled separately and their graphs compared. Crawling
# them together would merge two applications into one graph and destroy the
# comparison they exist to support.
# --------------------------------------------------------------------------

crawl:  ## Map the sound build -> runs/graphs/graph_sound.json
	$(call with_app, $(PY) -u -m discovery.crawler $(BASE) \
	  --exclude /broken --out $(GRAPHS)/graph_sound.json $(ARGS))

crawl-broken:  ## Map the broken build -> runs/graphs/graph_broken.json
	$(call with_app, $(PY) -u -m discovery.crawler $(BASE) --entry /broken \
	  --stay-under /broken --out $(GRAPHS)/graph_broken.json $(ARGS))

crawl-stable:  ## Crawl twice and diff — proves state identity does not drift
	$(call with_app, $(PY) -u -m discovery.crawler $(BASE) \
	  --exclude /broken --repeat 2 --out $(GRAPHS)/graph_sound.json $(ARGS))

map:  ## Print the app map an LLM would be shown (needs make crawl first)
	@$(PY) -m discovery.projections $(GRAPHS)/graph_sound.json --view summary

mermaid:  ## Print the graph as mermaid source
	@$(PY) -m discovery.projections $(GRAPHS)/graph_sound.json --view mermaid

draw:  ## Draw the graph you already crawled and open it (no re-crawl)
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

BASELINE := runs/baseline-sound

baseline:  ## Record a baseline of the sound build (crawl + flows, uses the LLM)
	$(call with_app, $(PY) -u main.py --baseline --exclude /broken \
	  --baseline-dir $(BASELINE) \
	  --save-flow-candidates runs/flow-candidates.json $(BASE) $(ARGS))

baseline-cached:  ## Re-record the baseline reusing saved flow candidates (no LLM)
	@test -f runs/flow-candidates.json \
	  || { echo "no cached candidates — run 'make baseline' first"; exit 2; }
	$(call with_app, $(PY) -u main.py --baseline --exclude /broken \
	  --baseline-dir $(BASELINE) \
	  --flow-candidates runs/flow-candidates.json $(BASE) $(ARGS))

regression:  ## Replay the baseline against the broken build — expect regressions
	@test -d $(BASELINE) \
	  || { echo "no baseline yet — run 'make baseline' first"; exit 2; }
	$(call with_app, $(PY) -u main.py --regression \
	  --baseline-dir $(BASELINE) $(BASE)/broken $(ARGS))

regression-clean:  ## Replay the baseline against the sound build — expect nothing
	@test -d $(BASELINE) \
	  || { echo "no baseline yet — run 'make baseline' first"; exit 2; }
	$(call with_app, $(PY) -u main.py --regression \
	  --baseline-dir $(BASELINE) --exclude /broken $(BASE) $(ARGS))

flow-rules:  ## Show the flow invariants the rule base holds
	@$(PY) -m knowledge.flows

values:  ## Print the readable value surface of one page (URL=...)
	@test -n "$(URL)" || { echo "usage: make values URL=/catalog"; exit 2; }
	$(call with_app, $(PY) -m discovery.readable "$(BASE)$(URL)" --text)

clean:  ## Remove caches and run records
	rm -rf runs __pycache__ */__pycache__
