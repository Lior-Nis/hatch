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
    make migrate     # start local Postgres (Docker) and apply migrations
    make check       # lint + types + tests (tests use a separate hatch_test DB)
    uv run hatch info

## Vertical slice

    HATCH_MEDIA_PROVIDER=fake uv run hatch run-fixture   # free synthetic video
    uv run hatch run-fixture                             # real Higgsfield generation (needs API keys)
    uv run hatch lineage <experiment_id>                 # why the video exists and how it was made
    uv run hatch serve                                   # human review UI at http://127.0.0.1:8321/review
    uv run hatch costs                                   # spend by video, IP, provider, day

Background mode (durable queue in Postgres; survives restarts):

    uv run hatch run-fixture --background                # queue the production
    uv run hatch worker                                  # run queued jobs (generation → QA)
    uv run hatch jobs                                    # status, attempts, timing per job

Operations:

    uv run hatch trace <experiment_id>                   # everything that happened, in order
    uv run hatch health                                  # provider failures, failed jobs, stalls (exit 1 on problems)

Logs are JSON lines on stderr (`HATCH_LOG_FORMAT=text` for plain text).

Every paid call is priced first and must pass the budget governor
(per-generation, per-video, rolling 24h, rolling 30 days). A blocked attempt is
recorded and no provider call is made.

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
| `make db-up`     | start local Postgres (port 54329)    |
| `make migrate`   | apply Alembic migrations             |

Schema changes: edit the models, then
`uv run alembic revision --autogenerate -m "..."`. A test fails if migrations
and models drift apart.

## Layout

    app/            domain modules of the modular monolith
    integrations/   vendor adapters (Higgsfield, Buffer, platform APIs, storage)
    tests/

Vendor SDKs stay inside `integrations/`; the domain depends only on Hatch's own
interfaces.
