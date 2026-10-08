# Shorts: what makes them read as human-made, and the flow that gets there

Research notes behind the shorts and trends guides (`services/api/todd/registry.py`) and the planner's short-video
flow (`prompts/planner.md`). Gathered 2026-10-08. Evidence is marked: [E] is platform data, a peer-reviewed study or a
large dataset; [P] is a practitioner or vendor claim. Everything here is generic: no product or niche is assumed.

## Why the output was all screen recordings

- The trend agent never looked at the frames. `trend_analyze` made an 8-frame sheet but didn't show it, and one
  agent wrote "I worked from transcripts, captions and numbers". So cards described what was said, not what was
  shown or how it was cut. One card claimed "hard cuts on the beat" for a single continuous phone shot of a desk.
- The engine could only make one kind of short. Every beat needed a spoken line, the only real footage was
  `short_record`, captions were per-word karaoke, and the guide said "prefer your recordings".
- The flow filmed first. A "footage" agent recorded a product tour before any format was chosen, so producers
  fitted the format around the tour.

## What reads as native

**Hook and payoff**
- [E] Over 63% of TikTok's highest-CTR auction ads show the key message or product within 3 s.
  ([TikTok](https://ads.tiktok.com/business/en-US/blog/9-creative-tips-to-drive-auction-ad-performance))
- [E] 90% of ad recall happens in the first 6 s.
  ([Creative Codes](https://ads.tiktok.com/business/en/blog/creative-best-practices-top-performing-ads))

**Lo-fi over polish**
- [E] TikTok tells advertisers to "lean into lo-fi… smartphones are perfect".
  ([SMB Playbook 2025](https://ads.tiktok.com/business/library/TikTok_SMB_Creative_Playbook_2025.pdf))
- [E] Native-looking ads "felt more like content".
  ([Magna/TikTok via Digiday](https://digiday.com/marketing/magna-research-the-dos-and-donts-of-native-and-repurposed-advertising-on-tiktok))

**Cut pace**
- [E] An eye-tracking study (2,520 viewings of 42 ads) found that each cut lifts attention. Median scene length was
  2.12 s (IQR 1.5–3 s), with diminishing returns and no scenes under 1 s.
  ([JAMS 2026](https://link.springer.com/article/10.1007/s11747-025-01137-x))
- [E] Top informational talking-head TikToks cut about every 2.7 s. Seamless jump cuts raised engagement.
  ([Dost & Huang 2026](https://archives.marketing-trends-congress.com/2026/pages/PDF/paper_professor_DOST_HUANG.pdf))

**Text, people and sound**
- [E] On Meta, text overlays scored +11 pts in positive response and human presence gave +27% CTR. Music plus
  voiceover scored +15 pts over no sound.
  ([Meta Reels guide](https://irp.cdn-website.com/e41755d3/files/uploaded/Reels+Playbook+from+Meta.pdf))
- [E] 40% of TikTok's top view-through ads use concise text overlays.

**Safe zones**
- [E] Meta: keep the top 14%, the bottom 35% and 6% at each side clear.
- TikTok's organic UI covers about 200 px at the top, 334 px at the bottom and 140 px on the right of a
  1080×1920 frame.

**Trending sounds**
- [E] Trending sounds are personal-use only for business accounts, which get the Commercial Music Library.
- [P] The posting API can't attach a sound, so it is added in the app.
  ([TikTok CML](https://ads.tiktok.com/resources/help/article/commercial-music-library?lang=en))
- The engine therefore renders text + sound shorts silent and names the sound to add in the app.

**What's saturating**
- [E] Slideshow/carousel views fell 23% and interactions 62% year over year (Metricool, 2.3M posts).
  ([Metricool](https://metricool.com/how-to-go-viral-on-tiktok))
- [P] AI avatars are "cluttering" app marketing.
- [E] Templated, mass-produced video is being down-ranked: YouTube's inauthentic-content rule, Meta's
  unoriginal-content penalties.
- The edge is format variety and specific detail.

**Practitioner conventions [P]**
- Text: TikTok Sans or Classic, white with a black edge or in a box, 1–2 lines.
- Captions: word-by-word karaoke reads as an automated clipper on formats that aren't talking heads. Phrase captions
  go with a voice; text + sound formats have no captions.
- Edits: hard cuts, jump cuts to remove dead time, and punch-ins of 1.1–1.3× to hide seams.

**Honesty [E]**
- FTC rule against AI-generated fake testimonials:
  [Hunton](https://hunton.com/hunton-retail-law-resource/ftc-issues-final-rule-targeting-fake-consumer-reviews-and-testimonials-including-those-generated-by-artificial-intelligence-ai-and-online-bots).
- TikTok labels realistic AI content.
- The engine never presents a stock or AI person as a customer, and never fakes a post, comment, review or number.

## Formats that work without the founder on camera

| Format | Shape | Shots | Audio |
| --- | --- | --- | --- |
| POV / text meme | "pov: …" from frame 0 → 1–2 situation shots → product payoff, 6–12 s | b-roll, a screen | text + sound |
| Listicle ("things nobody tells you") | hook → 3–5 items of 2–4 s, product as one item | b-roll per item, a screen | voice or text |
| Wall-of-text storytime | 1–3 slow b-roll shots, text block swapped every 3–5 s | b-roll | text + sound |
| Problem → solution | 2–3 s of the painful way → hard cut to the product doing it | the old way, then the screen | text + sound or voice |
| Hook + demo | 1.5–3 s reaction or situation → screen recording of the "magic moment" | a person (AI/stock), a screen | sound + text |
| Green-screen-style screenshot | a real screenshot full-frame, punch-ins on lines, then the product | stills, screens | voice |
| Satisfying screen use | one smooth messy-to-done flow, tight crops, speed ramps | a screen | UI sound or none |

Example: a [Pingo case](https://playkit.beehiiv.com/p/case-study-pingo-tiktok-strategy) used a crying face with
"why tf is spanish so hard", then the app demo, at 10–12 s, and got 50M / 24M / 10M views [P]. Recommendation for
that hook: keep an AI or stock reaction silent and put the words on screen.

## Decoding a reference (what code measures, what the model labels)

- **Measure in code; let the model label and interpret.** VLMs are good at shot attributes and poor at timing and
  camera motion. Sources: [From Shots to Stories](https://arxiv.org/html/2505.12237v1),
  [CameraBench](https://arxiv.org/abs/2504.15376).
- **Cuts.** Use FFmpeg scene scores with adaptive spikes against the local level, like
  [PySceneDetect's AdaptiveDetector](https://www.scenedetect.com/docs/latest/api/detectors.html). A hard cut scores
  0.4–1. A jump cut in a talking head scores about 0.15 against a near-zero background. A fast scroll scores high on
  every frame, so it isn't a string of cuts. (Measured on real references while building this.)
- **Frames.** Take one per shot, and one every 2.5 s of a long shot, labelled with the shot number and time.
  Numbered frames improve temporal grounding ([NumPro](https://arxiv.org/abs/2411.10332)). Show them to the model as
  images.
- **Cards** record the shot list as it really is: a fixed shot-type list, what each shot shows, how it's filmed,
  the exact text and what's said. They also record the audio mode, the text style, why it works (pointing at shots)
  and how to adapt it (keep the mechanism, change the content).
- **Clone tools agree:** Creatify, Higgsfield Marketing Studio and TopView all say "clone the structure, not the
  look". [AgenticGen](https://huggingface.co/papers/2609.09187) (TikTok ads) separates choosing a strategy from
  drafting it.

## Making each shot

| Card shot | Source |
| --- | --- |
| screen | `short_record` of the product (or the real public page the problem lives on), one moment per cut |
| screenshot | a still of a real page; never a made-up post or chat |
| talking-head / person / pov-hands | an AI shot, silent with the line as text; or stock of someone doing it; or drop the face |
| place / object | `short_stock` first, an AI shot second |
| text-only | text over a real shot, never a designed card |
| reused (TV, games, creators) | never; a shot that does the same job |

**Stock that doesn't look like stock**
- Search with plain, concrete, first-person words ("hands lacing skates", "pov typing laptop night"). Never
  "business" or "success".
- Prefer portrait clips. Reject glossy, posed, slow-motion, drone and watermarked clips.
- Use 0.8–2 s from the middle of a clip, slightly desaturated with light grain.
- API notes: Pixabay video search has no orientation filter, so filter on width < height yourself. Pexels has
  `orientation=portrait`.

**AI shots (when Higgsfield is connected)**
- Prompt as phone video:
  > Vertical 9:16 smartphone video, \<front-camera selfie | rear camera in one hand | propped>, slightly off-center,
  > one continuous take. \<the same cast description in every shot>. \<one simple action>. In \<an ordinary place>,
  > lit by \<a named light>; phone auto-exposure, natural skin texture, slight handheld shake. Screens and signs
  > blank or turned away.
- Never ask for text, UI or logos, and never use "cinematic", "epic" or "studio".
- Generate 4–5 s and keep the best 1–3 s.
- Sources: [Veo 3.1 guide](https://cloud.google.com/blog/products/ai-machine-learning/ultimate-prompting-guide-for-veo-3-1),
  [Kling 3.0 guide](https://blog.fal.ai/kling-3-0-prompting-guide/),
  [Luma UGC prompts](https://lumalabs.ai/news/ugc-style-ai-video-prompts).
- Higgsfield's public API (checked 2026-10-08):
  - `https://api.higgsfield.ai`, header `Authorization: Key ID:SECRET`.
  - `POST /{model-path}` returns a request id. Poll `GET /requests/{id}/status` for `video.url`.
  - Failed and NSFW results aren't charged.
  - Docs: [docs.higgsfield.ai](https://docs.higgsfield.ai/docs).
  - Sora 2's API shut down on 2026-09-24.

## Finding the references (Apify)

**`clockworks~tiktok-scraper`**
- Keyword search works: `searchQueries` with `searchSection: "/video"`, verified 2026-10-08.
- Don't set `videoSearchSorting` or `videoSearchDateFilter`. Those go through TikTok's mobile API, and that path
  has failed with "device id" errors (fixed and broken again in Aug 2026).
- Filter by date and by `textLanguage` yourself.

**Related videos**
- `postURLs` + `scrapeRelatedVideos` expands from the best on-niche results.
- `clockworks~tiktok-profile-scraper` gives creator baselines (plays ÷ the creator's median plays, as vidIQ does). It
  is worth adding for the top few.

**Costs and caps**
- Pay-per-event pricing on the free plan: $0.0037 a result, $0.001 an actor start, $0.0013 a video download.
- `maxTotalChargeUsd` caps a run, but Apify refuses caps under $0.50.

**Other platforms (not wired in yet)**
- YouTube Shorts: `streamers~youtube-scraper`, with `maxResultsShorts` and subscriber counts included.
- Instagram reels: `apify~instagram-hashtag-scraper` with `keywordSearch`. It has no follower counts; that needs a
  second call.
