# Higgsfield programmatic access: CLI vs API (research, 2026-10-01)

Scope: how Hatch's `MediaGenerator` adapter can drive Higgsfield for ~8s 9:16 clips, and what it costs.
Nothing was generated, purchased or logged in. Tags: **[V]** verified from a primary source,
**[I]** inferred (binary strings, arithmetic, or indirect evidence), **[?]** unknown.

## 0. Findings that change the plan

1. **The installed CLI is stale.** Local is `0.1.40` (built 2026-05-12); npm `latest` is `1.1.26` (2026-09-18) [V].
   Between them the auth flow changed (device login -> OAuth 2.0 PKCE with loopback callback), the backend
   changed (`fnf.higgsfield.ai/agents/*` -> `fnf-api-gw.higgsfield.ai/fnf/developer/v2alpha/*`) and the new
   binary contains the string "Stored credentials use an older auth flow." [I]. Upgrade before logging in:
   `npm install -g @higgsfield/cli@latest`. Whether 0.1.40 still works against production is [?].
2. **CLI and API are two separate products with separate money.** CLI/MCP spend web-subscription credits;
   the API is prepaid pay-as-you-go USD with key+secret auth [V].
3. **The CLI has no API-key / env-var credential.** Login is browser-only; tokens are short-lived [V].
4. **API list prices are public and machine-readable** (no auth):
   `GET https://dash.higgsfield.ai/api/v2/pricing/models/?page=1&page_size=50` [V].
5. **Veo and Sora are not in the API catalog**; Veo is CLI/web only, Sora 2 is web-pricing-table only [V/I].

## 1a. Route A: the CLI (`higgsfield` / `higgs` / `hf`)

Commands and flags, from `--help` on local 0.1.40 and on a 1.1.26 binary unpacked in a scratch dir [V]:

| Command | Flags / notes |
|---|---|
| global | `--json` (raw JSON), `--no-color`, `-v/--version` |
| `generate create <job_set_type> [--param value]...` | Model params are free-form `--name value` (both `--aspect_ratio` and `--aspect-ratio` accepted). Media: `--image`, `--start-image`, `--end-image`, `--video`, `--audio` (1.x adds `--image-references`, `--video-references`, `--audio-references`); each takes an upload/job UUID or a local path (auto-uploaded). `--wait`, `--wait-timeout` (default 10m), `--wait-interval` (default 3s). Prompt may come from stdin. 1.x calls the positional `<job_type>`. |
| `generate cost <job_set_type> [--param value]...` | Same params as create; "Estimate credits without creating a job". 1.x adds `generate cost workflow <name>`. |
| `generate get <id>` | Show one job. |
| `generate wait <id>` | `--timeout` (10m), `--interval` (3s), `-q/--quiet`. |
| `generate list` | `--image`, `--video`, `--text`, `--size` (20); 1.x adds `--audio`. |
| `model list` | `--image`, `--video`, `--text`; 1.x adds `--audio`. |
| `model get <job_set_type>` | Parameter schema, defaults, enums. |
| `upload create <file>` / `upload list` | list: `--image`, `--video`, `--audio`, `--size` (20). |
| `account status` | "Show account email, plan, and available credits". No flags. |
| `account transactions` | `--size` (20, max 100), `--cursor` (int in 0.1.40, string in 1.x). |
| `auth login` / `logout` / `token` | 0.1.40: "browser-based device login". 1.x: "OAuth 2.0 PKCE", `--port` for the loopback callback. `token` prints the access token. |
| `workspace list` / `set <id>` / `status` / `unset` | "Select billing workspace". |
| 1.x only | `workflow`, `preset`, `voices`, `website`, `game`, `generate workflow <name>`. |

Unauthenticated behaviour [V]: `model list`, `account status`, `auth token`, `workspace status` all return
`Error: Not authenticated. Hint: Run: hf auth login`. So the live catalog and `generate cost` need a login.

