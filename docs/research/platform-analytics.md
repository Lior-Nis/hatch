# Platform analytics ingestion: YouTube, TikTok, Instagram, Facebook (research, 2026-10-01)

Scope: what Hatch's analytics adapters can read from the platforms' official APIs for the operator's own short videos, how fresh it is, what access is
needed, and what kids/AI policy constrains the product. Read-only research: no accounts created, nothing logged in, nothing posted. Tags: **[V]**
verified in a primary source fetched today,
**[V*]** verified on a Meta page read through an HTML-to-text summariser (Meta blocks raw fetches; re-check wording at implementation), **[I]**
inferred, **[?]** unknown. Sources are numbered in section 9.

## 0. Findings that change the plan

1. **The 1h and 6h snapshots can only hold public counters, not watch-time metrics.** YouTube Analytics lags 48-72h with day granularity [V]; TikTok's
   rich metrics lag 24-48h [V]; Instagram insights "can be delayed up to 48 hours" [V*]; Facebook insights mostly update every 24h [V*]. Design
   1h/6h/24h as "counters only" and treat 72h/7d/30d as the first snapshots where watch time, completion and follows are trustworthy.
2. **No platform gives all nine normalized signals.** Impressions are effectively gone for short video on all four (YouTube: not for the Shorts feed;
   IG `impressions` and FB `post_impressions_unique` deprecated; TikTok: never had it). True completion rate exists only on TikTok (Business API).
   Follows-per-post is missing on Instagram Reels. The normalized schema needs nullable fields plus a per-platform "definition" tag.
3. **"Made for kids" on YouTube zeroes two signals by design**: comments are disabled and "Save to playlist" is disabled [V], so `comments` and
   `saves` are structurally 0 there, not "bad performance".
4. **TikTok is the long-lead item.** Watch time, completion, reach, new followers and favorites need the TikTok API for Business "Accounts API"
   (developer review ~3 business days + an access application form required since 2026-03-20 + app review 2-3 business days) [V]. The Display API
   works immediately in sandbox for up to 10 of your own accounts, but only returns views/likes/comments/shares [V].
5. **Meta and Google need no app review for own-account read access**: Meta Standard Access covers users with a role on the app [V*]; a Google OAuth
   app used by <100 users may stay unverified, but must be switched to "In production" or refresh tokens die after 7 days [V].
6. **View definitions moved under us**: YouTube Shorts `views` = play starts since 2025-03-31 (old definition is `engagedViews`), and since
   2026-08-24/27 all formats count a view when playback begins [V]. Instagram replaced `plays`/`impressions` with `views` in 2025 [V*]. Store the
   metric name and API version with every raw payload.
7. **Buffer exposes the native post URL** (`Post.externalLink`) through its GraphQL API [V], so id mapping is a URL parse with a list-and-match
   fallback. Buffer's own metrics refresh only daily [V], so they cannot replace the platform adapters for 1h/6h.

## 1. Normalized signal availability (summary)

| Signal | YouTube Shorts | TikTok | Instagram Reels | Facebook Reels |
|---|---|---|---|---|
| views | yes (2 APIs) | yes | yes | yes (plays) |
| impressions | no (Shorts feed) | no (`reach` only, Business API) | no (deprecated; `reach`) | no (deprecated 2026) |
| avg watch time | yes, lagged | Business API only | yes | yes |
| avg % viewed | yes, lagged | derive: avg time / duration [I] | derive [I] | derive [I] |
| completion rate | no (proxy: retention curve end) | Business API only | no (`reels_skip_rate` is a 3s skip rate) | derive from retention graph [I] |
| retention curve | yes (per-video report) | Business API only | no | yes |
| likes | yes | yes | yes | yes |
| comments | 0 by design if made-for-kids | yes | yes | yes |
| shares | yes, lagged | yes | yes | yes |
| saves | proxy, 0 if made-for-kids | Business API only (`favorites`) | yes | no [I] |
| follows gained | yes, lagged | Business API only | no for Reels | yes |

## 2. (A) YouTube: Data API v3 + YouTube Analytics API

