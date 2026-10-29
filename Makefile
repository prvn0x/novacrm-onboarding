.PHONY: install test lint typecheck check run once deals smoke pdf api-doc demo zip

install:            ## dependencies into .venv
	uv sync

test:               ## the offline suite (fakes only, no accounts needed)
	uv run pytest

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run mypy

check: lint typecheck test   ## everything CI would run

run:                ## watch the inbox (needs .env and config/team.yaml)
	uv run novacrm run

once:               ## one poll, then exit
	uv run novacrm run --once

deals:
	uv run novacrm deals

smoke:              ## the happy path against the real accounts: send the deal email first
	uv run novacrm check && uv run novacrm run --once && uv run novacrm deals

pdf:                ## the design doc as a PDF, for the submission
	uv run python scripts/export_pdf.py

api-doc:            ## regenerate docs/API_REQUESTS.md from the adapters
	uv run python scripts/api_examples.py

demo:               ## every scenario offline, no accounts
	uv run novacrm demo

zip:                ## the submission zip, without private files, secrets or local state
	rm -f ../novacrm-onboarding.zip
	cd .. && zip -qr novacrm-onboarding.zip novacrm-onboarding \
	  -x 'novacrm-onboarding/.private/*' 'novacrm-onboarding/.env' 'novacrm-onboarding/var/*' \
	     'novacrm-onboarding/.venv/*' '*/__pycache__/*' '*/.pytest_cache/*' '*/.ruff_cache/*' \
	     '*/.mypy_cache/*' 'novacrm-onboarding/config/team.yaml' '*.DS_Store'
	@echo "wrote ../novacrm-onboarding.zip"
