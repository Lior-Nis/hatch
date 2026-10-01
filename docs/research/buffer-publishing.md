# Buffer as Hatch's publishing backend (research, 2026-10-01)

Scope: can a Python `Publisher` adapter (schedule / publish now / cancel / get status) push one MP4 to YouTube Shorts,
TikTok, Instagram Reels and Facebook Reels via Buffer for 3 brands (~12 channels, ~24 posts/day)? Read-only research:
no account, login or post; the only live calls were unauthenticated GraphQL introspection of `https://api.buffer.com`.
Sources are from 2026 unless flagged. Tags: **[V]** verified in a primary source I read, **[I]** inferred, **[?]** unknown.

## 0. Findings that change the plan
1. **Buffer has a self-serve public API again**: GraphQL at `https://api.buffer.com`, personal API key, every plan incl.
   Free [V]. The legacy REST API (`api.bufferapp.com/1/`) retires **2027-02-01**; do not build on it [V].
2. **No idempotency key, no webhooks, no upload endpoint** [V]: look-before-retry, poll for status, host the MP4 at a
   permanent public HTTPS URL.
3. **Tight rate limits**: 250 req/24 h and 7,500 req/30 d on Essentials, shared by all personal keys [V]. Batch the polling.
4. **Permalink yes (`Post.externalLink`), native post id no** [V]; Hatch derives the id (2.6).
5. **TikTok privacy / comments / duet / stitch are not settable via the API** [V]; what Buffer applies is [?].
6. **Per-post metrics exist but Buffer calls them experimental** and advises against relying on them [V].

## 1. API, access, limits
| Item | Fact |
|---|---|
| Current API | GraphQL, one endpoint `POST https://api.buffer.com`, body `{"query", "variables"}` [V] |
| Status | Docs carry no beta label and say "available on every Buffer plan"; schema changelog has entries from 2026-01-28 to 2026-10-01. No explicit "GA" statement found [?]. Some fields/mutations are tagged Experimental or Preview [V]. |
| Legacy REST v1 | "will be retired on February 1, 2027"; existing apps only [V] |
| Token | Personal API key: Settings -> API -> Personal Access -> "+ New Key" (`publish.buffer.com/settings/api`); header `Authorization: Bearer <key>`. Self-serve, no approval or waitlist [V] |
| Key rules | Org **owner** only; verified account email; expiry is mandatory: 7/30/60/90 days or **1 year max**; permissions `postsRead postsWrite ideasRead ideasWrite accountRead accountWrite insightsRead` [V] |
| Key scope | Acts for the account across all its organizations and channels [V] |
| OAuth | OAuth 2.0 Authorization Code + PKCE "App Clients" (`auth.buffer.com`), self-registered; meant for acting on other users' accounts; cannot hold `insightsRead`. Not needed for Hatch [V] |
| Tooling | CLI `npm i -g @bufferapp/cli` (`BUFFER_API_KEY`), MCP server, browser explorer [V]; no Python SDK mentioned [I] |

| Rolling window, per client [V] | Free | Essentials | Team |
|---|---|---|---|
| 15 min / 24 h / 30 d | 100 / 250 / 3,000 | 100 / 250 / 7,500 | 100 / 500 / 15,000 |
| API keys / app clients | 1 / 1 | 3 / 3 | 5 / 5 |

- "Usage is shared across all your personal API keys as a group": extra keys add no quota [V].
- Every response carries `RateLimit` / `RateLimit-Policy` headers. Over limit: HTTP 429, `Retry-After`,
  `extensions.code = RATE_LIMIT_EXCEEDED`; a 429 costs no quota, other failed requests do. More quota: ask
  developersupport@buffer.com [V].
- Hatch budget [I]: 24 `createPost` + one batched `posts` poll every 15 min (96) = 120 req/day, 3,600 per 30 d.
  Polling each post separately would exceed 250/day.

