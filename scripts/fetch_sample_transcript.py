"""평가용 샘플 전사 수집 — 채널 RSS(키 불필요)로 최신 영상을 찾아 자막을 받는다.

원본 비저장 원칙(CLAUDE.md): 출력은 eval_data/ (gitignore)에만 저장. 커밋 금지.

사용:
    python scripts/fetch_sample_transcript.py <channel_id> [--max-videos 5]
    python scripts/fetch_sample_transcript.py --list   # channels_final CSV에서 channel_id 목록
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "eval_data")
CSV_PATH = os.path.join(BASE, "data", "channels_final_v1.0.csv")
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def list_channels():
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            raw = row.get("channel_id/URL", "")
            m = re.search(r"(UC[0-9A-Za-z_-]{22})", raw)
            cid = m.group(1) if m else "?"
            print(f"{cid}\t{row.get('성향축','')}\t{row.get('채널명','')}")


def rss_videos(channel_id: str):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    with urllib.request.urlopen(url, timeout=15) as r:
        root = ET.fromstring(r.read())
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        title = e.findtext("a:title", namespaces=NS)
        published = e.findtext("a:published", namespaces=NS)
        out.append({"video_id": vid, "title": title, "published": published})
    return out


def fetch_transcript(video_id: str):
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    tr = api.fetch(video_id, languages=["ko"])
    return [{"start_ms": int(s.start * 1000),
             "end_ms": int((s.start + s.duration) * 1000),
             "text": s.text} for s in tr]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("channel_id", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--max-videos", type=int, default=5)
    args = ap.parse_args()

    if args.list or not args.channel_id:
        list_channels()
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    videos = rss_videos(args.channel_id)
    print(f"RSS 최신 영상 {len(videos)}개. 자막 시도 (최대 {args.max_videos}개 성공까지)...")
    ok = 0
    for v in videos:
        if ok >= args.max_videos:
            break
        try:
            segs = fetch_transcript(v["video_id"])
        except Exception as e:
            print(f"  ✗ {v['video_id']} {v['title'][:40]} — {type(e).__name__}")
            continue
        total_chars = sum(len(s["text"]) for s in segs)
        out = {"video_id": v["video_id"], "title": v["title"],
               "published": v["published"], "channel_id": args.channel_id,
               "duration_ms": segs[-1]["end_ms"] if segs else 0,
               "segments": segs}
        path = os.path.join(OUT_DIR, f"transcript_{v['video_id']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        print(f"  ✓ {v['video_id']} {v['title'][:40]} — {len(segs)}세그먼트 {total_chars}자 → {os.path.relpath(path, BASE)}")
        ok += 1
    if ok == 0:
        print("자막을 받은 영상이 없습니다. 다른 채널을 시도하세요.")
        sys.exit(1)


if __name__ == "__main__":
    main()
