# Match engine tasks. Run from match-engine/.
#   make test                 unit + property tests (fast)
#   make eval-fast            EVAL layers 1-2 + tests/eval (pre-commit; under 2 minutes warm, DECISIONS D-118;
#                             the unit suite is `make test`, which CI runs separately)
#   make gate                 milestone gate: the slow tests and 50,000-example totality (about an hour)
#   make eval                 EVAL layers 1-3, report under reports/<timestamp>/
#   make eval BASELINE=main   ... diffed against a saved baseline
#   make eval-holdout         layer 4 (CI on release branches only; needs the CI key)
#   make tune BUDGET=200      propose weights under proposals/ (never edits config/)
#   make lint

ifeq ($(OS),Windows_NT)
PY ?= .venv/Scripts/python.exe
else
PY ?= .venv/bin/python
endif

BUDGET ?= 200
OBJECTIVE ?= ndcg@10
SPLIT ?= dev

.PHONY: test lint eval eval-fast eval-holdout tune gate

test:
	$(PY) -m pytest -q -m "not slow"

lint:
	$(PY) -m ruff check src tests scripts eval

eval-fast:
	$(PY) -m pytest -q tests/eval
	$(PY) -m eval.run --layers 1,2 $(if $(BASELINE),--baseline $(BASELINE),)

eval:
	$(PY) -m pytest -q
	$(PY) -m eval.run --layers 1,2,3 $(if $(BASELINE),--baseline $(BASELINE),)

gate:
	$(PY) -m pytest -q -m slow
	TOTALITY_EXAMPLES=50000 $(PY) -m pytest -q tests/test_totality.py

eval-holdout:
	$(PY) -m eval.run --layers 4

tune:
	$(PY) -m eval.tune.search --budget $(BUDGET) --objective $(OBJECTIVE) --split $(SPLIT)
