# Thin wrapper over tasks.py so `make` and `python tasks.py` stay in sync.
# Windows without make: `uv run python tasks.py <target>`.
RUN := uv run python tasks.py

.PHONY: install check lint typecheck fmt test eval ui

install:
	uv sync --all-extras

check:
	$(RUN) check

lint:
	$(RUN) lint

typecheck:
	$(RUN) typecheck

fmt:
	$(RUN) fmt

test:
	$(RUN) test

eval:
	$(RUN) eval

ui:
	$(RUN) ui
