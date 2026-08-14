.PHONY: help dev test test-fast test-watch lint format format-check typecheck check deadcode validate coverage-check coverage-ratchet perf-smoke launch-smoke ha-green-release-proof player-smoke media-check media-proof pre-release edge-release

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

format-check: ## Check formatting with ruff (no changes; mirrors CI)
	$(RUFF) format --check .

typecheck: ## Type-check with mypy
	$(MYPY) mammamiradio/ tests/

deadcode: ## Find unused code with vulture
	.venv/bin/vulture mammamiradio/

check: media-check lint format-check typecheck deadcode coverage-check ## Run all checks, including the strict media-rights gate
	@echo "All checks passed"

validate: ## Validate HA addon config (pre-merge gate)
	./scripts/validate-addon.sh

coverage-check: ## Check coverage stayed above per-module floors
	$(PYTHON) scripts/coverage-ratchet.py check

coverage-ratchet: ## Preview what coverage floors CI would commit on main
	$(PYTHON) scripts/coverage-ratchet.py update

perf-smoke: ## Run HA Green perf smoke against a live station
	$(PYTHON) scripts/ha-green-perf-smoke.py

launch-smoke: ## Cold-launch a station on temp dirs and assert first byte <= 2s
	$(PYTHON) scripts/ha-green-launch-smoke.py

ha-green-release-proof: ## Validate 20 physical HA Green cold-launch receipts and p95 <= 2s
	$(PYTHON) scripts/validate-ha-green-release-evidence.py

player-smoke: ## Run deterministic listener interactions against PLAYER_SMOKE_URL
	PLAYER_SMOKE_URL="$(or $(PLAYER_SMOKE_URL),http://127.0.0.1:8000)" PLAYWRIGHT_CLI="$(PLAYWRIGHT_CLI)" ./scripts/player-smoke.sh

media-check: ## Fast strict starter manifest, evidence, bytes, and audio gate
	$(PYTHON) scripts/media-proof.py --quick

media-proof: ## Full package, image, extractor, and transient-media proof
	$(PYTHON) scripts/media-proof.py --output "$(or $(MEDIA_PROOF_OUTPUT),tmp/media-proof/media-proof.json)"

pre-release: ## Run pre-release checks (version sync + invariants + CHANGELOG head + merge-gate settings)
	./scripts/pre-release-check.sh
	./scripts/check-merge-gate.sh

edge-release: ## Cut a manual edge release (edge version = newest built main short-SHA, opens a PR). ARGS="--target-sha <sha>" pins one exact commit.
	./scripts/cut-edge-release.sh $(ARGS)
