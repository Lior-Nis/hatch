# Hatch

Autonomous evolutionary short-form children's media studio.

Hatch is not a video generator. The product is the closed evolutionary learning
loop around video generation:

    hypothesis → genome → generation → QA → publishing → analytics
      → fitness → selection → mutation/exploration → next experiment

The product contract (PRD) and the execution backlog live in the Todoist
project **Hatch**. Read the PRD item there before changing behaviour.

## Quick start

    uv sync          # install dependencies into .venv
    make check       # lint + types + tests
    uv run hatch info

Copy `.env.example` to `.env` to override configuration locally. Every setting
is an environment variable prefixed with `HATCH_`. Secrets go in `.env` (never
committed) — never in Todoist, code, or logs.

## Commands

| Command          | What it does                         |
| ---------------- | ------------------------------------ |
| `make install`   | `uv sync`                            |
| `make lint`      | ruff lint + format check             |
| `make format`    | ruff auto-fix + format               |
| `make typecheck` | mypy (strict)                        |
| `make test`      | pytest                               |
| `make check`     | lint + typecheck + test              |

## Layout

    app/            domain modules of the modular monolith
    integrations/   vendor adapters (Higgsfield, Buffer, platform APIs, storage)
    tests/

Vendor SDKs stay inside `integrations/`; the domain depends only on Hatch's own
interfaces.
