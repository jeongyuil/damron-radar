"""채널 후보 실사 스크립트 — YouTube Data API로 §3.1 기준 실측.

사용법:
    1. .env에 YOUTUBE_API_KEY 설정 (Google Cloud Console → YouTube Data API v3 활성화 → API 키)
    2. uv run python scripts/audit_channels.py
    3. 결과: data/channels_audit_<날짜>.csv — 사람이 검토 후 candidates CSV에 반영

측정 항목 (§3.1 기준 매핑):
    - 구독자 수 (실측)               → 기준_규모 (10만+)
    - 최근 30일 업로드 수            → 기준_활동성 (주 2회+ = 30일 9회+)
    - 최근 30일 영상 평균 조회수      → 기준_규모 보조 (5만+)
    - 최근 30일 영상 제목 목록        → 기준_주제적합 판정 보조 (사람이 확인)
    - 최근 업로드 일시               → 활동 여부

쿼터: 채널당 ~3 units (channels.list 1 + playlistItems.list 1 + videos.list 1).
      전체 후보 ~59곳 = 약 200 units (일일 무료 쿼터 10,000의 2%).
주의: search.list(100 units)는 사용하지 않는다 — 쿼터 원칙(§4.1).
      channel_id가 없는 행은 handle로 해석 시도, 실패 시 '수동확인' 표기.
"""
from __future__ import annotations

import csv
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
if not API_KEY:
    sys.exit("YOUTUBE_API_KEY가 없습니다. .env를 확인하세요 (.env.example 참조)")

BASE = "https://www.googleapis.com/youtube/v3"
SRC = ROOT / "data" / "channels_candidates_v1.csv"
OUT = ROOT / "data" / f"channels_audit_{datetime.now():%Y%m%d}.csv"
WINDOW_DAYS = 30


def extract_channel_ref(row: dict) -> tuple[str, str]:
    """CSV 행에서 (종류, 값) 반환 — ('id', UC...) 또는 ('handle', @...) 또는 ('', '')."""
    field = row.get("channel_id/URL", "") or ""
    m = re.search(r"(UC[0-9A-Za-z_-]{22})", field)
    if m:
        return "id", m.group(1)
    m = re.search(r"youtube\.com/(@[^\s/|]+)", field)
    if m:
        return "handle", m.group(1)
    return "", ""


def api(path: str, **params) -> dict:
    params["key"] = API_KEY
    r = httpx.get(f"{BASE}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def audit_channel(kind: str, ref: str) -> dict | None:
    """채널 1곳 실측. 실패 시 None."""
    params = {"part": "snippet,statistics,contentDetails"}
    params["id" if kind == "id" else "forHandle"] = ref
    items = api("channels", **params).get("items", [])
    if not items:
        return None
    ch = items[0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]

    # 최근 업로드 50개 (30일 창 커버에 대부분 충분; 다작 채널은 하한 추정치로 명시)
    pl = api("playlistItems", part="contentDetails,snippet", playlistId=uploads, maxResults=50)
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    recent = [
        it for it in pl.get("items", [])
        if datetime.fromisoformat(it["contentDetails"]["videoPublishedAt"].replace("Z", "+00:00")) >= cutoff
    ]
    truncated = len(pl.get("items", [])) == 50 and len(recent) == 50  # 50개가 전부 30일 내 → 실제는 더 많음

    views = []
    if recent:
        ids = ",".join(it["contentDetails"]["videoId"] for it in recent[:50])
        vids = api("videos", part="statistics", id=ids).get("items", [])
        views = [int(v["statistics"].get("viewCount", 0)) for v in vids]

    n = len(recent)
    latest = pl["items"][0]["contentDetails"]["videoPublishedAt"] if pl.get("items") else ""
    return {
        "채널명(API)": ch["snippet"]["title"],
        "channel_id": ch["id"],
        "구독자_실측": int(ch["statistics"].get("subscriberCount", 0)),
        "30일_업로드수": f"{n}{'+' if truncated else ''}",
        "30일_평균조회수": round(sum(views) / len(views)) if views else 0,
        "최근_업로드": latest[:10],
        "판정_규모": "Y" if int(ch["statistics"].get("subscriberCount", 0)) >= 100_000
                     or (views and sum(views) / len(views) >= 50_000) else "N",
        "판정_활동성": "Y" if n >= 9 or truncated else ("N" if latest and latest[:10] < (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d") else "경계"),
        "최근_제목_샘플": " | ".join(it["snippet"]["title"][:40] for it in recent[:8]),
    }


def main() -> None:
    with open(SRC, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    targets = [r for r in rows if r["통과여부(통과|탈락|예외등재)"] in ("통과", "보류")]
    print(f"실사 대상: {len(targets)}곳 (통과+보류)")

    results = []
    for r in targets:
        kind, ref = extract_channel_ref(r)
        base = {"채널명": r["채널명"], "기존판정": r["통과여부(통과|탈락|예외등재)"]}
        if not ref:
            results.append({**base, "실사결과": "수동확인 필요 — channel_id/handle 없음"})
            print(f"  ⚠️  {r['채널명']}: 식별자 없음 — 수동 확인")
            continue
        try:
            data = audit_channel(kind, ref)
            if data is None:
                results.append({**base, "실사결과": f"API 조회 실패({ref})"})
                print(f"  ❌ {r['채널명']}: 조회 실패")
            else:
                results.append({**base, "실사결과": "OK", **data})
                print(f"  ✅ {r['채널명']}: 구독 {data['구독자_실측']:,} / 30일 {data['30일_업로드수']}편 / 평균 {data['30일_평균조회수']:,}회")
        except httpx.HTTPStatusError as e:
            results.append({**base, "실사결과": f"HTTP {e.response.status_code}"})
            print(f"  ❌ {r['채널명']}: {e}")

    all_keys: list[str] = []
    for r in results:
        for k in r:
            if k not in all_keys:
                all_keys.append(k)
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=all_keys)
        w.writeheader()
        w.writerows(results)
    print(f"\n결과 저장: {OUT}")
    print("다음 단계: 결과 검토 → channels_candidates CSV의 기준_규모/활동성 컬럼 갱신 → 주제적합은 제목 샘플로 판정")


if __name__ == "__main__":
    main()