### 2.1 Metrics
| API field / metric | Normalized signal | Notes |
|---|---|---|
| Data API `videos.list` `statistics.viewCount` | views (fast) | "Starting August 24, 2026, for all video formats ... viewCount will be updated to count views the moment a video begins to play (includes autoplay, hold the pointer over, and click/tap to play)" [V][1] |
| `statistics.likeCount`, `statistics.commentCount` | likes, comments (fast) | `favoriteCount` is deprecated and always 0; `dislikeCount` owner-only [V][1] |
| Analytics `views` | views | For Shorts: "number of times a Short starts to play or replay", since 2025-03-31 (targeted queries from 2025-04-30) [V][2] |
| Analytics `engagedViews` | views (engaged) | Pre-2025 methodology: "viewed past the first frame, or the user clicks/taps to play"; this is what YPP/monetization uses [V][2][3] |
| `estimatedMinutesWatched` | total watch time | [V][3] |
| `averageViewDuration` (seconds) | avg watch time | [V][3] |
| `averageViewPercentage` | avg % viewed | [V][3]; behaviour for looped Shorts (can exceed 100?) [?] |
| `likes`, `comments`, `shares` | likes, comments, shares | `shares` exists only in Analytics, not in the Data API [V][3][4] |
| `videosAddedToPlaylists` | saves (proxy) | Not a true "save"; and Save-to-playlist is disabled on made-for-kids videos [V][3][10] |
| `subscribersGained`, `subscribersLost` | follows gained | Per-video via `dimensions=video` or `filters=video==ID` [V][4] |
| `audienceWatchRatio`, `relativeRetentionPerformance` by `elapsedVideoTimeRatio` | retention curve | Single video ID per request [V][4]. Completion proxy = `audienceWatchRatio` at ratio 1.0 [I]. Whether Shorts return data here [?] |
| Reporting API `video_thumbnail_impressions`, `video_thumbnail_impressions_ctr` | impressions (not usable) | Bulk reports `channel_reach_basic_a1`, added 2026-01-15 [V][2][5]. A thumbnail impression is "displayed without interaction or autoplay" [V][2], so the Shorts feed is not covered [I]. Not in `reports.query` [V][4] |

Not available by API: Shorts-feed impressions, "viewed vs swiped away", completion rate as a metric [V by absence from [3][4]]. Use
`dimensions=creatorContentType` (`SHORTS`) to confirm a video is classified as a Short [V][6].

### 2.2 Freshness
- Analytics: "Data processing typically introduces a latency of 48 to 72 hours ... the `endDate` of your report only includes data up until the last
  day for which all requested metrics are fully processed"; Google's own advice for current counts is `videos.list` (doc section added 2026-09-09)
  [V][7][2]. Time dimension is `day`/`month` only, no hourly [V][4].
- Reporting API (bulk): first report available within 48h of creating the job; daily files, backfills replace data [V][5].
- Data API counters: documented as the "real-time" option [V][7]; exact refresh cadence [?].
- So: 1h/6h/24h = `viewCount`, `likeCount`, `commentCount` only. 72h = first partial Analytics row. 7d/30d = full.

### 2.3 Auth, access, quota
- Scopes: `https://www.googleapis.com/auth/yt-analytics.readonly` (Analytics) and `youtube.readonly` (Data API) [V][8].
  `yt-analytics-monetary.readonly` returns HTTP 403 for non-YPP channels [V][2].
- `ids=channel==MINE` or `channel==CHANNEL_ID` of the authenticated user's channel [V][8]. One Cloud project/OAuth client can hold a refresh token per
  channel (limit 100 refresh tokens per Google account per client) [V][9]; with Brand Accounts the channel is chosen on the consent screen [I].
- Verification: apps for personal use with fewer than 100 users can stay unverified (click through the "unverified app" screen) [V][11]. But in
  publishing status **Testing**, "authorizations by a test user will expire seven days from the time of consent", refresh token included [V][9][12].
  Set status to "In production" without submitting for verification [I: follows from [11][12]].
- Service accounts cannot own a YouTube channel; use the installed-app/web OAuth flow once per channel [I].
- Quota: 10,000 units/day default; `videos.list`, `playlistItems.list`, `channels.list` cost 1 unit each; `search.list` and `videos.insert` have
  separate 100/day buckets [V][13]. 144 polls/day batched (up to 50 ids per `videos.list` call [I]) is well under 1% of quota. Analytics API quota
  numbers [?] (visible in Cloud console).
