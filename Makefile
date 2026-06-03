.PHONY: help dev test test-fast test-watch lint format typecheck check deadcode validate coverage-check coverage-ratchet perf-smoke pre-release edge-release

PYTHON := .venv/bin/python
PYTEST := $(PYTHON) -m pytest
RUFF := .venv/bin/ruff
MYPY := .venv/bin/mypy

.DEFAULT_GOAL := help

help: ## Show this help (auto-generated from target annotations)
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk -F':.*?## ' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

dev: ## Run local dev server via start.sh (uvicorn + optional caddy proxy)
	./start.sh

test: ## Run full test suite with coverage
	$(PYTEST) --cov=mammamiradio --cov-report=term-missing

# Fast edit-loop runner: no coverage instrumentation, respects pyproject.toml
# addopts (excludes `requires_ffmpeg` tests). ~25-30s on this suite vs ~60s
# with coverage. Not for CI — coverage gate runs in the `test` target.
test-fast: ## Run tests without coverage (~25-30s edit-loop runner)
	$(PYTEST) -q

test-watch: ## Re-run tests on file save (pytest-watch)
	$(PYTHON) -m pytest_watch -- --cov=mammamiradio -x

lint: ## Lint with ruff
	$(RUFF) check .

format: ## Format with ruff
	$(RUFF) format .

typecheck: ## Type-check with mypy
	$(MYPY) mammamiradio/ tests/

deadcode: ## Find unused code with vulture
	.venv/bin/vulture mammamiradio/

check: lint typecheck deadcode coverage-check ## Run all checks (lint + typecheck + deadcode + coverage gate)
	@echo "All checks passed"

validate: ## Validate HA addon config (pre-merge gate)
	./scripts/validate-addon.sh

coverage-check: ## Check coverage stayed above per-module floors
	$(PYTHON) scripts/coverage-ratchet.py check

coverage-ratchet: ## Preview what coverage floors CI would commit on main
	$(PYTHON) scripts/coverage-ratchet.py update

perf-smoke: ## Run HA Green perf smoke against a live station
	$(PYTHON) scripts/ha-green-perf-smoke.py

pre-release: ## Run pre-release checks (version sync + invariants + CHANGELOG head)
	./scripts/pre-release-check.sh

edge-release: ## Cut a manual edge release (edge version = main short-SHA, opens a PR)
	./scripts/cut-edge-release.sh
