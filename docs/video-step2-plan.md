# Todd — Video, step 2: shorts grounded in what's working now

Status: proposal (2026-10-06). Builds on step 1 (`video` toolset, `media` service) on branch `video-step1-slideshows`.
Folds in the original step 2 (app demo clips via browser screencast) and the AI-generation part of step 4.

**Todd is a generic system.** Nothing below is tuned to one product: the engine works out the niche, the trends and the
formats itself from whatever the human points it at (a URL, a repo, a sentence). dropin.hockey (a local consumer site)
and Todd itself (a developer tool) are the first two **test cases**: the engine has to reach the quality bar below on
both, on its own, and keep reaching it as it changes.

## Why step 1's output was weak

The first real examples (dropin.hockey, Todd) were captioned stock photos with a slow zoom. They failed for reasons the
toolset can't rank its way out of:

- **The copy came from the product description, not from the niche.** "Want to skate at the CU Boulder rink?" is what an
  ad says. Nobody in hockey TikTok talks like that.
- **The story was a feature list, not a format.** No hook in the first second, no tension or payoff, no reason to watch
  to the end.
- **Keyword stock photos are the wrong footage.** Pro players and posed models, not 20-year-olds at a college rec rink.
  CLIP ranked the candidates fine; the candidates were the problem.
- **Stills, no voice, no sound.** A captioned slideshow reads as an ad, and short-form viewers skip ads.

What performs in these niches right now is the opposite: raw, relatable, native to the culture (beer-league and drop-in
POVs, goalie bits, "types of players" skits for hockey; real screens and receipts for app launches, "build in public").

## What the engine does

For a product and a platform (TikTok, Reels, Shorts):

1. **Niche map.** Who it's for and where they hang out: communities, hashtags, creators, the words they use (for the
   hockey test case: "beer league", "drop-in", "rec center", "goalie"), all worked out from the product inputs.
2. **Trend scan.** Pull recent short videos in those niches with their metrics, and keep the **outliers**: videos that
   did far better than their creator's usual (views ÷ the creator's median views over the last N posts). Outliers point
   at formats that work for ordinary accounts, not at celebrities.
3. **Format cards.** For each outlier: transcript, ~8 sampled frames, on-screen text, sound. A model turns them into a
   reusable card: the hook (first 1–2 s: what's shown, said and written), the beats with timings, shot types, caption
   style, pacing, sound, length, CTA, and why it works. Similar videos are merged into one format with several examples.
4. **Adapt.** Pick the 2–3 formats that fit the product. For each, write a script in that format: 3 hook variants, the
   beats, voiceover lines, on-screen text, and a shot list where every shot names its source (below). **The human picks
   or edits the script before anything is spent.**
5. **Produce.** Footage, in this order of preference:
   1. real product footage, **filmed by Todd itself**: Todd built the product, so it knows its pages, its flows and its
      local preview, and records them in the agents' browser (`short_record`: a 9:16 phone tab, scripted taps, scrolls
      and typing, every step timestamped for sync). Plus the human's own clips if they upload some;
   2. AI clips (Higgsfield) for what can't be filmed: people, places, reactions, b-roll, often started from a real
      screenshot or a generated still so the style stays consistent;
   3. stock, last.
   Voiceover from ElevenLabs with word timestamps. No generated music: a track the human supplies (ducked under the
   voice), or the platform's trending sound added when posting.