- Compliance audit is needed only to raise quota above default (and, separately, for public uploads from new API projects) [V][13]; not needed for
  reading [I].

## 3. (B) TikTok: Display API vs API for Business (Accounts API)

Research API is for researchers/public data and does not apply (scopes `research.*`) [V][14].

### 3.1 Metrics
| API field | Normalized signal | Notes |
|---|---|---|
| Display `/v2/video/list/`, `/v2/video/query/`: `view_count`, `like_count`, `comment_count`, `share_count` | views, likes, comments, shares | Scope `video.list`. Also `id`, `create_time`, `share_url`, `video_description` (max 150), `title` (max 150), `duration`, `is_aigc` [V][15] |
| Display `/v2/user/info/`: `follower_count`, `likes_count`, `video_count` | account-level follows (delta only) | Scope `user.info.stats` [V][16] |
| Business `GET /open_api/v1.3/business/video/list/`: `video_views`, `likes`, `comments`, `shares` | views, likes, comments, shares | Scope `video.list`. `video_views` = playback duration > 0, first playback in an impression session; organic + paid combined [V][17] |
| Business `favorites` | saves | `video.list` scope [V][17] |
| Business `reach` | (unique viewers; closest to impressions) | [V][17] |
| Business `total_time_watched`, `average_time_watched` | watch time, avg watch time | Scope `video.insights` [V][17] |
| Business `full_video_watched_rate` | completion rate | "percentage of viewers who finish watching" [V][17] |
| Business `new_followers` | follows gained | Scope `video.insights` [V][17] |
| Business `video_view_retention` (second, percentage), `engagement_likes`, `impression_sources`, `audience_*` | retention curve, traffic sources | [V][17] |

Not available: impressions (feed exposures) on either API; anything beyond the four counters on the Display API.

### 3.2 Freshness
- Business API: `video_views`, `likes`, `comments`, `shares`, `reach`, watch-time and completion fields have **"24-48 hours (UTC Time)"** latency;
  ids/caption/`create_time` have none [V][18]. Metrics are lifetime aggregates; "post data will stop updating 365 days after the post is published"
  [V][17].
- Watch-time/reach fields go missing if the video "has not been active ... for more than 7 days" [V][17]: the 30d snapshot may come back empty for
  dead posts. Analytics must be turned on once in the mobile app [V][17].
- Display API counter latency is not documented [?]; assumed near-live [I]. It is the only source for 1h/6h.

### 3.3 Auth, access, limits
- Display API (developers.tiktok.com): Login Kit OAuth; access token 24h, refresh token 365 days [V][19]. **Sandbox** needs no app review, allows up
  to 10 target accounts you own, up to 5 sandboxes [V][20]. Production requires app review with a public website, privacy policy/ToS and a demo video;
  "Apps must not be for private or personal use" [V][21], so a single-operator internal tool should plan to stay in sandbox [I]. Sandbox longevity and
  review turnaround [?]. Rate limit 600 requests/minute per endpoint; `/v2/video/query/` takes 20 ids [V][22][15].
- Business API (business-api.tiktok.com): register as developer ("review result in three business days"), since 2026-03-20 submit the Accounts API
  Access Application Form before requesting the "TikTok Accounts" scope, then app review "2 to 3 business days"; up to 5 apps per developer
  [V][23][24][17]. Works for Business and Personal accounts [V][24]. Token: `expires_in` 86400, refresh token valid one year, `open_id` is the
  `business_id` [V][25]. Default "Basic" limits: 10 QPS, 600 QPM, 864,000/day [V][26]. Permitted use includes "analyzing TikTok profile and post
  insights" for owned accounts [V][24].
- One app covers all 3 accounts on either API (one OAuth grant and token set per account) [I from [20][25]]. Whether the 365-day refresh token is
  extended on use, or needs yearly manual re-consent [?].

## 4. (C) Instagram Reels: Instagram Platform (Graph API)

