PY ?= python3
SRC := src
COVERAGE_FLOOR := 95

.PHONY: help test coverage dogfood baseline compile build clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

test: ## Run the test suite (stdlib unittest, no third-party deps)
	PYTHONPATH=$(SRC) $(PY) -m unittest discover -s tests

coverage: ## Run tests under coverage.py and enforce the floor (needs: pip install coverage)
	PYTHONPATH=$(SRC) $(PY) -m coverage run --source=$(SRC) -m unittest discover -s tests
	$(PY) -m coverage report -m
	$(PY) -m coverage report --fail-under=$(COVERAGE_FLOOR) >/dev/null

dogfood: ## Run slopguard-python on its own source (complexity-only, fast)
	PYTHONPATH=$(SRC) $(PY) -m slopguard analyze --path $(SRC)/slopguard --no-coverage --fail-over 300

baseline: ## Assert the sample-app regression baseline (needs: pip install coverage)
	PYTHONPATH=$(SRC) $(PY) -m slopguard analyze \
	  --path sample-apps/todolist/todolist --project-dir sample-apps/todolist \
	  --json --quiet \
	  | $(PY) -c 'import sys,json; r=json.load(sys.stdin)["summary"]; \
	    assert r["methodCount"]==12, r; assert r["crappyMethodCount"]==0, r; \
	    print("baseline ok:", r["methodCount"], "methods,", r["crappyMethodCount"], "crappy")'

compile: ## Byte-compile every module (a fast syntax gate)
	$(PY) -m compileall -q $(SRC) tests

build: ## Build a wheel + sdist (needs: pip install build)
	$(PY) -m build

clean: ## Remove caches and build artifacts
	rm -rf build dist *.egg-info src/*.egg-info .coverage coverage.json
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
