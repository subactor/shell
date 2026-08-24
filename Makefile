.PHONY: install dev test smoke

install:
	python -m pip install -e .

dev:
	python -m pip install -e '.[dev]'

test:
	pytest

smoke:
	subactor-shell one "hello from Subactor Shell Bridge"
