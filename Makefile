.PHONY: dev test test-watch lint format typecheck check deadcode validate coverage-check coverage-ratchet pre-release

PYTHON := .venv/bin/python
PYTEST := $(PYTHON) -m pytest
RUFF := .venv/bin/ruff
MYPY := .venv/bin/mypy

dev:
	./start.sh

test:
	$(PYTEST) --cov=mammamiradio --cov-report=term-missing

test-watch:
	$(PYTHON) -m pytest_watch -- --cov=mammamiradio -x

lint:
	$(RUFF) check .

format:
	$(RUFF) format .

typecheck:
	$(MYPY) mammamiradio/ tests/

deadcode:
	.venv/bin/vulture mammamiradio/

check: lint typecheck deadcode coverage-check
	@echo "All checks passed"

validate:
	./scripts/validate-addon.sh

coverage-check:
	$(PYTHON) scripts/coverage-ratchet.py check

coverage-ratchet:
	$(PYTHON) scripts/coverage-ratchet.py update

pre-release:
	./scripts/pre-release-check.sh