## 2. Request shapes
All calls: `POST https://api.buffer.com` with `Authorization: Bearer $BUFFER_API_KEY`, `Content-Type: application/json`.
Every document below was validated against the live introspected schema with graphql-core [V]; none was executed.
Errors: docs say GraphQL returns HTTP 200 with either top-level `errors[]` (`UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`,
`UNEXPECTED`, `RATE_LIMIT_EXCEEDED`) or a typed error inside `data` (`... on MutationError { message }`) [V].
Observed: a request with no token gets **HTTP 401** and code `UNAUTHENTICATED`, so handle non-200 too [V].

**2.1 Organizations and channels** [V]
```graphql
query { account { id email organizations { id name channelCount limits { channels scheduledPosts } } } }
query Channels($org: OrganizationId!) { channels(input: { organizationId: $org }) {
  id name displayName service type serviceId externalLink isDisconnected isLocked isQueuePaused timezone
  metadata { ... on TiktokMetadata { defaultToReminders } ... on InstagramMetadata { defaultToReminders }
             ... on YoutubeMetadata { defaultToReminders } } } }
```

`service`: `youtube | tiktok | instagram | facebook | ...`; `type`: `page | profile | business | account | channel | group`.
`serviceId` = "the external ID of the channel on social network API" [V]; exact value per network [?]. Ids are 24-hex.

**2.2 Create a scheduled video post** [V]
```graphql
mutation Create($input: CreatePostInput!) { createPost(input: $input) {
  ... on PostActionSuccess { post { id status schedulingType shareMode dueAt channelId channelService } }
  ... on MutationError { message } } }
```

```json
{ "input": { "channelId": "<24-hex>", "text": "caption / description",
    "schedulingType": "automatic", "mode": "customScheduled", "dueAt": "2026-10-02T15:00:00.000Z",
    "assets": [ { "video": { "url": "https://media.example.com/v/abc.mp4", "metadata": { "thumbnailOffset": 2000 } } } ],
    "metadata": { "youtube": { "title": "...", "categoryId": "24", "madeForKids": true, "privacy": "public" } } } }
```

- `CreatePostInput`: `channelId!`, `mode!` (`addToQueue | customScheduled | shareNext | shareNow`), `schedulingType!`
  (`automatic | notification`), `dueAt` (ISO 8601 UTC), `text`, `assets` (ordered; each entry exactly one of
  `image | video | document`), `metadata` (one key per network), `saveToDraft`, `tagIds`, `needsApproval` [V].
- One channel per call. Result union: `PostActionSuccess | NotFoundError | UnauthorizedError | UnexpectedError |
  RestProxyError | LimitReachedError | InvalidInputError` [V].
- **Media**: public, direct, HTTPS URL; there is no upload endpoint. "Buffer fetches the media when the post goes
  out", so signed/expiring URLs (S3 pre-signed) fail later, and bot-protected hosts may block the fetcher; Buffer
  suggests Cloudinary or a public Cloudflare R2 bucket [V]. An unfetchable URL can already fail at create time
  with a `MutationError` [V]. `VideoAssetInput.thumbnailUrl` is rejected; use `metadata.thumbnailOffset` (ms) [V].
- Experimental fan-out: `createContentItem(input:{organizationId, title, posts:[CreatePostInput]})` creates all
  channel posts in one request, all-or-nothing; "can change without a deprecation period" [V].

| Platform | `metadata` input (live schema) [V] | Notes [V] |
|---|---|---|
| YouTube Shorts | `youtube { title, categoryId, madeForKids, privacy, isAiGenerated, notifySubscribers, embeddable, license }` | `title` + `categoryId` required on create. `privacy`: `public` / `unlisted` / `private` (default public). `madeForKids` defaults to **false**. Category ids: 1 Film & Animation, 10 Music, 22 People, 23 Comedy, 24 Entertainment, 27 Education. `text` = description (5,000), title 100 chars. Shorts only; no custom thumbnail. |
| TikTok | `tiktok { isAiGenerated, title }` | `title` is for photo posts. No privacy, comment, duet or stitch fields. Caption 2,200 chars, max 5 hashtags. Cover via `thumbnailOffset`. |
| Instagram Reels | `instagram { type, shouldShareToFeed, isAiGenerated, firstComment, geolocation, link, stickerFields }` | `type: reel` and `shouldShareToFeed` are required. Cover via `thumbnailOffset` only. Caption 2,200 chars. |
| Facebook Reels | `facebook { type, firstComment, linkAttachment, annotations }` | `type: reel` required (enum `post` / `reel` / `story`). No AI flag, no title input; no first comment on Reels. |