Credentials and environment:
- Credentials file: `~/.config/higgsfield/credentials.json` (dir exists locally with only `credentials.json.lock`
  and `update.json`) [V dir / I filename]. Fields include `access_token`, `refresh_token`, `expires_in` [I].
- Env vars in 0.1.40 binary [I]: `HIGGSFIELD_CREDENTIALS_PATH`, `HIGGSFIELD_API_URL`, `HIGGSFIELD_APP_URL`,
  `HIGGSFIELD_DEVICE_AUTH_URL`, `HIGGSFIELD_NO_UPDATE_CHECK`, `HIGGSFIELD_DISABLE_TELEMETRY`,
  `HIGGSFIELD_TELEMETRY`, `HIGGSFIELD_SENTRY_DSN`, `HIGGSFIELD_INSTALL_METHOD`, `HIGGSFIELD_PACKAGE_MANAGER`,
  plus `DO_NOT_TRACK`. 1.1.26 adds `HIGGSFIELD_CONFIG_PATH`, `HIGGSFIELD_WORKSPACE_ID`, `HIGGSFIELD_USER_ID`,
  `HIGGSFIELD_SURFACE`, `HIGGSFIELD_OAUTH_{CLIENT_ID,AUTHORIZATION_URL,TOKEN_URL,REDIRECT_URI,AUDIENCE,SCOPES}`.
- **No `HIGGSFIELD_API_KEY`/`HF_KEY`-style variable exists in either binary** [I]. Official help: "No API key
  needed ... API keys belong to the Higgsfield API, a separate developer product" [V].
- README: "tokens are short-lived. Re-run `higgsfield auth login`" on `Session expired` [V]. The binary has a
  refresh path (`auth.Refresh`) [I]; how long an unattended session survives is [?].
- Telemetry (Sentry) is on by default; the binary detects agent hosts (Claude Code, Codex, Cursor) [I].

JSON output shapes (source repo `higgsfield-ai/cli-src` is private, HTTP 404; shapes are from Go struct tags):

| Call | Shape | Status |
|---|---|---|
| `generate create --json` (no `--wait`) | job IDs | [V] skill doc: "Without `--wait`, you get the job IDs" |
| `generate create --wait --json` | array of final job objects | [V] skill doc |
| job object | `id`, `status`, `result_url`, `job_set_type`; also `min_result_url`, `fail_reason`, `params`, `created_at` | `status`/`result_url` [V] via README `jq '.[] | select(.status=="completed") | .result_url'`; rest [I] |
| `generate list --json` | array of jobs (README) / `{items, cursor}` in 1.x SDK types | [V] / [I] |
| `generate cost --json` | `{"credits": float, "credits_exact"?: float}` (1.x: `{"credits": float}`) | [I] |
| `account status --json` | `{"email", "credits", "subscription_plan_type"}` | [I] |
| `account transactions --json` | `{"items":[{"display_name","credits","action","created_at"}], next_cursor}` | [I] |
| upload | `{"id", "upload_url", ...}` then confirm | [I] |

Job statuses: `queued`, `in_progress`, `completed`, `failed`, `nsfw`, `canceled`, plus `ip_detected` (content
policy) [V docs + skill troubleshooting; CLI terminal set [I]]. Human output on failure:
`Job ended with status "failed"`, `Timeout after 10m; last status "..."`. HTTP 429 and Cloudflare/DataDome
captcha HTML responses are documented failure modes [V].

Result URL: `result_url` on the job object; `--wait` prints it [V]. CDN host and expiry are [?].

## 1b. Route B: Higgsfield API (`api.higgsfield.ai`)

Console `console.higgsfield.ai` / `cloud.higgsfield.ai` (both redirect to `open.higgsfield.ai`), docs
`docs.higgsfield.ai` [V].

- **Auth**: `Authorization: Key <KEY_ID>:<KEY_SECRET>`. Legacy `hf-api-key` + `hf-secret` headers also accepted.
  Not a Bearer token. 401 body `{"detail":"Invalid credentials"}` [V].