6. **Lock the voice, plan, preview, then generate.** The voiceover is rendered first and its timestamps decide every
   cut, caption and clip length; a cheap animatic is approved before any video is generated (see
   [Locking voice to picture](#locking-voice-to-picture)).
7. **Assemble.** One timeline render in the `media` service: clips cut to the voiceover's beats, word-by-word captions,
   any music ducked under the voice, 9:16, captions in the safe zone.
8. **Review.** A critic pass (a model watches sampled frames and reads the transcript against the format card: is
   there a hook in the first second, is the text readable, does it end on the payoff), then the human. Hook variants
   render as separate cuts for A/B.
9. **Later (step 4):** post through the approval gates, pull views and watch time back, and let the next scan favour
   what worked for this account.

Everything lands in the run folder, so it's visible in the Files view and survives restarts:

```
video/<slug>/research/      sources.json (videos + metrics + outlier scores), transcripts, sampled frames
video/<slug>/formats.json   format cards
video/<slug>/script.json    the chosen script: hooks, beats, VO lines, on-screen text, shot list
video/<slug>/shots/         recordings, generated clips, stills
video/<slug>/audio/         voiceover takes (+ character timestamps), a music track if the human gives one
video/<slug>/timeline-<hook>.json   the timing plan: every cut, caption and clip length, derived from the take
video/<slug>/<slug>-<hook>.mp4   one cut per hook variant
```

## Test cases and the quality bar

The scripts below were written by hand to show the bar. They are not templates: the engine must find formats like these
from live data and write scripts like these by itself, for any product. Where they mention a specific trend, that's
what the trend scan should discover, not something the code knows.

### dropin.hockey, format "every drop-in has these 5 players" (relatable roster skit)

Seen across #beerleaguebum / drop-in hockey TikTok: a roster of recognisable characters, one per quick cut, the last
one is the punchline. Here the punchline is the product.

| t | Shot (source) | Voiceover | On screen |
| --- | --- | --- | --- |
| 0–1.5 s | bench, skates being laced, rec rink (Higgsfield) | "Every drop-in at the Rec has the same five guys." | every CU drop-in has these 5 guys |
| 1.5–4 | player in full pro gear, wobbly crossover (Higgsfield) | "Full pro gear. Hasn't scored since high school." | 1. the pro-gear guy |
| 4–6.5 | goalie walking in late, everyone cheers (Higgsfield) | "The goalie. Three minutes late, still a hero." | 2. the goalie |
| 6.5–9 | group chat on a phone: "is drop-in even on today??" (generated screen) | "The guy who asks if it's even on. Every. Week." | 3. "is it on today?" |
| 9–13 | dropin.hockey week view, the club game that bumped Thursday's slot (screen recording) | "And the one who already checked. dropin.hockey." | 4. the one who checked |

~13 s, dry deadpan voice, word-synced captions. Variant hooks: "POV: you're the goalie at CU
drop-in", "rating every guy at drop-in hockey".

### Todd, format "receipts on screen" (build in public)

| t | Shot (source) | Voiceover | On screen |
| --- | --- | --- | --- |
| 0–1.5 s | Todd run card flipping to Done (screen recording) | "I typed one sentence and this thing launched my site." | 1 sentence → live site |
| 1.5–4 | typing the goal into Todd (screen recording) | "Launch a waitlist for dropin.hockey." | |
| 4–7 | three agents' live windows, sped up (screen recording) | "Three agents. Twenty minutes." | 3 agents · 20 min |
| 7–10 | the approval card: $10.44 for the domain (screen recording) | "It asked before it spent a cent." | asked before paying |
| 10–13 | the live site on a phone, then the GitHub page (screen recording) | "Open source. Runs on your computer." | link in bio |

Series variant: "Day 1 of letting an AI agent run my side project", a built-in reason to follow.

Both lean on real screen recordings, which is why browser screencast capture moves up from "step 2, later" to the core.

### Generic by design

- **No product or niche knowledge in code or prompts.** The niche map, formats and scripts are data in the run folder,
  produced from the inputs and live trend data. A format card learned in one run goes into a shared **format library**
  (DB, like `MediaAsset`), tagged by niche and platform and dated, so later runs start from what's known and refresh it.
- **Test fixtures, not hand-tuning.** `tests/video_eval/` holds the two cases as fixtures: product inputs plus a frozen
  snapshot of their trend data (so the eval is reproducible and costs nothing to re-run). An eval command runs the
  pipeline up to the script (and optionally renders) and scores the output with a rubric:
  - a hook in the first 1.5 s that would stop a scroll in that niche;
  - the niche's own language, no ad-speak;
  - the product shows up as the payoff, not the pitch;
  - every beat traceable to a format card with real examples;
  - footage preference respected (real > AI > stock);
  - captions readable and in the safe zone;
  - 7–20 s.

  Scores are tracked across versions, so a change that helps one case and hurts the other shows up. More fixtures get
  added as Todd is used on other products.
- **Platform rules as data too:** lengths, safe zones and AI-content labels per platform live in one table, not in the
  scripts.

## Locking voice to picture

Video generation is the expensive step and the hardest to redo, so everything that decides timing is settled before it,
with steps that are free or cost cents. The order is fixed, and each step's output is the contract for the next:

1. **Script** (free). Hooks, beats, one voiceover line and on-screen text per beat, one shot per beat with its source.
   Validated before anything else: an estimated length from the speaking rate (about 2.8 words/s) inside the
   platform's limits, every beat has a shot, every AI shot has a prompt. The human approves it.
2. **Voice lock.** Each hook variant is voiced as one continuous take (hook + body), so the delivery flows naturally,
   with word timestamps. **The take is the master clock**: nothing changes the voice without re-planning. Two voices:
   - **the scaffold voice, free**: Piper (local, CPU) reads the script and a local recogniser (faster-whisper) hears
     where each word landed. Caption words come from the script, only the times from what was heard, so a misheard
     word never shows; times are good to about a tenth of a second, plenty for a preview;
   - **the final voice, cents**: ElevenLabs, after the human approves the scaffold. Its character timestamps come from
     the model that made the audio, so there is no drift at all. The plan is redone from this take before anything
     is generated, and if the generation price moves by more than 20% from the scaffold's estimate, the human is asked
     again.
3. **Timing plan** (free, deterministic): `timeline-<hook>.json`, computed from the timestamps.
   - Cuts sit on frame boundaries, 2 frames before the first word of each beat, so the picture changes just ahead of
     the voice, as editors cut. The hook starts at frame 0 with the take's leading silence trimmed; the video ends
     `end_hold_s` after the last word.
   - A beat can ask for extra time (`hold_s`, e.g. for a visual payoff). The voice is split in the silence between
     the two lines and the rest moves later; the take is never stretched.
   - A shot can pin a moment to a word (`sync: {word, at_s}`, e.g. the tap on Approve lands on "cent"). The clip's
     in-point is chosen so that moment lands on the word. Screen recordings log when each action happened, so their sync
     points are exact.
   - Captions come from the same timestamps: 1–3 words per page, never across a beat, the spoken word highlighted.
   - Every shot gets a slot length. Every AI shot gets a generation spec: the shortest length the model supports that
     covers its longest slot across hook variants plus a 0.25 s handle, and the price. Their sum is the exact cost.
   - A **sync report** lists each beat (line, slot, how the shot fits: trim, speed, freeze) and flags anything outside
     the limits: a recording sped up more than 2×, a clip slowed below 0.85×, a beat under 0.7 s, words faster than a
     caption can be read.
4. **Scaffold / animatic** (free), **reviewed by the human** (`short_review`). The whole cut rendered from the plan
   with the voice, the real captions and Todd's own screen recordings, numbered (v1, v2…) and labelled by beat so
   feedback can point at one ("b3 is rushed"). The human approves or says what to change; pace has its own controls
   (`short_pace`: voice speed, pause between lines, least time per shot, words per caption). Each AI shot is its start frame (a $0.003 still or a screenshot) with a slow zoom and a label
   (prompt, length, price). The agent reviews it frame by frame (`cut_frames`), then the human watches it. **Approving
   the animatic approves the spend**: one `authorize_spend` for the batch, at the planned price.
5. **Generate** (expensive, once). Each AI clip is generated image-to-video from the exact still approved in the
   animatic, at the planned length, in parallel. Nothing about timing is decided here.
6. **Final cut.** The same timeline with the generated clips in place of the placeholders. A clip that came back short,
   or with the action in the wrong place, is flagged and re-generated on its own at the same length. The voice and the
   other shots don't move.

**Rendering keeps sync to the sample.** Video segments are rendered to exact frame counts and joined video-only; the
voice is one track placed by sample offsets; captions are burned in from the same timeline in the final pass. Audio is
never cut into per-segment files and concatenated, which is where AAC padding gaps and drift come from. A test renders
colour-coded segments against a test tone and checks that the cut frame and the tone's onset agree within one frame.

**If a line changes after generation**, the new take moves the timings. The planner compares old and new slots: clips
that still cover their new slot within the trim and speed limits are kept, and the rest are listed with their
re-generation price before anything is spent.

## Providers (checked 2026-10-06)

**Trend data.** TikTok's official Research API is for academic and non-profit research only, and commercial use is
explicitly excluded, so a commercial trend scan needs a data provider. Each sits behind one `trends` interface:

| Provider | Covers | Gives | Cost | Notes |
| --- | --- | --- | --- | --- |
| [Apify](https://apify.com/) actors (**chosen**) | TikTok, Instagram Reels, YouTube Shorts | search by keyword or hashtag, a creator's recent videos, views, likes, shares, music, video URL | per actor run (pay as you go) | The human's choice (2026-10-06): one Apify token in the vault, an actor per platform behind the `trends` interface. Public data via scraping; the user brings their own token. |
| [ScrapeCreators](https://docs.scrapecreators.com/) | TikTok, Instagram Reels, YouTube Shorts | keyword/hashtag search, trending feed, a creator's recent videos (for the outlier baseline), transcripts, play/like/share counts, music, download URL | about $47 per 25k credits, most calls 1 credit; credits don't expire | One key for all three platforms. Public data via scraping: the platforms' ToS don't bless it, and the user brings their own key. |
| [YouTube Data API v3](https://developers.google.com/youtube/v3) | YouTube Shorts | `search.list` by topic, date and view count; stats via `videos.list` | free quota (10k units/day; a search is 100) | Official. No Shorts filter (filter on duration and vertical yourself), and no transcripts for other people's videos (transcribe with Scribe instead). |
| [EnsembleData](https://ensembledata.com/) | TikTok, Instagram, YouTube | similar to ScrapeCreators | from $100/month | An alternative behind the same interface. |

Reference videos are used for analysis only: the transcript, ~8 sampled frames and metadata stay in `research/`, the
downloaded file is deleted right after sampling, and nothing from them is republished.

**Higgsfield** ([API](https://higgsfield.ai/blog/higgsfield-api), [docs](https://docs.higgsfield.ai/)): one key
(`Authorization: Key <id>:<secret>`, base `https://api.higgsfield.ai`), 50+ video and image models, submit-then-poll
(`/requests/{id}/status`) or webhooks, Python and TypeScript SDKs. Pay as you go from a prepaid balance ($5 minimum),
failed generations aren't charged, outputs are kept at least 7 days (download them right away), and output can be used
commercially. Prices per second of video: Seedance 2.5 $0.074, Kling 3.0 $0.112, PixVerse 6 $0.115, MiniMax H3 $0.13.
Stills (Soul 2): $0.003 each. Typical use: a generated still in the right style (or a real screenshot) as the start
frame, then image-to-video for a 3–5 s shot.

**ElevenLabs, voiceover only** ([pricing](https://elevenlabs.io/pricing/api)): text to speech with character
timestamps from the same call (`POST /v1/text-to-speech/{voice_id}/with-timestamps`, header `xi-api-key`; response
`audio_base64` plus `alignment.characters` / `character_start_times_seconds` / `character_end_times_seconds`), so
captions and cuts come from the model that made the audio and can't drift. $0.08 per 1K characters (Multilingual v2,
v3): a 15 s script is ~250 characters, about 2¢ a take. Scribe speech-to-text ($0.22/hour) can transcribe references
that have no captions.

**Rough cost of one 15 s short:** trend scan and transcripts ≈ $0.10; voiceover for three hook variants, with a few
re-takes ≈ $0.20; three AI shots of 4–5 s on Seedance or Kling ≈ $0.90–1.70, plus any re-generated shot. **About
$1.50–3 per video** in provider costs, plus model tokens for analysis and writing. Everything before generation (the
script, the voice lock, the plan, the animatic) costs well under a dollar, which is the point.

## How it fits into Todd

Same structure as step 1: built-in toolsets registered in `registry.py`, heavy work in the `media` container, keys in
the vault, spending through `authorize_spend`, nothing posted without the existing approval gates. The planner decides
which agents run which stages; a run typically has a research agent (scan + format cards), then a producer agent.

**Tools** (names provisional; each writes its result into the run folder):

| Tool | Does | Spends |
| --- | --- | --- |
| `trend_scan(terms, platforms, days, limit)` | recent short videos for the niche terms, with metrics and outlier scores → `research/sources.json` | provider credits |
| `trend_analyze(video)` | transcript + sampled frames + on-screen text of one reference → a draft format card for the agent to finish | provider / STT |
| `format_save(card)` / `format_search(niche, platform)` | the shared format library | – |
| `script_new(slug, format, hooks, beats)` | validated script (timings add up, every beat has a shot source) → `script.json`; the human approves it | – |
| `screen_record(url, steps, seconds, device)` | a phone- or desktop-sized recording of a real page via CDP screencast → `shots/` | – |
| `clip_generate(prompt, start_image, seconds, model)` | an AI clip (Higgsfield) → `shots/` | yes |
| `voiceover(slug)` | one take per hook variant + character timestamps (ElevenLabs) → `audio/` | cents |
| `plan(slug, hook)` | the timing plan from the take → `timeline-<hook>.json`, a sync report and the exact generation cost | – |
| `cut_render(slug, hook, mode)` | `animatic` (placeholders for AI shots) or `final` → `<slug>-<hook>[-animatic].mp4` | – |
| `cut_frames(slug)` | a contact sheet of the cut at fixed intervals, so the agent can look at its own work before the human does | – |

**media service additions:** `/render/timeline` (clips, stills, overlays, the voice track, an optional ducked music
track, word-synced ASS captions via libass: all available in its FFmpeg build), `/files/put` (the API hands it the
voiceover bytes; the API never mounts the workspace), `/frames` (sample frames from a clip), and `/screencast/assemble`
(CDP frames → constant-frame-rate MP4).

**New clients** (`tools/`): one per provider behind a small interface (`trends`, `higgsfield`, `elevenlabs`), so a
provider can be swapped or a second one added without touching the tools. Each gets an `integrations.py` catalog entry
for its key.

**Gates:** the human approves the script, then the animatic with its exact price; that approval is one
`authorize_spend` for the whole generation batch (and one for a short's voiceover takes), so the human isn't asked per
clip. A per-video cap; AI-generated content is flagged in the run so it's labeled when posted (step 4).

## Build order

1. **Foundation, no paid keys needed.** Done (2026-10-06): the script schema and validator, the timing planner, the
   timeline renderer with word captions (scaffold and final modes), the ElevenLabs client, Todd filming its own product
   (`short_record`), and the free scaffold voice. Still to do: `cut_frames`, the format library table, and the eval
   harness with the two frozen fixtures.
2. **Trend scan and format cards** with Apify. Done (2026-10-06): `trend_scan`, `trend_analyze`, `format_save`,
   `format_search`. Learned on the real scans: TikTok blocks keyword search on the scraper (its mobile API refused a
   device), so the scan works from hashtags, which the niche map produces anyway; a hashtag can be off-niche
   (#hockeyboys is half book-fandom edits, #oranjehockey is field hockey), so the research agent has to judge, not
   just rank; two scans of ~60 videos cost $0.22 each, studying 4 videos ~$0.02. Still to do: freeze the two
   fixtures' scan data for the eval, Instagram Reels and YouTube Shorts actors.
3. **ElevenLabs:** voiceover takes with character timestamps (the voice lock).
4. **Higgsfield:** clips with spend gates, start-frame consistency.
5. **Eval both test cases end to end**, fix what the rubric flags, then step 4 (posting and the performance loop).

## Decisions needed

1. ~~Trend data~~: Apify (decided 2026-10-06).
2. **Spending:** a default cap per video (proposed $5) and the rule that a script and its estimate need approval before
   any generation.
3. **AI people:** may the engine generate AI people for skits (labeled as AI-generated when posted), or only product,
   places and objects?
4. **Keys** (the human adds them later): Apify, ElevenLabs (any plan for testing; a paid plan for commercial use),
   Higgsfield (key id + secret). All go in the vault. Until then the scaffold works with none of them.
