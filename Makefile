.PHONY: install dev test lint run clean

install:
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -e .

dev: install
	.venv/bin/pip install -e ".[dev]"

test:
	.venv/bin/python -m pytest

doctor:
	.venv/bin/python -m gmscrape doctor

providers:
	.venv/bin/python -m gmscrape providers

clean:
	rm -rf out/*.csv out/*.json out/*.jsonl out/*.xlsx
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