- **SDK**: `pip install higgsfield-client` (PyPI 0.2.0, 2026-09-17, Python >=3.8, only dep `httpx`), sync and
  async. Env: `HF_KEY="id:secret"` or `HF_API_KEY` + `HF_API_SECRET`. Node: `@higgsfield/client` [V].
- **Submit**: `POST https://api.higgsfield.ai/<endpoint-id>` with JSON body. Optional `Idempotency-Key` header
  (1-255 chars; same key + same body returns the original `request_id`, no second charge; mismatch -> 422) [V].
  Response: `{"status":"queued","request_id","status_url","cancel_url"}` [V].
- **Poll**: `GET /requests/{request_id}/status` (use the returned `status_url`). Recommended 2s rising to 10s
  with jitter. Completed video: `{"status":"completed","request_id","video":{"url":"https://...mp4"}}` [V].
- **Cancel**: `POST /requests/{id}/cancel`, only while queued; 202 on success, 400 once started [V].
- **Status enum**: `queued`, `in_progress`, `completed`, `failed`, `nsfw`, `canceled` [V].
- **Webhooks**: add `?hf_webhook=<https url>` to the submit URL. POST envelope
  `{"request_id","status","error","payload":{"video":{"url","content_type"}}}` on `completed`/`failed`/`nsfw`;
  retried up to 2h on 5xx/network, not on 4xx; duplicates possible; must answer within 10s. No signature
  scheme is documented [V / ? for signing].
- **Cost estimate**: `POST /estimate/<endpoint-id>` with the same body -> `{"credits":"1.500","usd":"0.094"}`
  (docs example, implies about $0.0627 per API credit) [V format]. Not wrapped by the Python SDK [V].
- **Billing rules**: `failed` and `nsfw` are not charged; cancelled queued requests are refunded; balance
  credits expire after one year; outputs retained "at least seven days" so download immediately [V].
- **Concurrency**: over the limit returns HTTP 400 `Maximum number of concurrent requests (N) has been reached`;
  no `Retry-After`. Public tiers by USD purchased in a 28-day window: $0 -> 2, $25 -> 10, $100 -> 20,
  $1000 -> 40 [V, `dash.higgsfield.ai/api/v2/concurrency/tiers/`]. The pricing FAQ also says a first key gets 20
  concurrent as a launch offer [V]; which one applies to a new account is [?].
- **Uploads** (image-to-video inputs): `POST /files/generate-upload-url {"content_type"}` ->
  `{public_url, upload_url, upload_headers}`, then PUT the bytes; SDK `upload_file(path)` returns the URL [V].
- Also present: an "Agent API" (`/v1/agent/sessions`), an LLM-driven session that bills an LLM turn on top of
  generations. Not suitable for a deterministic adapter [V].

## 2. Video models per route

### API endpoints (text-to-video; each family also has `image-to-video`) [V docs + pricing endpoint]

