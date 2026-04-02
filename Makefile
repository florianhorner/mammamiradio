.PHONY: dev test test-watch lint format typecheck check validate

dev:
	./start.sh

test:
	pytest --cov=mammamiradio --cov-report=term-missing

test-watch:
	ptw -- --cov=mammamiradio -x

lint:
	ruff check .

format:
	ruff format .

typecheck:
	mypy mammamiradio/ tests/

check: lint typecheck test
	@echo "All checks passed"

validate:
	./scripts/validate-addon.sh
