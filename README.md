# Hatch

Autonomous evolutionary short-form children's media studio.

Hatch is not a video generator. The product is the closed evolutionary learning
loop around video generation:

    hypothesis → genome → generation → QA → publishing → analytics
      → fitness → selection → mutation/exploration → next experiment

The product contract (PRD) and the execution backlog live in the Todoist
project **Hatch**. Read the PRD item there before changing behaviour.

See `docs/runbook.md` for the path from an empty machine to a running pilot and
`docs/architecture.md` for the module map, hard boundaries and recorded
assumptions.

## Quick start

    uv sync          # install dependencies into .venv
    make migrate     # start local Postgres (Docker) and apply migrations
    make check       # lint + types + tests (tests use a separate hatch_test DB)
    uv run hatch info

## Vertical slice

    HATCH_MEDIA_PROVIDER=fake uv run hatch run-fixture   # free synthetic video
    uv run hatch run-fixture                             # real Higgsfield generation (needs API keys)
    uv run hatch lineage <experiment_id>                 # why the video exists and how it was made
    uv run hatch serve                                   # dashboard + human review at http://127.0.0.1:8321
    uv run hatch costs                                   # spend by video, IP, provider, day

Background mode (durable queue in Postgres; survives restarts):

    uv run hatch run-fixture --background                # queue the production
    uv run hatch worker                                  # run queued jobs (generation → QA)
    uv run hatch jobs                                    # status, attempts, timing per job

Publishing (needs a Buffer API key, a public media bucket and mapped accounts):

    uv run hatch accounts channels                       # Buffer channels and their ids
    uv run hatch accounts map <ip> <platform> --channel-id … --external-account-id …
    uv run hatch accounts list                           # which accounts each IP has / lacks
    uv run hatch publish-ready                           # give approved videos their next posting slot

Evolution (needs the Anthropic key; runs by itself every 6 hours while a worker is up):

    uv run hatch evolve [ip-slug]                        # run one cycle now
    uv run hatch decisions                               # what was decided and why

Higgsfield SDK example (one billable Seedance 2.5 clip, outside Hatch's budget governor):

    # .env.local (git-ignored): HF_KEY=<key id>:<key secret>
    uv run python -m examples.seedance_2_5.main          # prints the video URL, or why there is none

Hatch itself reads `.env` and then `.env.local`, so the same `HF_KEY` line also
configures the Higgsfield adapter (explicit `HATCH_HIGGSFIELD_API_KEY` and
`HATCH_HIGGSFIELD_API_SECRET` take priority). Tests never read either file.

Operations:

    uv run hatch trace <experiment_id>                   # everything that happened, in order
    uv run hatch health                                  # provider failures, failed jobs, stalls (exit 1 on problems)

Pilot:

    uv run hatch audit-analytics                         # every due observation collected or explicitly failed (exit 1 on gaps)
    uv run hatch pilot-report > pilot-review.md          # the seven pilot questions, economics, recommendation

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