| Endpoint id | 9:16 | Duration | Resolution | Native audio | List price |
|---|---|---|---|---|---|
| `alibaba/wan-3.0/text-to-video` | yes | 2-30s | 480p/720p/1080p (**default 1080p**) | `generate_audio` (default true) | $0.05 / $0.10 / $0.20 per s |
| `kling-video/v2.6/pro/text-to-video` | yes | 5 or 10s | n/a | `sound` on/off (default on) | $0.07 and $0.14 per s (off/on [I]) |
| `kling-video/v3.0/std/text-to-video` | yes | 3-15s | n/a | `sound` (default on) | from $0.084/s; $0.126/s shown for Standard [I: with sound] |
| `kling-video/v3.0-turbo/text-to-video` | yes | 3-15s | 720p/1080p | none | $0.112 / $0.14 per s |
| `lightricks/ltx-2.5/text-to-video/fast` | yes (16:9, 9:16 only) | 6/8/10s | 720p-4k | `generate_audio` (default true) | $0.09/s at 720p, $0.13 at 1080p |
| `pixverse/v6/text-to-video` | yes | 1-15s | 360p-1080p | `generate_audio` (default true) | $0.0978/s listed at 1080p (15% off $0.115) |
| `xai/grok-imagine-video/v1.5/reference-to-video` | [?] | 1-15s | 480p-1080p | [?] | $0.08/s (480p) to $0.25/s |
| `wan/v2.7/text-to-video` | yes | 2-15s | 720p/1080p | `audio_url` input only | $0.10 / $0.15 per s |
| `wan/v2.6/text-to-video` | **no aspect_ratio field** | 5/10/15s | 720p/1080p | `audio_url` input | $0.10 / $0.15 per s |
| `minimax/hailuo-2.3/standard/text-to-video` | **no aspect_ratio field** | 6 or 10s | fixed 768p | none | $0.0467/s (6s), $0.056/s (10s) |
| `kling-video/v2.5-turbo/pro/text-to-video` | **no aspect_ratio field** | 5 or 10s | 1080p | none | $0.07/s (std i2v: $0.042/s at 720p) |
| `bytedance/seedance-2.0/text-to-video` | yes | 4-15s | 480p-4k | `generate_audio` (default true) | from $0.1407/s (480p) |
| `bytedance/seedance-2.5/text-to-video` | yes | 4-30s | 480p-1080p | `generate_audio` (default true) | from $0.2057/s (480p) |

The pricing endpoint only exposes sample configurations per mode ("from" values). Treat `POST /estimate/...`
as the authority. Not in the API catalog: Veo, Sora, Seedance 1.5, DoP [V for absence from docs + pricing data].

### CLI `job_set_type` ids (from `MODELS.md` in `higgsfield-ai/cli`, generated from `model list`) [V]

| id | 9:16 | Duration | Resolution | Audio param |
|---|---|---|---|---|
| `seedance1_5` (Seedance 1.5 Pro) | yes | 4 / 8 / 12 | 480p/720p/1080p | `generate_audio` (default true) |
| `seedance_2_0`, `seedance_2_0_mini` | yes | integer (4-15) | 480p-4k (mini: 480p/720p) | `generate_audio` (default true) |
| `seedance_2_5` | yes | 4-30 | up to 1080p; `--mode t2v` | yes |
| `kling3_0` | yes | 3-15 | `--mode std|pro|4k` | `--sound on|off` (default on) |
| `kling3_0_turbo` | yes | 3-15 | 720p/1080p | none |
| `kling2_6` | yes | 5 / 10 | n/a | `--sound` boolean (default true) |
| `veo3_1` | yes (16:9, 9:16) | 4 / 6 / 8 | `--quality basic|high|ultra`; `--variant veo-3-1-fast|veo-3-1-preview` | native [I] |
| `veo3_1_lite` | yes | 4 / 6 / 8 | n/a | `generate_audio` (default **false**) |
| `veo3` | yes | fixed | n/a | **requires `--start-image`** (no pure text-to-video) |
| `wan2_7` | yes | integer | 720p/1080p | audio reference input |
| `wan2_6` | yes | 5 / 10 / 15 | `--quality 720p|1080p` | audio reference input |
| `grok_video` | yes | integer | n/a | [?] |
| `minimax_hailuo` | **no aspect_ratio param** | 6 / 10 | 512/768/1080 | none |

Also mentioned by the official skill catalog as valid ids not in `model list`: `wan3_0`, `wan3_0_prime`,
`minimax_h3`, `cinematic_studio_video_4_0`, `happy_horse_video` [V mention, ? params]. Sora 2 and Higgsfield DoP
appear in the web pricing table but in neither `MODELS.md` nor the API catalog [V absence]. Always confirm with
`higgsfield model get <id> --json` after login.

## 3. Pricing

### Subscription credits (CLI / MCP / web), live `higgsfield.ai/pricing`, 2026-10-01 [V]

| Plan | Price | Credits/mo | USD per credit | Video concurrency |
|---|---|---|---|---|
| Free | $0 | 0 | n/a | 1 |
| Starter | $19/mo (same on annual) | 270 | $0.0704 | 2 |
| Plus | $59/mo, $47/mo billed annually | 1,200 | $0.0492 / $0.0392 | 6 |
| Ultra | $129/mo, $99/mo billed annually | 3,000 (6,000 / 9,000 options) | $0.0430 / $0.0330 | 8 |

