.PHONY: install test lint typecheck run
install:
	python -m pip install -e '.[dev]'
test:
	pytest
lint:
	ruff check .
typecheck:
	mypy src
run:
	algen-agent-runtime
