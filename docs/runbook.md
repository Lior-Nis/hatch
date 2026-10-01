# Hatch operator runbook

From an empty machine to a running 30-day pilot. Steps marked **(you)** need a
human: they involve accounts, billing or approval that Hatch must not do.
Each has its own task in Todoist with plan, price and the exact action.

Secrets go in `.env` only. Never paste them into Todoist, code or logs.

## 1. Install

Needs Docker, [uv](https://docs.astral.sh/uv/), ffmpeg.

    uv sync
    cp .env.example .env
    make migrate              # starts Postgres and applies migrations
    make check                # lint, types, tests
    uv run hatch seed-ips     # Nibbin Hollow, Puzzle Pond, Sock Planet
    uv run hatch seed-models  # video model catalogue for routing
    uv run hatch info         # confirms environment and budget limits

Free rehearsal of the whole production path, no vendor calls:

    HATCH_MEDIA_PROVIDER=fake uv run hatch run-fixture
    uv run hatch serve        # http://127.0.0.1:8321

## 2. Credentials (you)

| Order | Todoist task | Fills in `.env` | Unblocks |
|---|---|---|---|
| 1 | Apply for TikTok API access (long lead) | `HATCH_TIKTOK_CLIENT_KEY/SECRET` | TikTok analytics |
| 2 | Higgsfield API key and prepaid balance | `HATCH_HIGGSFIELD_API_KEY/SECRET` | Real video generation |
| 3 | Anthropic API key | `HATCH_ANTHROPIC_API_KEY` | Hypotheses, novelty, content QA, storyboards |
| 4 | Provision the social accounts | none | Publishing and analytics |
| 5 | Buffer account and API key | `HATCH_BUFFER_API_KEY` | Publishing |
| 6 | Public media bucket (R2) | `HATCH_ASSET_STORE=s3`, `HATCH_S3_*`, `HATCH_ASSET_PUBLIC_BASE_URL` | Publishing |
| 7 | Google Cloud OAuth client | `HATCH_YOUTUBE_CLIENT_ID/SECRET` | YouTube analytics |
| 8 | Meta developer app | `HATCH_META_APP_ID/SECRET` | Instagram and Facebook analytics |

Two decisions are also open: the video resolution tier and the kids-content
platform policy. Hatch runs on conservative defaults until they are answered.

## 3. Verify each stage with a live run

Do these in order. Each closes one or more `awaiting-credentials` tasks.
Stop and inspect after every step; the first real calls cost money.

1. **One real Short.** `uv run hatch run-fixture`, then
   `uv run hatch lineage <experiment_id>` and `uv run hatch costs`.
   Expect one playable 9:16 MP4, a ledger entry near $0.40, full lineage.
2. **Content QA.** With the Anthropic key set, run
   `uv run hatch run-fixture --background` and `uv run hatch worker --until-idle`.
   Open the review page. All gates should show a real verdict with reasons,
   not "no model configured".
3. **Creative agents.** `uv run hatch evolve`, then
   `uv run hatch worker --until-idle`. Expect new candidates per IP with a
   hypothesis and genome each, visible on the dashboard and in
   `uv run hatch decisions`.
4. **Publishing.** Connect the accounts in Buffer, then:

       uv run hatch accounts channels
       uv run hatch accounts map <ip> <platform> --channel-id … --external-account-id …
       uv run hatch accounts list

   Approve one video in the review UI, run `uv run hatch publish-ready` and the
   worker. Expect one post per mapped platform with its platform post id.
5. **Analytics.** Authorise once per account:

       uv run hatch auth youtube
       uv run hatch auth tiktok --redirect-uri <registered uri>
       uv run hatch auth meta

   With a worker running, snapshots arrive at 1h, 6h, 24h, 72h, 7d and 30d
   after publication. `uv run hatch audit-analytics` must report no gaps.

## 4. Run the pilot

Keep two processes up for 30 days, under systemd, tmux or similar:

    uv run hatch worker       # production, QA, publishing, analytics, fitness, evolution
    uv run hatch serve        # dashboard and review queue

The worker schedules its own cycles: evolution every 6 hours and publishing
every 30 minutes. It survives restarts and database blips; jobs are durable
and idempotent.

Cadence is two videos per IP per day, set by `HATCH_PUBLISH_SLOTS_UTC`.
`HATCH_PIPELINE_TARGET_PER_IP` is how many unpublished candidates each IP
keeps in flight. Raise it to build the initial population faster; spending
still stops at the daily ceiling.

Daily routine, about ten minutes:

1. Open `/review`. Approve or reject every waiting video with a reason.
   Resolve escalations in writing. Audit a few automated rejections.
2. `uv run hatch health`. Exit code 1 means provider failures, failed jobs or
   stalled work; the output says which.
3. Glance at `/costs` or `uv run hatch costs`.

Weekly:

    uv run hatch audit-analytics   # every due observation collected or explicitly failed
    uv run hatch decisions         # what evolution decided and why
    uv run hatch knowledge         # what Hatch currently believes, with sample sizes
    uv run hatch autonomy          # agreement between automated QA and your reviews

Day 30:

    uv run hatch pilot-report > pilot-review.md

The memo answers the PRD's seven pilot questions from stored data and
proposes scale, iterate or stop. The decision is yours and is recorded in
the memo's last section. Autonomous publishing stays off whatever the
numbers say; enabling it is a separate, explicit decision and a code change.

## 5. Budget arithmetic

| Item | Figure |
|---|---|
| One 8 s clip, Wan 3.0 480p | about $0.40 |
| Single-scene video | about $0.40 plus language-model calls |
| Three-scene video | about $1.20, under the $1.50 hard maximum |
| Six single-scene videos a day | about $2.40 |
| Six three-scene videos a day | about $7.20 |
| Daily ceiling | $15 |
| 30-day ceiling | $500 |

Rejected and regenerated videos add to this. The target of $0.50 per video
only holds for single-scene videos at 480p. Language-model cost per video is
not yet measured; `hatch costs` will show it after the first live runs.

## 6. When something goes wrong

| Symptom | Meaning | Action |
|---|---|---|
| Ledger rows with status blocked | A limit would have been exceeded | Nothing ran. Wait for the window to roll, or decide on limits yourself |
| Review page shows an escalated gate | A gate could not decide, or no model was configured | Resolve it in the review UI with a written reason before approving |
| Job in failed state | Retries exhausted or a permanent error | `uv run hatch jobs`, then `uv run hatch trace <experiment_id>` |
| `audit-analytics` lists gaps | An observation is overdue with no failure record | Check that the worker is running and the account is authorised |
| Publication failed | Buffer or the platform refused the post | The trace shows the vendor message; fix the account and rerun `publish-ready` |
| Evolution creates nothing | No Anthropic key, pipeline already full, or budget blocked | `uv run hatch decisions` and `uv run hatch health` |