Conflict: Higgsfield's own blog (dated 2026-09-11/15) states Starter $15 / 200 credits and Plus $49 / 1,000.
The live page above is newer; plans are evidently changing, so re-check at purchase. Top-up pack prices and
whether monthly credits roll over are [?].

Credits per clip, derived from the pricing page's "Compare features" table (annual view, so
credits = plan credits x 12 / listed video count; consistent across all three plans) [I arithmetic on V data].
The table does not state clip length; values match about 5s for Kling/Wan and 8s for Veo.

| Model (web name) | Credits per listed clip | Plan availability |
|---|---|---|
| Seedance 1.5: 480p / 720p / 1080p | 1.5 / 3 / 7.5 | Starter+ |
| Kling 2.5 Turbo 720p / 1080p | 4 / 6 | Starter+ (not in CLI `MODELS.md`) |
| Hailuo 2.3 Fast 768p / Hailuo 2.3 768p | 4 / 6 | Starter+ |
| Kling 2.6 without / with sound | 5 / 10 | Starter+ |
| Wan 2.6 and Wan 3.0 at 480p | 5 | Starter+ |
| Kling 3.0 720p; Wan 2.6 720p; Grok Video | 7.5 | Starter+ |
| Kling 3.0 1080p; Wan 3.0 720p | 8.75 | Plus+ / Starter+ |
| Seedance 2.0 Fast 720p | 12.5 | Starter+ |
| Veo 3.1 Fast 720p/1080p | 16 | Plus+ |
| Seedance 2.0 720p / 1080p | 22.5 / 45 | Plus+ |
| Veo 3.1 720p/1080p | 40 | Plus+ |

Directly stated by Higgsfield [V]: Kling 3.0, 8s, 720p is "about 14 credits" (pricing page); Kling 3.0, 8s, 1080p
is 20 credits and Seedance 2.5, 8s, 1080p is 72 credits (blog, September 2026).

### Approximate USD per ~8s 9:16 clip

Budget: target < $0.50, hard max $1.50 per finished short. If a short is stitched from N clips, multiply.

| Route / model | Config | Cost | Fits |
|---|---|---|---|
| API `alibaba/wan-3.0/text-to-video` | 8s, 480p, audio | $0.40 | target |
| API `alibaba/wan-3.0/text-to-video` | 8s, 720p, audio | $0.80 | hard max |
| API `kling-video/v2.6/pro/text-to-video` | 5s silent / 5s sound / 10s silent | $0.35 / $0.70 / $0.70 [I] | target / max / max |
| API `kling-video/v3.0/std/text-to-video` | 8s silent / with sound | $0.67 / $1.01 [I] | hard max |
| API `lightricks/ltx-2.5/text-to-video/fast` | 8s, 720p, audio | $0.72 | hard max |
| API `pixverse/v6/text-to-video` | 8s, 1080p, audio | $0.78 | hard max |
| API `kling-video/v3.0-turbo/text-to-video` | 8s, 720p, silent | $0.90 | hard max |
| API Seedance 2.0 / 2.5 | 8s, 480p | $1.13 / $1.65 | max / **no** |
| CLI `seedance1_5` | 8s, 720p (about 6 credits if the 3-credit unit is 4s [I]) | $0.42 Starter, $0.30 Plus, $0.24 Plus annual | target, unverified |
| CLI `kling2_6` | 10s silent (10 credits [I]) | $0.70 Starter, $0.49 Plus | max / target |
| CLI `kling3_0` | 8s, 720p (14 credits [V]) | $0.99 Starter, $0.69 Plus, $0.55 Plus annual | hard max |
| CLI `veo3_1` fast | 8s (16 credits [I]) | $0.79 Plus, $0.63 Plus annual, $0.53 Ultra annual | hard max, Plus+ only |
| CLI `seedance_2_5` | 8s, 1080p (72 credits [V]) | $3.5-$5.1 | **no** |