### 4.1 Metrics (`GET /{ig-media-id}/insights?metric=...`, all `lifetime`)
| Metric / field | Normalized signal | Notes |
|---|---|---|
| `views` | views | "Total number of times IG Media has been played on Instagram"; introduced 2025-01-21 [V*][27][28] |
| `reach` | (unique accounts; closest to impressions) | Estimated [V*][27] |
| `ig_reels_avg_watch_time` | avg watch time | Reels only; unit milliseconds [I] |
| `ig_reels_video_view_total_time` | total watch time | Includes replays [V*][27] |
| `reels_skip_rate` | (inverse hook rate, not completion) | "Percentage of views from people who skipped during the first 3 seconds"; added 2025-12-03 [V*][27][28] |
| `likes`, `comments`, `shares`, `saved`, `reposts`, `total_interactions` | likes, comments, shares, saves | Organic only; ads excluded [V*][27] |
| `crossposted_views`, `facebook_views`, `total_views`, `total_likes`, `total_comments` | cross-surface totals | 2025-12 and 2026-04 additions [V*][27][28] |
| Media fields `like_count`, `comments_count`, `view_count`, `saved_count`, `shares_count`, `reposts_count` | fast counters | On the IG Media node, no insights call; `saved_count`/`shares_count`/`reposts_count` added 2026-04-22 [V*][28][29] |

Not available for Reels: `follows`, `profile_visits`, `profile_activity` (FEED and STORY only) [V*][27]; impressions (`impressions` deprecated for
media created after 2024-07-02; `plays`, `clips_replays_count`, `ig_reels_aggregated_all_plays_count` deprecated for all versions 2025-04-21)
[V*][27][28]; completion rate; retention curve. Follows gained must be derived from account-level follower count deltas [I].

### 4.2 Freshness
"Data used to calculate metrics can be delayed up to 48 hours"; unavailable data returns an empty data set rather than 0; metrics stored up to 2 years
[V*][27]. Media-node counters assumed near-live [I]. Insights webhooks are not supported with Instagram Login [V*][27], so polling is the only option.

### 4.3 Auth, access, limits
- Account must be an Instagram professional (business or creator) account; with Instagram Login no Facebook Page is required [V*][30].
- Instagram Login: scopes `instagram_business_basic` + `instagram_business_manage_insights`, host `graph.instagram.com` [V*][27][31]. Facebook Login
  alternative: `instagram_basic`, `instagram_manage_insights`, `pages_read_engagement`, `pages_show_list`, host `graph.facebook.com`, IG account
  linked to a Page [V*][27][32].
- Standard Access: "can only be requested from app users who have a role on the requesting app"; no App Review; Business Verification only for
  Advanced Access [V*][33]. Advanced Access is needed only for accounts "you don't own or manage" [V*][31]. Whether each IG account must also be added
  as an app tester under Instagram Login [I: yes].
- Tokens (Instagram Login): short-lived 1h, long-lived 60 days, refresh via `GET graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token`
  when the token is at least 24h old and unexpired; not refreshed for 60 days = dead, re-login needed [V*][31][34]. Schedule a weekly refresh job.
- Rate limit: "Calls within 24 hours = 4800 * Number of Impressions" per account [V*][31][35]. Floor for a new account with near-zero impressions [?];
  batch metrics into one `/insights` call per media to stay safe.
- One Meta app covers all 3 IG accounts and all 3 Pages [I].

## 5. (D) Facebook Reels: Graph API video insights

### 5.1 Metrics (`GET /{video-id}/video_insights`, Page access token, all `lifetime`)
| Metric | Normalized signal | Notes |
|---|---|---|
| `blue_reels_play_count` | views | Plays after an impression is counted; third-party docs: >= 1 ms, excludes replays [V*][36] |
| `fb_reels_total_plays`, `fb_reels_replay_count` | views incl. replays | [V*][36] |
| `post_video_avg_time_watched` (ms) | avg watch time | [V*][36] |
| `post_video_view_time` (ms) | total watch time | Includes replays [V*][36] |
| `post_video_retention_graph` | retention curve, completion (last bucket [I]) | "percentage of times your reel was played at various timestamp segments" [V*][36] |
| `post_video_likes_by_reaction_type` | likes | [V*][36] |
| `post_video_social_actions` | comments, shares | One object with both [V*][36] |
| `post_video_followers` | follows gained | "The number of follows for your reel" [V*][36] |
| `post_impressions_unique` | (reach) **deprecated** | Listed for removal in v25.0 changelog (released 2026-02-18); Page Insights doc: deprecated "by June 15, 2026 ... for all API versions", invalid-metric error afterwards [V*][37][38] |