| Media limits (help centre) [V] | Length | Size | Aspect / format |
|---|---|---|---|
| YouTube Shorts | up to 3 min | 10 GB | 9:16 or 1:1; mov, mp4, mpg, avi, webm |
| TikTok | 3 s - 10 min | 1 GB | min 360 px per side; MP4, MOV, WEBM; 23-60 fps |
| Instagram Reels | 5 s - 15 min | 300 MB | 4:5 to 9:16; video 25 Mbps max, audio 128 kbps max |
| Facebook Reels | 3 - 90 s | 1 GB | 9:16; MP4 |

Safe canonical file [I]: MP4 (H.264/AAC), 9:16, 5-90 s, under 300 MB, 24-60 fps. That the API enforces these composer
limits is [I]; the experimental `configuration` query exposes per-channel `FileSizeRule` / `DurationRule` / `FormatRule` [V].

**2.3 Publish now.** New post: same mutation with `"mode": "shareNow"`, no `dueAt` [V]. Existing scheduled post:
`editPost(input:{ id, mode: shareNow })`; the schema says a non-null `mode` "applies that mode" and
`Post.allowedActions` lists `publishPostNow` [I: valid against the schema, no docs example]. Publishing is
asynchronous (`status: sending`), so expect the permalink only on a later poll [I].

**2.4 Cancel** [V]. Check `allowedActions` contains `deletePost` first. Soft cancel: `editPost(input:{ id, saveToDraft: true })` [V].
```graphql
mutation { deletePost(input: { id: "<postId>" }) { ... on DeletePostSuccess { id } ... on MutationError { message } } }
```

`PostNotDeletableError` says publishing or published posts "can no longer be deleted", while the intro guide says
"Remove scheduled or sent posts" [V]; behaviour for sent posts, and any effect on the network, is [?].

**2.5 Status** [V]
```graphql
query { post(input: { id: "<postId>" }) { id status schedulingType dueAt sentAt externalLink channelId channelService
  error { message rawError supportUrl } allowedActions } }
query Poll($org: OrganizationId!, $since: DateTime!) { posts(first: 50, input: { organizationId: $org,
    filter: { status: [sent, error, sending], dueAt: { start: $since } }, sort: [{ field: dueAt, direction: asc }] }) {
  edges { node { id status sentAt externalLink channelId error { message } } } pageInfo { hasNextPage endCursor } } }
```

`PostStatus`: `draft | needs_approval | scheduled | sending | sent | error`. Filters: `channelIds`, `status`, `dueAt` and
`createdAt` `{start,end}`, `tagIds`, `postTypes`; there is no filter by a list of post ids [V].

**2.6 Native post id / permalink**
- `Post.externalLink` = "The external URL of the post at the destination service" [V]. No webhook: "There are no
  webhooks, so keeping data in sync means polling" [V].
- No native-id field on `Post` (all 28 fields checked by introspection) [V].
- Deriving the id [I, needs one live test per platform]: YouTube `/shorts/<videoId>`, TikTok `/video/<id>`, Facebook
  `/reel/<id>`; Instagram permalinks carry a shortcode, so resolve the media id via the Graph API
  (`/{ig-user-id}/media?fields=id,permalink`). The URL formats Buffer actually returns are [?].

## 3. Idempotency and safe retries
- `createPost` takes no idempotency key or client id. Buffer's guide: a `create_post` whose response was lost "will
  publish a second post if the agent simply tries again" [V].
- Buffer's recommended recovery [V]: on timeout / dropped connection, list posts for that channel in a narrow
  `createdAt` window and check whether it landed; retry only when the error proves nothing was written.
