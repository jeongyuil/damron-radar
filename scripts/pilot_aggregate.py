"""파일럿 리포트용 지표 집계 — utterances JSON → 리포트 데이터(JSON).

PRD §8 지표의 파일럿 축약판:
  ② 관점 분포: 이슈 × 채널(축) stance 가중 평균 (발언 3개 미만 = 표본 부족 표시)
  ④ 팩트/주장/의견 비율: 채널별 type 분포
  ⑥ 팩트체크 단서 큐: type=claim & verifiable_points 존재
  + 주요 발언 (이슈별, 인용+타임스탬프 링크)
  + 인용 총량 카운터 (리포트 저작권 §7.3)
"""
import json, glob, sys
from collections import defaultdict
from pathlib import Path

PILOT = Path(__file__).resolve().parent.parent / "transcripts" / "pilot"
manifest = {m["video_id"]: m for m in json.load(open(PILOT / "_manifest.json", encoding="utf-8"))}

utts = []
for f in glob.glob(str(PILOT / "*_utterances.json")):
    d = json.load(open(f, encoding="utf-8"))
    vid = d["video_id"]
    meta = manifest.get(vid, {})
    for u in d["utterances"]:
        u["video_id"] = vid
        u["channel"] = meta.get("channel", "?")
        u["axis"] = meta.get("axis", "?")
        u["url_ts"] = f"https://youtube.com/watch?v={vid}&t={int(u['start_s'])}s"
        utts.append(u)

MAIN_ISSUES = ["전당대회", "정성호·보완수사권", "사법·선거법", "부동산"]

# ② 관점 분포 (이슈 × 채널)
stance_map = defaultdict(lambda: defaultdict(list))
for u in utts:
    if u["issue"] in MAIN_ISSUES and u.get("stance_score") is not None:
        stance_map[u["issue"]][f"{u['channel']}|{u['axis']}"].append(
            (u["stance_score"], u.get("confidence", 0.8)))
perspective = {}
for issue, chans in stance_map.items():
    perspective[issue] = {}
    for ch, pairs in chans.items():
        n = len(pairs)
        wavg = sum(s * c for s, c in pairs) / max(sum(c for _, c in pairs), 1e-9)
        perspective[issue][ch] = {"n": n, "stance": round(wavg, 2), "표본부족": n < 3}

# ④ 유형 분포 (채널별)
type_dist = defaultdict(lambda: {"fact": 0, "claim": 0, "opinion": 0})
for u in utts:
    if u.get("type") in ("fact", "claim", "opinion"):
        type_dist[f"{u['channel']}|{u['axis']}"][u["type"]] += 1

# ⑥ 팩트체크 단서 큐
factcheck = [
    {"channel": u["channel"], "axis": u["axis"], "speaker": u["speaker"],
     "quote": u["quote_excerpt"], "points": u["verifiable_points"],
     "url": u["url_ts"], "issue": u["issue"]}
    for u in utts
    if u.get("type") == "claim" and u.get("verifiable_points") and u.get("confidence", 0) >= 0.7
]
factcheck.sort(key=lambda x: -len(x["points"]))

# 주요 발언 (이슈별 상위 — confidence·stance 강도 기준)
key_utts = defaultdict(list)
for u in sorted(utts, key=lambda x: (-(abs(x.get("stance_score") or 0)), -x.get("confidence", 0))):
    if u["issue"] in MAIN_ISSUES and len(key_utts[u["issue"]]) < 6:
        key_utts[u["issue"]].append({
            "channel": u["channel"], "axis": u["axis"], "speaker": u["speaker"],
            "quote": u["quote_excerpt"], "summary": u["summary"], "type": u["type"],
            "stance": u.get("stance_score"), "url": u["url_ts"]})

# 인용 총량
quote_chars = sum(len(u.get("quote_excerpt", "")) for u in utts)

out = {
    "generated": "2026-07-30",
    "sample": {
        "videos": len(manifest), "utterances": len(utts),
        "channels": sorted({f"{u['channel']}({u['axis']})" for u in utts}),
        "period": "2026-07-24 ~ 2026-07-29",
        "chars_total": sum(m["chars"] for m in manifest.values()),
    },
    "perspective": perspective,
    "type_dist": dict(type_dist),
    "factcheck_queue": factcheck[:10],
    "key_utterances": dict(key_utts),
    "quote_stats": {"total_quote_chars": quote_chars, "avg_per_utterance": round(quote_chars / max(len(utts),1))},
}
with open(PILOT / "_report_data.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"발언 {len(utts)}건 집계 완료 → _report_data.json")
print(f"인용 총량: {quote_chars:,}자 (발언당 평균 {out['quote_stats']['avg_per_utterance']}자)")
for issue, chans in perspective.items():
    print(f"\n[{issue}]")
    for ch, v in sorted(chans.items()):
        flag = " (표본부족)" if v["표본부족"] else ""
        print(f"  {ch:<28} n={v['n']:<3} stance={v['stance']:+.2f}{flag}")