Not available: impressions/reach after the 2026 deprecation (replacement for Reels [?]; `post_media_view` / `post_total_media_view_unique` exist on
Page posts but the Page Insights doc says "Interactions on Reels are not included") [V*][38]; saves [I]. The classic `total_video_*` metrics (3s
views, 97% complete views) are documented for videos, not Reels [V*][36]; whether they return data for a Reel [?].

### 5.2 Freshness
Page Insights: "Most metrics will update once every 24 hours"; only the last two years available; **"Page Insights data is only available on Pages
with 100 or more likes"** [V*][38]. Whether the 100-likes gate and the 24h cadence apply to `/video_insights` on Reels [?]: test with the first brand
before relying on FB data.

### 5.3 Auth, access, limits
- Permissions: `pages_show_list`, `pages_read_engagement`, `read_insights`; Page access token from a person who can perform the ANALYZE task
  [V*][32][39]. Standard Access as in 4.3 (operator is app admin and Page admin) [V*][33].
- Tokens: long-lived user token ~60 days; a Page token derived from it "do[es] not have an expiration date" [V*][40].
- Rate limit with Page tokens: "Calls within 24 hours = 4800 * Number of Engaged Users" per Page; user/app tokens: 200 calls/hour per user [V*][35].
  Monitor `X-Business-Use-Case-Usage`.
- Publishing cap (relevant to schedule, not analytics): 30 API-published Reels per Page per 24h; Reel 3-90 s, 9:16 [V*][41].

## 6. Mapping a Buffer post to the native id

- Buffer GraphQL API (`https://api.buffer.com`, Bearer personal API key): `Post.externalLink` = "The external URL of the post at the destination
  service"; also `sentAt`, `channelId`, `channelService`, `status` [V][42]. Query `posts(input:{filter:{status:[sent], channelIds:[...]}})` once per
  poll cycle, not per post.
- Buffer limits per key: 100 requests/15 min; 250/24h on Free and Essentials (500 Team); 3,000/30 days on Free (7,500 Essentials, 15,000 Team)
  [V][43]. Budget roughly one paginated query per hour.
- Whether `externalLink` is populated for all four video services, and how soon after `sentAt` [?].

| Platform | Parse from `externalLink` [I for URL shapes] | Official fallback: list and match |
|---|---|---|
| YouTube | `youtube.com/shorts/{videoId}` or `watch?v={videoId}` | `channels.list(mine=true, part=contentDetails)` -> uploads playlist -> `playlistItems.list` (1 unit); match `publishedAt` + title [V][13] |
| TikTok | `tiktok.com/@{user}/video/{id}`; `id` = Display `id` = Business `item_id` [I] | `/v2/video/list/` (sorted by `create_time` desc, 20/page) or `/business/video/list/`; match `create_time` + caption prefix (150-char cap) [V][15][17] |
| Instagram | `instagram.com/reel/{shortcode}`; shortcode is **not** the media id [V*][29] | `GET /{ig-user-id}/media?fields=id,shortcode,permalink,timestamp,caption,media_product_type`; match `shortcode` or `permalink` [V*][29] |
| Facebook | `facebook.com/reel/{video-id}` [I] | `GET /{page-id}/video_reels` returns `id`, `description`, `updated_time` (in the publishing guide; the edge reference page lists only POST) [V*][41] |

Recommendation [I]: put a short unique token (e.g. `#h7k2q`) in every caption/description at publish time so the fallback match is exact rather than
time-window + fuzzy caption, and persist native id + permalink at first match.

## 7. Children's content, AI labelling and policy risk

**YouTube**
- Every video/channel must be set "made for kids" (MFK) or not; YouTube may override the choice [V][10]. API: `status.selfDeclaredMadeForKids` (write)
  and `status.madeForKids` (effective) [V][1].
- MFK disables: autoplay on home, cards/end screens, watermarks, memberships, **comments**, donate, live chat, merchandise, **notification bell**,
  **personalized advertising**, miniplayer, Super Chat/Stickers, **Save to playlist / Watch later**; channel-level MFK also removes Posts [V][10].
  Measurement effect: comments = 0, saves = 0, no bell-driven returning traffic; subscribers still count [I].