- Hatch pattern [I]: persist the publication row (`submitting`, request time) before the call; give each
  (video, channel) a unique `dueAt` and match on `channelId + dueAt + text` when reconciling; never auto-retry a
  `shareNow`; serialize submits per channel.
- The only idempotent write is `createContentItemDraft.correlationId` (client UUID; a retry "returns the first
  content item in its current state") [V]. Draft, then `promoteContentItemDraftToPosts` (fails with
  `ContentItemStateError` once promoted) would give at-most-once creation [I]; both are Experimental [V].

## 4. Automatic publishing per platform
`schedulingType: automatic` = "Buffer's publishing workers send the post, with nobody having to act"; `notification` =
phone reminder [V]. Read `schedulingType` back from the mutation and assert `automatic`.

| Platform | Automatic | Requirements / restrictions [V] |
|---|---|---|
| YouTube Shorts | Yes | Connect as channel **Owner** (not Manager). Unverified YouTube accounts: 10 posts/24 h, no links in descriptions. No YouTube audio library. New channels may need a day or two before connecting. |
| TikTok | Yes | Becomes notification if the channel's "Enable Notifications by default" is on, the video exceeds 10 min, or music/polls are wanted. No account-type requirement stated. Buffer-sent posts cannot use TikTok "Promote". |
| Instagram Reels | Yes | **Professional (Business or Creator) account required**; Personal accounts are notification-only. A Facebook Page link is **optional** (only for locations and liking mentions). No trending music, product tags, collab posts. |
| Facebook Reels | Yes | **Pages only**: profiles cannot connect, Groups are notification-only. Connecting user needs Full control of the Page. Reel also appears in the feed. |

- One API guide's "Supported platforms" list omits TikTok, but the help centre, the schema (`TikTokPostMetadataInput`,
  added 2026-01-28) and the roadmap ("TikTok support for our API": Released) all include it [V].
- Risk [?]: a suggestion-board post claims Buffer's TikTok path uses TikTok `PULL_FROM_URL`, which needs the media
  domain verified with TikTok. Not confirmed by Buffer's docs; test TikTok early with the real media host.

## 5. Pricing and posting limits
| Monthly billing, per channel [V] | Essentials | Team |
|---|---|---|
| Channels 1-10 / 11-25 / 26-50 | $6 / $4 / $3 | $12 / $4 / $3 |

- **12 channels on Essentials: $68/mo (10 x $6 + 2 x $4), about $57/mo billed yearly** [I: arithmetic; Buffer's own
  examples 10 = $60 and 25 = $120 confirm the tiers are graduated]. Team would be $128/mo.
- Essentials: unlimited scheduled posts ("fair use"), 1 user, API included. Team adds users, approvals and 500
  req/day [V]. Essentials is enough for Hatch with batched polling [I].
- **Free**: max **3 channels**, 10 scheduled posts per channel at a time, API included [V]. So a 4-channel test does
  not fit Free: test 3 platforms on Free, use the 14-day Essentials trial, or pay 4 x $6 = $24 for a month. Whether
  the trial needs a card is [?].
- Buffer's posting caps per channel per rolling 24 h [V]: Facebook 35, Instagram 50 (posts + reels + stories),
  TikTok 25, YouTube Shorts 10 if unverified, otherwise unlimited. Hatch needs 6. Live check:
  `dailyPostingLimits(input:{channelIds, date}) { channelId limit sent scheduled isAtLimit }` [V].

## 6. Analytics through Buffer
- `Post.metrics [{ type name value unit description }]` + `Post.metricsUpdatedAt`; rollups via
  `aggregatedPostMetrics(input:{organizationId, startDateTime, endDateTime, channelIds})`, window max 365 days [V].
- Needs a personal key with `insightsRead`. Refreshed once a day (new posts can take ~24 h); an absent metric means
  "not reported", not zero [V]. Types: `views, impressions, reach, reactions, comments, shares, reposts, saves, follows,
  clicks, engagementRate, likes` (Facebook), `totalTimeWatched, averageTimeWatched` (TikTok, Instagram Reels) [V].
