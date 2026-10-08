"""The trends toolset: scan (Apify, faked), outlier ranking, studying references, the format library. The real
sandbox server on a temp folder, a fake media service and a fake Apify: no network, no FFmpeg."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from todd import registry, vault
from todd.config import config
from todd.db import LedgerEntry, select, session
from todd.integrations import find_integrations
from todd.runtime import RunContext
from todd.sdk import ToolError, _current_ctx, set_ctx
from todd.tools import apify, media, sandbox, trends

from .helpers import new_run

SERVER = Path(__file__).resolve().parents[2] / "sandbox" / "server.py"
KV = "https://api.apify.com/v2/key-value-stores/abc/records"
NOW = datetime.now(timezone.utc)


def tiktok(vid: str, plays: int, fans: int, days_ago: int = 10, shares: int = 100, ad: bool = False,
           subs: bool = True, tag: str = "beerleaguehockey") -> dict:
    return {"id": vid, "webVideoUrl": f"https://www.tiktok.com/@u{vid}/video/{vid}", "playCount": plays,
            "diggCount": plays // 20, "commentCount": 10, "shareCount": shares, "collectCount": 5,
            "createTimeISO": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
            "text": f"video {vid} #hockey", "hashtags": [{"name": "hockey"}], "isAd": ad,
            "authorMeta": {"name": f"u{vid}", "fans": fans},
            "musicMeta": {"musicName": "original sound", "musicOriginal": True},
            "videoMeta": {"duration": 30, "subtitleLinks": [
                {"language": "eng-US", "downloadLink": f"{KV}/subtitle-{vid}"}] if subs else []},
            "searchHashtag": {"name": tag}}


VTT = b"WEBVTT\n\n00:00:00.820 --> 00:00:03.360\nI ball in some kids or what?\n\n00:00:04.180 --> 00:00:06.380\n" \
      b"Sorry. Like you scout these kids.\n"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("SANDBOX_TOKEN", "t")
    spec = importlib.util.spec_from_file_location("todd_sandbox_server_trends", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    client = TestClient(mod.app)
    monkeypatch.setattr(config, "workspace_root", str(tmp_path))

    async def box_call(path, payload, timeout=120, **_):
        r = client.post(path, json=payload, headers={"X-Sandbox-Token": "t"})
        if r.status_code >= 400:
            raise ToolError(f"sandbox {path} -> {r.status_code}: {r.text[:1000]}")
        return r.json()

    monkeypatch.setattr(sandbox, "call", box_call)
    calls: list[tuple[str, dict]] = []

    async def media_call(path, payload, timeout=120):
        calls.append((path, payload))
        run = tmp_path / payload["run_id"]
        if path == "/files/put":
            p = run / payload["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(payload["data_b64"]))
            return {"path": payload["path"], "size_bytes": p.stat().st_size, "sha256": "x"}
        if path == "/shots":
            (run / payload["out"]).write_bytes(b"png")
            if payload.get("delete_source"):
                (run / payload["src"]).unlink()
            return {"duration_s": 7.0, "shots": [{"n": 1, "start": 0.0, "end": 3.9}, {"n": 2, "start": 3.9, "end": 7.0}],
                    "cuts": 1, "avg_shot_s": 3.5, "longest_shot_s": 3.9, "sheets": [payload["out"]],
                    "images_b64": [base64.b64encode(b"sheet-" + payload["src"].encode()).decode()]}
        if path == "/transcribe":
            return {"text": "heard words", "words": [{"word": "heard", "start": 0.5, "end": 0.8},
                                                     {"word": "words", "start": 0.8, "end": 1.2}]}
        raise AssertionError(path)

    monkeypatch.setattr(media, "call", media_call)
    runs: list[tuple[str, dict]] = []
    fetched: list[str] = []
    items = {"scan": [
        tiktok("1", 212000, 7505), tiktok("2", 70400, 1953, shares=278), tiktok("3", 900000, 2_000_000),
        tiktok("4", 40000, 50), tiktok("5", 300000, 1000, ad=True), tiktok("6", 500000, 900, days_ago=400),
        tiktok("7", 3000, 100), tiktok("1", 212000, 7505, tag="beerleague"), {"error": "No videos", "input": "x"},
        tiktok("8", 60000, 30000, subs=False)]}

    async def run_actor(token, actor, payload, max_wait=600, max_charge_usd=None):
        assert token == "apify-test-token" and actor == apify.TIKTOK
        assert max_charge_usd and max_charge_usd >= 0.01  # every run is capped at what was approved
        runs.append((actor, payload))
        if payload.get("scrapeRelatedVideos"):
            return {"id": "r3", "usageTotalUsd": 0.02}, [
                tiktok("1", 212000, 7505), {**tiktok("20", 90000, 500), "searchHashtag": None, "isRelated": True},
                {**tiktok("21", 80000, 400), "searchHashtag": None, "textLanguage": "nl"}]
        if payload.get("postURLs"):
            ids = [u.rsplit("/", 1)[1] for u in payload["postURLs"]]
            return {"id": "r2", "usageTotalUsd": 0.011}, [
                {**tiktok(i, 1, 1), "mediaUrls": [f"{KV}/video-{i}"]} for i in ids]
        return {"id": "r1", "usageTotalUsd": 0.0333}, items["scan"]

    async def fetch(token, url, max_bytes=25_000_000):
        assert token == "apify-test-token" and apify.stored(url)
        fetched.append(url)
        return VTT if "subtitle" in url else b"\x00\x00\x00\x20ftypisom video"

    monkeypatch.setattr(apify, "run_actor", run_actor)
    monkeypatch.setattr(apify, "fetch", fetch)
    rid = new_run("trends")
    (tmp_path / rid).mkdir()
    tok = set_ctx(RunContext(rid))
    yield {"run": rid, "dir": tmp_path / rid, "calls": calls, "runs": runs, "fetched": fetched}
    _current_ctx.reset(tok)
    vault.delete_secret("APIFY_API_TOKEN")


def ledger(run_id: str) -> list[LedgerEntry]:
    with session() as s:
        return s.exec(select(LedgerEntry).where(LedgerEntry.run_id == run_id)).all()


def test_scan_ranks_breakouts_and_keeps_everything(loop, lab, monkeypatch):
    from todd.sdk import get_ctx
    asked: list[tuple[str, dict]] = []

    async def go():
        async def later(question, agent=None, data=None):
            asked.append((question, data))
            await asyncio.sleep(0.05)
            return "later"

        monkeypatch.setattr(get_ctx(), "ask_human", later)
        # the scan asks for its own key (side-by-side scans share one card)
        for res in await asyncio.gather(*[trends.trend_scan.ainvoke({"hashtags": ["beerleaguehockey"]})
                                          for _ in range(2)], return_exceptions=True):
            assert isinstance(res, ToolError) and "didn't add APIFY_API_TOKEN" in str(res)
        assert len(asked) == 2 and asked[0][1] == {"secret_name": "APIFY_API_TOKEN"}  # one at a time, not two at once
        assert "console.apify.com/settings/integrations" in asked[0][0]

        async def paste(question, agent=None, data=None):
            asked.append((question, data))
            await asyncio.sleep(0.05)
            vault.set_secret(data["secret_name"], "apify-test-token")
            return ""

        asked.clear()
        monkeypatch.setattr(get_ctx(), "ask_human", paste)
        assert await asyncio.gather(trends._key(), trends._key()) == ["apify-test-token"] * 2
        assert len(asked) == 1  # one card, and both went ahead with the key it saved
        for bad in ([], ["#ok", "not ok"], [f"t{i}" for i in range(9)]):
            with pytest.raises(ToolError, match="hashtags"):
                await trends.trend_scan.ainvoke({"hashtags": bad})
        r = await trends.trend_scan.ainvoke({"hashtags": ["#BeerLeagueHockey", "beerleague"], "per_tag": 10})
        assert lab["runs"][0][1] == {"hashtags": ["beerleaguehockey", "beerleague"], "resultsPerPage": 10,
                                     "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES"}
        # deduped (video 1 came from both tags), no ad (5), nothing older than 120 days (6), nothing under 10k plays (7)
        assert r["found"] == 8 and r["kept"] == 5
        assert [v["id"] for v in r["top"]] == ["4", "2", "1", "8", "3"]  # by reach: 40000/100 floor, 70400/1953, …
        top = r["top"][0]
        assert top["reach"] == 400.0 and top["transcript"] is True
        assert r["top"][-1]["reach"] == 0.45  # a big account's hit: far down the list
        saved = json.loads((lab["dir"] / "video" / "research" / r["scan"] / "scan.json").read_text())
        assert len(saved["videos"]) == 5 and saved["usd"] == 0.0333
        (e,) = ledger(lab["run"])
        assert (e.merchant, e.status, e.amount_usd) == ("Apify", "completed", 0.08)  # 20 results, priced up front
        assert e.data["usd"] == 0.0333  # what Apify actually charged
    loop.run_until_complete(go())


def test_analyze_reads_transcripts_takes_frames_and_deletes_the_video(loop, lab):
    vault.set_secret("APIFY_API_TOKEN", "apify-test-token")

    async def go():
        scan = (await trends.trend_scan.ainvoke({"hashtags": ["beerleaguehockey"]}))["scan"]
        with pytest.raises(ToolError, match="ids from scan"):
            await trends.trend_analyze.ainvoke({"scan": scan, "ids": ["999"]})
        with pytest.raises(ToolError, match="no scan"):
            await trends.trend_analyze.ainvoke({"scan": "../x", "ids": ["1"]})
        r = await trends.trend_analyze.ainvoke({"scan": scan, "ids": ["1", "8"]})
        one, eight = r["videos"]
        # decoded shot by shot: the words said over each shot, from the subtitles
        assert one["transcript_source"] == "subtitles" and one["cuts"] == 1 and one["avg_shot_s"] == 3.5
        assert one["shots"] == [{"n": 1, "start": 0.0, "end": 3.9, "said": "I ball in some kids or what?"},
                                {"n": 2, "start": 3.9, "end": 7.0, "said": "Sorry. Like you scout these kids."}]
        assert one["speech_share"] == pytest.approx((3.36 - 0.82 + 6.38 - 4.18) / 7.0, abs=0.01)
        assert eight["transcript_source"] == "local transcription"  # no subtitles: transcribed here, free
        assert eight["shots"][0]["said"] == "heard words"
        assert one["sheets"] == [f"video/research/{scan}/1.jpg"] and r["sheets_shown"] == 2
        from todd.sdk import get_agent_id, get_ctx
        shown = get_ctx().pop_images(get_agent_id())  # the agent sees every sheet, in the order of the videos
        assert [base64.b64decode(b) for b, _ in shown] == [f"sheet-video/research/{scan}/{i}.mp4".encode()
                                                          for i in ("1", "8")]
        d = lab["dir"] / "video" / "research" / scan
        assert (d / "1.jpg").exists() and not (d / "1.mp4").exists()  # the reference itself is gone
        assert json.loads((d / "1.json").read_text())["caption"] == "video 1 #hockey"
        dl = lab["runs"][-1][1]
        assert dl["postURLs"] == ["https://www.tiktok.com/@u1/video/1", "https://www.tiktok.com/@u8/video/8"]
        assert dl["shouldDownloadVideos"] is True
        assert [e.description.split(":")[0] for e in ledger(lab["run"])] == ["Trend scan", "Download 2 reference "
                                                                             "TikToks to study (scan beerleaguehockey)"]
    loop.run_until_complete(go())


def test_format_cards_are_saved_with_real_examples_and_found_by_niche(loop, lab):
    vault.set_secret("APIFY_API_TOKEN", "apify-test-token")

    async def go():
        await trends.trend_scan.ainvoke({"hashtags": ["beerleaguehockey"]})
        card = {"name": "men's-league guy, deadpan", "tags": ["BeerLeagueHockey", "adulthockey"],
                "hook": {"visual": "rink-side phone camera", "line": "I ball in some kids or what?"},
                "shots": [{"seconds": 3.9, "type": "talking-head", "shows": "a guy in full gear on the bench",
                           "camera": "handheld, rink-side", "said": "I ball in some kids or what?"},
                          {"seconds": 3.1, "type": "person", "shows": "the kid skating away", "text": "he's 12"}],
                "audio": "skit",
                "why": "The gap between how seriously he takes men's league and reality", "examples": ["1", "2", "zz"],
                "length_s": [30, 60], "adapt": "the product is how the sane friend knows the schedule"}
        with pytest.raises(ToolError, match="examples must be"):
            await trends.format_save.ainvoke({**card, "examples": ["nope"]})
        with pytest.raises(ToolError, match="audio is one of"):
            await trends.format_save.ainvoke({**card, "audio": "music"})
        with pytest.raises(ToolError, match="shot 2"):
            await trends.format_save.ainvoke({**card, "shots": [card["shots"][0], {"type": "drone", "shows": "x"}]})
        saved = await trends.format_save.ainvoke(card)
        assert saved["examples"] == 2 and saved["needs"] == ["person", "talking-head"]
        assert saved["measured"] is None and "no measured pace" in saved["note"]  # nothing studied shot by shot yet
        # (a different size of scan: the same charge twice in a run would stop to ask the human)
        scan = (await trends.trend_scan.ainvoke({"hashtags": ["beerleaguehockey"], "per_tag": 12}))["scan"]
        await trends.trend_analyze.ainvoke({"scan": scan, "ids": ["1", "2"]})
        studied = await trends.format_save.ainvoke(card)
        assert studied["measured"] == {"examples": 2, "shots": 2, "avg_shot_s": 3.5, "length_s": [7.0, 7.0],
                                       "speech_share": pytest.approx(0.68, abs=0.01)}
        found = (await trends.format_search.ainvoke({"tags": ["adulthockey", "goalie"]}))["cards"]
        mine = next(c for c in found if c["id"] == saved["id"])
        assert mine["tags"] == ["beerleaguehockey", "adulthockey"] and mine["hook"]["line"].startswith("I ball")
        assert mine["audio"] == "skit" and mine["shots"][1] == {"seconds": 3.1, "type": "person",
                                                                "shows": "the kid skating away", "text": "he's 12"}
        assert [e["id"] for e in mine["examples"]] == ["1", "2"] and mine["examples"][0]["reach"] == 28.25
        assert saved["id"] not in [c["id"] for c in (await trends.format_search.ainvoke({"tags": ["saas"]}))["cards"]]
    loop.run_until_complete(go())


def test_apify_token_only_goes_to_apify_storage(loop):
    assert apify.stored(f"{KV}/subtitle-1") and not apify.stored("https://evil.example/v2/key-value-stores/x")
    assert not apify.stored("http://api.apify.com/v2/key-value-stores/x/records/y")  # https only
    assert not apify.stored("https://api.apify.com/v2/users/me")  # storage only
    with pytest.raises(ToolError, match="not a file in Apify's storage"):
        loop.run_until_complete(apify.fetch("secret", "https://evil.example/steal"))
    assert trends.parse_vtt(VTT.decode()) == [{"start": 0.82, "end": 3.36, "text": "I ball in some kids or what?"},
                                              {"start": 4.18, "end": 6.38, "text": "Sorry. Like you scout these kids."}]
    assert apify.estimate(apify.TIKTOK, 60) == pytest.approx(0.001 + 60 * 0.0037)


def test_trends_toolset_and_apify_routing(loop):
    ts = registry.all_toolsets()
    assert [t.name for t in ts["trends"].tools] == ["trend_scan", "trend_analyze", "format_save", "format_search"]
    vault.set_secret("APIFY_API_TOKEN", "apify-test-token")
    try:
        ctx = RunContext(new_run("trends routing"))
        ctx.toolsets = {"trends": ts["trends"]}  # type: ignore[attr-defined]
        tok = set_ctx(ctx)
        try:
            r = loop.run_until_complete(find_integrations.ainvoke({"services": ["apify", "tiktok trends"]}))
        finally:
            _current_ctx.reset(tok)
        assert [s["route"] for s in r["services"]] == ["toolset", "toolset"]
    finally:
        vault.delete_secret("APIFY_API_TOKEN")


def test_scan_searches_phrases_keeps_the_language_and_follows_related_videos(loop, lab):
    vault.set_secret("APIFY_API_TOKEN", "apify-test-token")

    async def go():
        with pytest.raises(ToolError, match="search phrases"):
            await trends.trend_scan.ainvoke({"queries": ["x"]})
        r = await trends.trend_scan.ainvoke({"queries": ["Drop In  Hockey", "beer league goalie"],
                                             "hashtags": ["beerleague"], "per_tag": 5})
        assert lab["runs"][-1][1] == {"searchQueries": ["drop in hockey", "beer league goalie"],
                                      "searchSection": "/video", "hashtags": ["beerleague"], "resultsPerPage": 5,
                                      "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES"}  # no sort/date: those break
        assert r["scan"].startswith("drop-in-hockey") and r["top"][0]["found_by"] == "#beerleaguehockey"
        assert "search found nothing for drop in hockey" in r["note"]
        with pytest.raises(ToolError, match="from this run's scans"):
            await trends.trend_scan.ainvoke({"related": ["999"]})
        rel = await trends.trend_scan.ainvoke({"related": ["1"], "min_plays": 1000})
        payload = lab["runs"][-1][1]
        assert payload["postURLs"] == ["https://www.tiktok.com/@u1/video/1"] and payload["scrapeRelatedVideos"]
        assert [v["id"] for v in rel["top"]] == ["20"]  # not the seed itself, not the Dutch caption
        saved = json.loads((lab["dir"] / "video" / "research" / rel["scan"] / "scan.json").read_text())
        assert saved["other_language"] == 1 and saved["related"] == ["1"]
    loop.run_until_complete(go())