- No documented Analytics API restriction specific to MFK videos, and Shorts can be MFK [?/I: none found].
- AI disclosure: required when AI is used "to meaningfully alter or generate photorealistic content"; not required for "non-realistic content" such as
  "a fully animated video", though "AI generated music" is in the must-disclose examples [V][44]. API flag: `status.containsSyntheticMedia` [V][1].
  Stylised cartoon output is exempt [I]; AI music is not.
- Monetization: "inauthentic content" policy (renamed 2025-07-15 from "repetitious content" to cover content that is "repetitive or mass-produced")
  [V][45]. Current page lists "Generic or Repetitive Content" ("looks like it's made with a template, or ... repetitive ... after watching several
  videos in a row"), "Unsatisfying or Off-putting Content" and "AI Personas Related to Sensitive Topics" [V][45]; press dates this clarification to
  2026-07-16 [46].
- Kids quality principles decide MFK monetization: low-quality = heavily promotional, encouraging negative behaviour, deceptively educational, **"Hard
  to follow ... often the result of mass production or autogeneration"**, sensational/keyword stuffing, strange use of children's characters; a
  channel with "a strong focus on low-quality" MFK content "may be suspended from the YouTube Partner Program" [V][45][47].
- Risk read [I]: three brands each mass-producing templated AI Shorts is squarely the pattern these policies name. Mitigation is product-level
  (distinct narrative per video, clear beginning/middle/end, real educational value, varied formats), and "quality" should be a gate before
  publishing, not only a fitness signal after.

**TikTok**
- "You must be at least 13 years old to have a TikTok account"; US under-13s get a separate curated experience with no profile/commenting; suspected
  under-age accounts are removed (Community Guidelines released 2026-08-25, effective 2026-09-24) [V][48]. There is no creator-side "made for kids"
  flag [I]. The reachable audience on TikTok is therefore parents/13+, and child-performer-style or child-impersonating accounts risk bans [I].
- AIGC: "We require clear labeling when AI or editing is used to realistically depict people or scenes"; disclosure via the AIGC label or a clear
  caption/watermark/sticker; not needed for "artistic styles, like anime" or generic TTS narration [V][48]. Display API exposes the label as `is_aigc`
  [V][15]. Unoriginal content is ineligible for the For You feed [V][48].

**Meta (Instagram, Facebook)**
- "We require people to disclose, using our AI-disclosure tool, whenever they post organic content with photorealistic video or realistic-sounding
  audio that was digitally created or altered, and we may apply penalties if they fail to do so" [V*][49]. Meta also auto-applies "AI info" from
  C2PA/IPTC signals (2024 posts, older) [V*][50]. Since 2026-06-22 the Instagram publishing API accepts `is_ai_generated=true`, and IG Media has an
  `is_ai_generated` read field [V*][28][29]; whether Buffer passes it through [?].
- Minimum account age 13; no "made for kids" content designation exists for organic posts [I]. Facebook monetization penalises "unoriginal" repeated
  content (July 2025, secondary sources) [51].
- Safe default for all platforms [I]: label everything as AI-generated where a label exists (TikTok AIGC, Meta AI info), set YouTube
  `containsSyntheticMedia` whenever a clip is photorealistic or uses AI music, set MFK=true on YouTube for content aimed at 4-8s, and run brand
  accounts as adult-operated publisher accounts.

## 8. Recommendation: minimum human actions for one brand, in priority order

| # | Action (human) | Lead time | Adapter credentials afterwards |
|---|---|---|---|
| 1 | **YouTube**: Google Cloud project; enable YouTube Data API v3 + YouTube Analytics API; OAuth consent screen (External) set to **In production** (unverified is fine); create OAuth client (Desktop); run consent once as the brand channel with `youtube.readonly` + `yt-analytics.readonly` | ~1 hour, no review | `client_id`, `client_secret`, per-channel `refresh_token`, `channel_id` |
| 2 | **Meta (IG + FB together)**: Meta developer account; one Business-type app (operator = app admin); convert IG account to professional and link it to the brand's Page; grant `pages_show_list`, `pages_read_engagement`, `read_insights`, `instagram_basic`, `instagram_manage_insights` via Facebook Login; exchange for long-lived user token, then Page token | ~1-2 hours, no App Review (Standard Access) | `app_id`, `app_secret`, `page_id`, non-expiring Page access token, `ig_user_id`. (Instagram Login alternative: per-account 60-day token + weekly refresh) |
| 3 | **TikTok Display API (sandbox)**: developers.tiktok.com account; app with Login Kit + Display API; create sandbox; add the brand account as target user; authorize `user.info.basic`, `video.list`, `user.info.stats` | ~1-2 hours, no review | `client_key`, `client_secret`, per-account `open_id`, `refresh_token` (365 d), access token refreshed daily |
| 4 | **LONG LEAD: TikTok API for Business (Accounts API)**: register as developer (3 business days) -> Accounts API Access Application Form -> create app with "TikTok Accounts" scopes `video.list`, `video.insights`, `user.info.basic` (2-3 business days) -> account owner authorizes; turn on Analytics in the TikTok mobile app | ~1-2 weeks realistic [I]; approval not guaranteed | `app_id`, `secret`, per-account `open_id` (= `business_id`), `access_token` (24h) + `refresh_token` (1 year) |
| 5 | **Buffer**: personal API key (for `externalLink` mapping) | minutes | `BUFFER_API_KEY`, organization id, channel ids |

Long-lead / blocking items: (a) step 4, start it first in calendar time even though it is priority 4; (b) a new Facebook Page may need **100 likes**
before insights return data; (c) Google OAuth must not be left in Testing; (d) yearly TikTok re-consent and 60-day Meta user-token chain need an
expiry alert. Brands 2 and 3 reuse the same four apps: only the per-account consent/token step repeats (minutes each).

Polling plan that matches reality [I]: 1h/6h/24h -> YouTube `videos.list`, TikTok Display `video/query`, IG media fields (+ `/insights`), FB
`video_insights`; 72h/7d/30d -> add YouTube Analytics `reports.query` (`dimensions=video`, plus one retention query per video), TikTok Business
`video/list` with `filters.video_ids`, full IG/FB insights. Load: 24 posts/day x 6 = 144 post-polls/day, far below every documented limit.

## 9. Verified vs inferred vs unknown

**Verified (primary source text read today)**: YouTube metric names, Shorts view change, 48-72h latency, quota costs, 7-day Testing token expiry, MFK
feature list, AI disclosure rules, monetization and kids quality principles; TikTok Display fields, scopes, token lifetimes, sandbox rules, review
criteria, rate limits; TikTok Business field list, required scopes, 24-48h latency table, review timings, rate limit levels; TikTok age and AIGC
rules; Buffer `externalLink`, metrics cadence, rate limits.
**Verified via summariser [V*]** (Meta docs; names cross-checked in two independent passes plus changelogs): IG insights metric table and limitations,
IG Media fields, changelog dates, access levels, token lifetimes, rate formulas, FB Reels metric list, v25.0 deprecations, 100-likes rule, AI
disclosure wording.
**Inferred**: URL shapes of `externalLink`; units of `ig_reels_avg_watch_time`; near-live behaviour of public counters; completion proxies from
retention curves; 50-id batching on `videos.list`; sandbox being adequate long term; TikTok Business end-to-end lead time; absence of MFK-style flags
on TikTok/Meta; policy-risk reading.
**Unknown (test with the first brand)**: whether YouTube retention reports return data for Shorts and how `averageViewPercentage` treats loops; any
MFK-specific analytics gaps; YouTube Analytics API quota; TikTok Display counter latency and sandbox lifetime; TikTok refresh-token renewal after 365
days; IG rate-limit floor for low-impression accounts; whether FB `video_insights` needs 100 Page likes, its update cadence, and the Reels reach
replacement after `post_impressions_unique`; whether `total_video_*` metrics work on Reels; whether Buffer fills `externalLink` for every service and
forwards AI-label flags.

## 10. Sources (fetched 2026-10-01 unless noted)

[1] https://developers.google.com/youtube/v3/docs/videos  
[2] https://developers.google.com/youtube/analytics/revision_history (entries 2025-03-26 to 2026-09-13)  
[3] https://developers.google.com/youtube/analytics/metrics · [4] https://developers.google.com/youtube/analytics/channel_reports  
[5] https://developers.google.com/youtube/reporting/v1/reports and .../reports/channel_reports  
[6] https://developers.google.com/youtube/analytics/dimensions · [7] https://developers.google.com/youtube/analytics/data_model  
[8] https://developers.google.com/youtube/analytics/reference/reports/query · [9] https://developers.google.com/identity/protocols/oauth2  
[10] https://support.google.com/youtube/answer/9527654 · [11] https://support.google.com/cloud/answer/13464323  
[12] https://support.google.com/cloud/answer/15549945  
[13] https://developers.google.com/youtube/v3/determine_quota_cost ; https://developers.google.com/youtube/v3/getting-started ; https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits  
[14] https://developers.tiktok.com/doc/tiktok-api-scopes  
[15] https://developers.tiktok.com/doc/tiktok-api-v2-video-object ; .../tiktok-api-v2-video-list ; .../tiktok-api-v2-video-query (updated 2026-08)  
[16] https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info  
[17] https://business-api.tiktok.com/portal/docs?id=1762228421622786 (Get post data of a TikTok account, v1.3)  
[18] https://business-api.tiktok.com/portal/docs?id=1746624508278786 (Accounts Insights data latency)  
[19] https://developers.tiktok.com/doc/oauth-user-access-token-management · [20] https://developers.tiktok.com/doc/add-a-sandbox  
[21] https://developers.tiktok.com/doc/app-review-guidelines · [22] https://developers.tiktok.com/doc/tiktok-api-v2-rate-limit  
[23] https://business-api.tiktok.com/portal/docs?id=1738855176671234 ; ...?id=1738855242728450 (register, create app)  
[24] https://business-api.tiktok.com/portal/docs?id=1737944384433218 (Accounts API overview)  
[25] https://business-api.tiktok.com/portal/docs?id=1833997638479041 ; ...?id=1738083939371009 (token, authorization)  
[26] https://business-api.tiktok.com/portal/docs?id=1740029171730433 (rate limits)  
[27] https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights  
[28] https://developers.facebook.com/docs/instagram-platform/changelog  
[29] https://developers.facebook.com/docs/instagram-platform/reference/instagram-media  
[30] https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login  
[31] https://developers.facebook.com/docs/instagram-platform/overview · [32] https://developers.facebook.com/docs/permissions  
[33] https://developers.facebook.com/docs/graph-api/overview/access-levels/  
[34] https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login  
[35] https://developers.facebook.com/docs/graph-api/overview/rate-limiting/  
[36] https://developers.facebook.com/docs/graph-api/reference/video/video_insights/ ; third-party definitions: https://help.funnel.io/en/articles/8769477-facebook-reels-dimensions-and-metrics  
[37] https://developers.facebook.com/docs/graph-api/changelog/version25.0  
[38] https://developers.facebook.com/docs/graph-api/reference/insights/ ; secondary: https://support.dataslayer.ai/understanding-upcoming-removal-of-metrics-on-facebook (2026-08-05)  
[39] https://developers.facebook.com/docs/video-api/guides/insights/  
[40] https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived  
[41] https://developers.facebook.com/docs/video-api/guides/reels-publishing ; https://developers.facebook.com/docs/graph-api/reference/page/video_reels/  
[42] https://developers.buffer.com/reference.md ; https://developers.buffer.com/guides/rest-migration.html ; https://developers.buffer.com/guides/post-metrics.md  
[43] https://developers.buffer.com/guides/api-limits.md · [44] https://support.google.com/youtube/answer/14328491  
[45] https://support.google.com/youtube/answer/1311392  
[46] https://techcrunch.com/2026/07/20/youtube-clarifies-policies-around-ai-slop-and-upsetting-videos/ (secondary)  
[47] https://support.google.com/youtube/answer/10774223  
[48] https://www.tiktok.com/community-guidelines/en/youth-safety ; .../en/integrity-authenticity (2026 August version)  
[49] https://transparency.meta.com/policies/community-standards/misinformation (changelog to 2025-04-07; older)  
[50] https://about.fb.com/news/2024/04/metas-approach-to-labeling-ai-generated-content-and-manipulated-media/ (2024; older)  
[51] https://www.tubefilter.com/2025/07/15/ai-slop-unoriginal-repetitive-content-monetization-facebook-meta/ (2025; secondary, not opened: search summary only)  
