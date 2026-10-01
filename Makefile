.PHONY: install lint format typecheck test check db-up db-down migrate

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

db-up:
	docker compose up -d --wait db

db-down:
	docker compose down

migrate: db-up
	uv run alembic upgrade head

test: db-up
	uv run pytest

check: lint typecheck test
