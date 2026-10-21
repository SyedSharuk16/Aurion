.PHONY: install test lint types cov causality power run preflight all clean

install:
	python3 -m pip install -e ".[dev]"

test:
	python3 -m pytest tests -q

# The four causality tests (docs/01-architecture.md §3.3). These gate everything:
# a failure here means a result cannot be trusted, not that a test is flaky.
causality:
	python3 -m pytest tests/causality -v

lint:
	python3 -m ruff check aurion tests tools

types:
	python3 -m mypy aurion tools

cov:
	python3 -m pytest tests -q --cov=aurion --cov-report=term-missing

# Power and capacity tables (docs/07). Re-run with measured pilot values to resolve
# Q10 (data purchase) and Q13 (trial budget).
power:
	python3 tools/power.py

# Start the live process. Reads credentials from the environment only; `make preflight`
# reports what is missing without starting anything. Neither buys data, and neither makes
# the system emit a signal before it has history (see README, "Actually running it").
preflight:
	python3 -c "import os; from aurion.config.models import load_config; \
	from aurion.runtime.app import preflight; \
	fs = preflight(os.environ, load_config('config.yaml').meta.mode); \
	print('\n'.join(f.render() for f in fs) or 'preflight clean'); \
	raise SystemExit(1 if any(f.fatal for f in fs) else 0)"

run:
	python3 -m aurion.runtime.app

all: lint types cov causality

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