Subscription maths assume every monthly credit is used; unused credits raise the real per-clip cost. Whether
audio changes the Seedance 1.5 or Wan 3.0 price is [?]. Retries are billed on both routes unless the job ends
`failed`/`nsfw` (API: explicitly not charged [V]; CLI: [?]).

API launch offers seen on the pricing page [V, time-limited]: 15% off "sale models" on sign-up, pick 3 models
for up to 75% off, $15 balance for a verified business email plus card. The related cashback pool endpoint
reports `exhausted: true`, `expires_at: 2026-10-01`, so do not budget on it.

## 4. Credit pools and minimum tier

- CLI, MCP and web share **one** pool: "Both use the same Higgsfield account and plan credits";
  "Every generation through MCP, the ChatGPT plugin, or the CLI deducts credits at standard rates" [V].
- Unlimited models and free generations **do not apply** to CLI/MCP [V, help center and pricing page].
- Minimum tier: "An active paid subscription is required" for agent access [V, MCP help page; the CLI page
  itself only says "plan credits"]. Free has 0 credits, so Starter ($19/mo) is the practical floor. Starter is
  limited to "selected models" and 2 parallel videos; Veo and most 1080p tiers need Plus [V].
- CLI use "falls under the Developer Terms" [V]; there is "no native credit cap" for agent sessions [V].
- The API is a **separate** prepaid balance: "pay-per-generation pricing with no subscriptions or seat fees";
  "you top up a balance and pay per generation"; no sales call needed [V]. No source says subscription credits
  can be spent through API keys; treat the pools as separate [I]. Minimum top-up amount is [?].

## 5. Recommendation

**Wrap the HTTP API (official `higgsfield-client` SDK or plain `httpx`), not the CLI, for the first slice.**

- Non-interactive key+secret auth works in a server, CI and tests. The CLI needs a human browser login and
  short-lived tokens, and has no key-based option.
- The API returns cost in USD before submitting (`/estimate`), so the budget guard is one call. The CLI only
  returns credits, and USD depends on which plan was bought.
- Documented contract: status enum, idempotency keys, webhooks, "failed is not charged". The CLI's JSON shapes
  are undocumented and already changed between 0.1.x and 1.x.
- No monthly commitment; $0.40 (480p) or $0.80 (720p) per 8s clip with native audio on Wan 3.0.

Trade-off: the cheapest 720p-with-audio option is probably CLI `seedance1_5` on a subscription (about
$0.25-$0.45 per 8s clip), and Veo exists only there. Keep `MediaGenerator` narrow enough that a
`HiggsfieldCliGenerator` can be added later if a subscription is already being paid for.

### Concrete request (recommended)

```bash
# 1. cost, in USD            -> {"credits":"...","usd":"..."}
curl -sX POST https://api.higgsfield.ai/estimate/alibaba/wan-3.0/text-to-video \
  -H "Authorization: Key $HF_API_KEY:$HF_API_SECRET" -H "Content-Type: application/json" \
  -d '{"prompt":"...","duration":8,"resolution":"480p","aspect_ratio":"9:16","generate_audio":true}'
# 2. submit                  -> {"status":"queued","request_id","status_url","cancel_url"}
curl -sX POST https://api.higgsfield.ai/alibaba/wan-3.0/text-to-video \
  -H "Authorization: Key $HF_API_KEY:$HF_API_SECRET" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"prompt":"...","duration":8,"resolution":"480p","aspect_ratio":"9:16","generate_audio":true}'
# 3. poll status_url until terminal -> {"status":"completed","video":{"url":"https://...mp4"}}
```

```python
import higgsfield_client  # reads HF_KEY="id:secret" or HF_API_KEY + HF_API_SECRET

result = higgsfield_client.subscribe(
    "alibaba/wan-3.0/text-to-video",
    arguments={
        "prompt": "...",
        "duration": 8,
        "resolution": "480p",
        "aspect_ratio": "9:16",
        "generate_audio": True,
    },
)
mp4_url = result["video"]["url"]  # download now; retention is only guaranteed for 7 days
```

