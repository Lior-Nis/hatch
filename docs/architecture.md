# Hatch architecture

Hatch is a Python modular monolith. One process type runs the CLI, one runs
background jobs (`hatch worker`), one serves the operator UI (`hatch serve`).
All three share one Postgres database. There are no microservices and no
message broker: the job queue is a Postgres table.

The product contract is the PRD in the Todoist project **Hatch**. This file
describes how the code realises it and records the assumptions made on the way.

## The loop

    experiment memory ─► hypothesis ─► genome ─► script / storyboard
          ▲                                             │
          │                                             ▼
    exploit / mutate / explore                 media generation (Higgsfield)
          ▲                                             │
          │                                             ▼
      IP fitness ◄─ video fitness ◄─ analytics ◄─ publishing ◄─ human approval ◄─ automated QA

Every arrow is a stored record, so any video can be traced back to the
hypothesis and decision that caused it (`hatch lineage`, `hatch trace`).

## Module map

| Package | Owns |
|---|---|
| `app/ips` | IPs, characters, canon, IP lifecycle states |
| `app/experiments` | Experiment, hypothesis, creative spec, video lifecycle, lineage queries |
| `app/creative` | Genome (typed genes, versioned), hypothesis and candidate generation, experiment memory |
| `app/evolution` | Mutation, anti-cloning, evidence, replication, IP lifecycle decisions, allocation, parent selection, planner, the evolution cycle |
| `app/production` | Orchestration graph (script, storyboard, scenes, assembly), retries, model routing, the `MediaGenerator` port |
| `app/quality` | QA gates, the fail-closed runner, human review, audits, shadow autonomy |
| `app/publishing` | Platform packaging, publishability check, posting slots, the `Publisher` port |
| `app/analytics` | Immutable metric snapshots, observation checkpoints, completeness audit, the `AnalyticsAdapter` port |
| `app/fitness` | Video fitness (heuristic v1), account baselines, IP fitness aggregation |
| `app/knowledge` | Derived findings synthesised from evidence (may be rewritten; evidence may not) |
| `app/budgets` | Spend ledger, budget governor, spend reports |
| `app/llm` | `LanguageModel` port and the priced, recorded `call_model` wrapper |
| `app/scheduling` | Durable job queue, worker, recurring cycles |
| `app/observability` | Structured logs, per-experiment trace, health checks |
| `app/admin` | Operator dashboard and review UI |
| `app/pilot` | 30-day pilot review report |
| `integrations/*` | Vendor adapters: Higgsfield, Buffer, Anthropic, YouTube, TikTok, Meta, object storage, and fakes for tests |

`app/bootstrap.py` is the only place that chooses concrete adapters. Domain
code depends on ports and never sees a vendor response object. Each adapter
records provider, model and configuration so a decision can be reproduced.

## Hard boundaries in code

- **Safety.** A mandatory QA rejection is terminal. `QA_REJECTED` has no
  transition to an approved state, and `assert_publishable` re-derives
  publishability from stored QA and review evidence, never from a score.
  When no content-review model is configured the content gates escalate
  instead of passing.
- **Human approval.** A video reaches the review queue only after QA, and is
  published only after a recorded human approval. No code path publishes
  without one. `app/quality/shadow.py` measures agreement between QA and
  humans; it contains no switch.
- **Budget.** Every paid call (media and language model) is priced first and
  reserved through the governor: per generation, per video, rolling 24 hours,
  rolling 30 days. A refusal is stored as a blocked ledger row and no vendor
  call is made. Limits come from settings and are never raised by code.
- **Evidence.** Rows using the `Evidence` mixin (metric snapshots, QA results,
  reviews, ledger entries, decisions) are append-only. A flush that modifies
  one raises `ImmutableEvidenceError`. Derived knowledge is stored separately.
- **Lifecycles.** IP status, experiment status and video status are state
  machines enforced on assignment.

## Evolution rules

- Video fitness and IP fitness are separate. Video fitness compares a post
  with its own account's baseline per platform and never uses views alone.
  IP fitness is a confidence-weighted aggregate over at least three videos.
- A strong video only earns a replication request: three to five controlled
  descendants that keep the mechanism genes and tell a new story. The parent's
  hypothesis is promoted only on a `supported` verdict.
- Allocation targets 60% exploit, 25% mutate, 15% explore. With nothing
  replicated yet, the exploit share falls through to mutation and exploration.
  Exploration never drops to zero.
- Anti-cloning rejects candidates that are lexically too close to an existing
  video's surface or that name an external IP.

## Canonical media and accounts

A video is generated once. Platform variants differ only in packaging: title,
caption, hashtags, made-for-kids flags. Each IP has one account per platform
(3 IPs × 4 platforms). Hatch owns post ids, snapshots and decisions. Buffer is
a delivery mechanism, not a store of record.

## Recorded assumptions

These were chosen as the simplest option compatible with the PRD. Each one
that changes cost, safety or product behaviour has its own decision task in
Todoist.

| Assumption | Where | Status |
|---|---|---|
| Higgsfield prepaid HTTP API, Wan 3.0 at 480p, about $0.40 per 8 s clip | `docs/research/higgsfield-api.md` | Decision task open (resolution tier) |
| Per-generation ceiling of $0.75, half the per-video maximum | `app/config.py` | Stated in the same decision task |
| Daily ceiling at the low end of the approved range ($15) | `app/config.py` | Operator may set up to $20 |
| Content QA uses one multimodal Claude call shared by four gates | `app/quality/content.py` | Unverified against a live model |
| Buffer has no idempotency key, so Hatch looks before it creates | `docs/research/buffer-publishing.md` | Unverified against the live API |
| Observation checkpoints at 1h, 6h, 24h, 72h, 7d, 30d | `app/analytics/ingestion.py` | |
| Fitness is a transparent heuristic, not a learned model | `app/fitness/heuristic.py` | Replaceable behind `FitnessEvaluator` |
| Autonomy thresholds: 300 compared decisions, at most 1% false positives | `app/quality/shadow.py` | Operator decides Stage C regardless |
| Kids-content platform flags default to the conservative setting | `app/publishing/packaging.py` | Decision task open (platform policy) |

Everything that talks to a vendor has been tested only against fakes and
recorded-shape stubs. The Todoist tasks labelled `awaiting-credentials` stay
open until each is verified with a live run.