- Help centre: "experimental ... we don't recommend relying on it for reporting or production tools" [V]. It also says
  analytics are unavailable for Instagram Reels and that Facebook Reels sit outside Insights, which conflicts with the
  enum notes; real per-platform coverage is [?]. Verdict [I]: a coarse daily fallback, not Hatch's primary source.

## 7. Alternatives (for the human to weigh; Buffer stays the default)
- **Zernio** (formerly Late / getlate.dev, which 301-redirects there). API-first REST. 2 accounts free, accounts 3-10
  $6/mo, 11-100 $3/mo: 12 accounts = $54/mo [V prices, I total]. `Idempotency-Key` header (24 h), `platformPostUrl` on
  get/list, presigned upload to 5 GB, webhooks, retry/unpublish, `tiktokSettings` [V]. Best fit on paper; young product.
- **Ayrshare**. Mature REST API billed per "social profile" (one brand across all networks): Premium $149/mo (1),
  Launch $299/mo (up to 10; 28-day trial), Business from $599/mo [V]; 3 brands = $299/mo. `POST /post` takes `mediaUrls`,
  `scheduleDate`, `youTubeOptions.madeForKids`, `idempotencyKey`; returns `postIds[]` with native `id` + `postUrl` [I].
- **Upload-Post**. REST `POST https://api.upload-post.com/api/upload` (multipart), Python SDK. "Profile" = one account
  per platform (3 brands = 3). Free: 2 profiles, 10 uploads/mo, **no TikTok**; Basic $24/mo (5); Professional $50/mo
  (25) [V]. Status by `request_id` returns `platform_post_id` + `post_url` [V]. Small vendor; idempotency key [?].
- **Postiz**. Open source (self-host) or cloud: $29/mo (5 channels), $39 (10), $49 (30), $99 (100), API + webhooks on
  every plan; 12 channels = $49/mo [V]. Public API capped at 90 requests/hour, has an upload endpoint [V]. Native
  id/URL in responses [?]. Self-hosting means owning each platform's developer app and review [I].
- **Publer**. API only on Business/Enterprise, Bearer token, 100 requests per 2 min [I]. Pricing renders client-side;
  secondary sources say about $10/mo for the first account plus about $7 per extra on Business [?].
- **Metricool**. Priced per brand; "Metricool API (Zapier, Make)" starts at Advanced, up to 15 brands for $53/mo [V].
  Analytics-first; publishing API shape, native ids and idempotency not checked [?].

## 8. Recommendation: minimum path to one MP4 on four platforms for one brand
Human actions, in order:
1. Create a Buffer account as organization **owner** and verify the email.
2. Prepare the brand's accounts: YouTube channel (signed in as Owner; verify the YouTube account), TikTok account,
   Instagram **Business/Creator** account, Facebook **Page** with Full control.
3. Plan: start the 14-day Essentials trial or pay for 4 channels ($24/mo); Free holds only 3. Production: Essentials
   with 12 channels (~$68/mo).
4. Connect the 4 channels at `account.buffer.com/channels` on desktop, logged in to the exact account each time. In
   the TikTok, Instagram and YouTube channel settings turn **off** "Enable Notifications by default".
5. Create the key (Settings -> API -> Personal Access -> + New Key): `postsRead`, `postsWrite`, `accountRead`,
   `insightsRead`; expiry 1 year; set a rotation reminder.
6. Provide a permanent public HTTPS media host (for example a public R2 bucket / custom domain; no signed URLs, no
   bot protection) and keep each file until its post is `sent`.

The adapter then needs: `BUFFER_API_KEY` (expires within a year), the media base URL, the `organizationId`, and per
brand four `channelId`s with their `service` (discoverable via 2.1, stored as config). Per publication it persists the
Buffer `post.id`, `status`, `dueAt`, `sentAt`, `externalLink`, the derived native id and `error.message`.

First live test [I]: (a) run 2.1, assert `isDisconnected=false` and `defaultToReminders=false`; (b) `createPost` with
`saveToDraft: true` per channel to validate payloads without publishing; (c) one `customScheduled` post per channel 10
min out; (d) poll, record each `externalLink` format, confirm `schedulingType` stayed `automatic`; (e) check the TikTok
post's privacy/comment settings by hand.

## 9. Verified vs inferred vs unknown
**Verified** (primary source read 2026-10-01): endpoint, auth, key rules, plan availability, rate limits, legacy REST
retirement date; every type, field and enum name in section 2 (docs reference, live introspection, static validation);
media-by-URL rule; no webhooks; no idempotency key; `externalLink`; automatic vs notification semantics; account-type
requirements; posting caps; media specs; Buffer prices and Free limits; metrics fields and their "experimental"
caveat; prices of Zernio, Upload-Post, Postiz, Ayrshare, Metricool. The schema has a kids flag for YouTube only.

**Inferred**: 12-channel cost; request budget; `editPost(mode: shareNow)` on an existing post; no permalink in the
mutation response; deriving native ids from permalinks; API media limits equal composer limits; draft+promote as an
at-most-once path; canonical MP4 profile; Ayrshare and Publer API details (read via a summarising fetcher).

**Unknown** (needs a live test or a question to Buffer): whether the API is formally GA; `externalLink` format per
platform and its delay after `sent`; the TikTok privacy / comment / duet / stitch values Buffer applies; whether TikTok
needs the media domain verified; whether media is fetched only at publish time; what `deletePost` does to a sent
post; whether the trial needs a card; metric coverage for Reels; Publer pricing; a user report (undated) that Buffer
blocks duplicate content via the API.

## Sources
- Buffer developer docs, raw markdown under `https://developers.buffer.com/`: `llms.txt`, `reference.md`, `changelog.md`,
  `roadmap.html`, `guides/{authentication, getting-started, api-limits, posts-and-scheduling, hosting-media,
  content-items, error-handling, efficient-api-usage, rest-migration, post-metrics, character-limits, cli}.md`,
  `examples/{create-video-post, create-scheduled-post, get-channels, get-posts-with-metrics}.md`.
- Live: `POST https://api.buffer.com` introspection, no auth.
- Buffer site: https://buffer.com/pricing.md , https://buffer.com/developers/legacy-api , https://buffer.com/api
- Buffer help centre, `https://support.buffer.com/en-us/articles/` + `using-buffers-api-GtIYIQilz5`,
  `how-to-create-your-buffer-api-key-ShIgYVwM6j`, `troubleshooting-buffers-api-VgBuQXUCDI`, `daily-posting-limits-kJ0JtpsdvD`,
  `using-tiktok-with-buffer-oGEroY9Of2`, `using-youtube-shorts-with-buffer-Jl8iR6jIck`, `using-instagram-with-buffer-YSjg2dXFV8`,
  `connecting-your-instagram-account-to-buffer-n9Ad6veXsu`, `using-facebook-with-buffer-jY1AM9fz2G`,
  `supported-channels-LM3P7Y4zsp`, `connecting-your-channels-to-buffer-HvWLgAJvL9`.
- User reports, unverified, `https://suggestions.buffer.com/p/` + `binary-file-upload-file_upload-support-for-tiktok-in-mcp-api`,
  `duplicate-post-detection-blocks-new-post-creation-via-api-after-original-drafts-deleted`.
- Alternatives: https://zernio.com/pricing , https://docs.zernio.com/posts/create-post.mdx , https://www.ayrshare.com/pricing/ ,
  https://www.ayrshare.com/docs/apis/post/post , https://www.upload-post.com/llms-full.txt , https://docs.upload-post.com/llms-full.txt ,
  https://postiz.com/pricing , https://docs.postiz.com/public-api/introduction , https://publer.com/docs , https://publer.com/plans
  (prices need JS; secondary: https://socialrails.com/blog/publer-pricing ), https://metricool.com/pricing/