Set `resolution` explicitly: the Wan 3.0 default is 1080p ($0.20/s). The `/estimate/<endpoint-id>` path is
documented with a Soul example only; its use with this endpoint is [I].

### CLI equivalent (after `npm i -g @higgsfield/cli@latest` and a human `higgsfield auth login`)

```bash
higgsfield generate cost seedance1_5 --prompt "..." --aspect_ratio 9:16 --duration 8 \
  --resolution 720p --generate_audio true --json            # -> {"credits": N}
higgsfield generate create seedance1_5 --prompt "..." --aspect_ratio 9:16 --duration 8 \
  --resolution 720p --generate_audio true --wait --wait-timeout 20m --json
# -> array of job objects; take [0].result_url when [0].status == "completed"
```
USD = credits x plan rate (config value); reconcile with `higgsfield account transactions --json`.

## 6. Verified vs inferred vs unknown

**Verified** (primary sources read on 2026-10-01): all CLI flags above (local `--help`, 0.1.40 and 1.1.26);
unauthenticated commands fail with "Not authenticated"; CLI model ids and params (`MODELS.md`); API auth,
endpoints, input schemas, status enum, webhook envelope, idempotency, estimate response format, billing and
retention rules (docs.higgsfield.ai); API per-second list prices and concurrency tiers (public `dash` endpoints);
subscription plan prices, credits and the compare table (live pricing page); shared CLI/web credit pool, no
unlimited on CLI, paid subscription required (help center); SDK version, env vars and base URL (PyPI + source).

**Inferred**: every CLI JSON shape except `status` / `result_url`; credentials filename and fields; absence of a
CLI API-key env var; credits-per-clip figures and the clip length behind them; Kling "sound on" price mapping;
all "8s" CLI costs except Kling 3.0; API and subscription pools being separate; `/estimate` on non-Soul endpoints.

**Unknown** (needs a login or a purchase to settle): exact `generate cost` for `seedance1_5`, `veo3_1_lite`,
`wan3_0` and others at 8s 9:16; whether 0.1.40 still authenticates; CLI token lifetime for unattended use;
whether CLI failed jobs are refunded; result URL host and expiry on the CLI route; credit rollover and top-up
prices; API minimum top-up; real concurrency for a new API account; webhook signing; whether audio is priced
separately on Wan 3.0 / Seedance 1.5; output pixel dimensions at "480p" 9:16; kids-content moderation behaviour.

## Sources

- Local: `higgsfield <cmd> --help` (0.1.40), `strings` on `~/.npm-global/lib/node_modules/@higgsfield/cli/vendor/hf`
- https://github.com/higgsfield-ai/cli (README.md, MODELS.md, release v1.1.26 binary `--help` and strings)
- https://github.com/higgsfield-ai/skills (`higgsfield-generate/SKILL.md`, `references/model-catalog.md`, `references/troubleshooting.md`)
- https://github.com/higgsfield-ai/higgsfield-client and https://pypi.org/project/higgsfield-client/
- https://docs.higgsfield.ai/docs/llms.txt and linked pages: `authentication.md`, `concepts/requests.md`,
  `concepts/polling.md`, `concepts/idempotency.md`, `concepts/billing-and-retention.md`, `concepts/rate-limits.md`,
  `how-to/webhooks.md`, `help/faq.md`, `models/video-generation.md`, per-model pages under `models/`
- https://open.higgsfield.ai/pricing and its data: https://dash.higgsfield.ai/api/v2/pricing/models/?page=1&page_size=50,
  https://dash.higgsfield.ai/api/v2/concurrency/tiers/
- https://higgsfield.ai/pricing (rendered in a browser; plan cards and compare table)
- https://higgsfield.ai/blog/credits-vs-unlimited-ai-video-generation
- https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-access-higgsfield-via-cli
- https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent

