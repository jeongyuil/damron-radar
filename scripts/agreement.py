"""라벨러 간 일치율(inter-annotator agreement) — 두 golden 파일 비교.

사용:
    python scripts/agreement.py <golden_A.json> <golden_B.json>

출력: type/sentiment/stance 일치율 + 불일치 항목 목록(판례집 안건).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_ab_structurer import OUT_DIR, BASE


def main():
    A = json.load(open(sys.argv[1], encoding="utf-8"))
    B = json.load(open(sys.argv[2], encoding="utf-8"))
    la = {x["idx"]: x for x in A["labels"]}
    lb = {x["idx"]: x for x in B["labels"]}
    common = [i for i in sorted(la) if i in lb
              and la[i].get("type") and lb[i].get("type")]

    t_ok = sum(1 for i in common if la[i]["type"] == lb[i]["type"])
    s_pairs = [i for i in common if la[i].get("sentiment") and lb[i].get("sentiment")]
    s_ok = sum(1 for i in s_pairs if la[i]["sentiment"] == lb[i]["sentiment"])
    st_pairs = [i for i in common
                if la[i].get("stance_score") is not None and lb[i].get("stance_score") is not None]
    st_ok = sum(1 for i in st_pairs
                if abs(la[i]["stance_score"] - lb[i]["stance_score"]) <= 1)

    print(f"라벨러: {A.get('labeler')} vs {B.get('labeler')} — 공통 라벨 {len(common)}건")
    if common:
        print(f"type 일치율: {t_ok}/{len(common)} = {t_ok/len(common):.0%}")
    if s_pairs:
        print(f"sentiment 일치율: {s_ok}/{len(s_pairs)} = {s_ok/len(s_pairs):.0%}")
    if st_pairs:
        print(f"stance ±1 이내 일치: {st_ok}/{len(st_pairs)} = {st_ok/len(st_pairs):.0%}")

    md = [f"# 라벨러 불일치 목록 — 판례집 합의 안건",
          f"{A.get('labeler')} vs {B.get('labeler')} | video {A['video_id']}", ""]
    n_dis = 0
    for i in sorted(set(la) | set(lb)):
        a, b = la.get(i, {}), lb.get(i, {})
        diffs = []
        if a.get("type") != b.get("type"):
            diffs.append(f"type: {a.get('type') or '미정'} vs {b.get('type') or '미정'}")
        if a.get("sentiment") != b.get("sentiment"):
            diffs.append(f"sentiment: {a.get('sentiment')} vs {b.get('sentiment')}")
        if a.get("stance_score") != b.get("stance_score"):
            diffs.append(f"stance: {a.get('stance_score')} vs {b.get('stance_score')}")
        if diffs:
            n_dis += 1
            start_s = (a.get("start_ms") or b.get("start_ms") or 0) // 1000
            md.append(f"## #{i} [{start_s//60}:{start_s%60:02d}] — {' | '.join(diffs)}")
            if a.get("note"):
                md.append(f"- {A.get('labeler')} 메모: {a['note']}")
            if b.get("note"):
                md.append(f"- {B.get('labeler')} 메모: {b['note']}")
            md.append(f"- **합의 결과**: (토론 후 기입) → 판례: ")
            md.append("")
    path = os.path.join(OUT_DIR, f"disagreements_{A['video_id']}.md")
    open(path, "w", encoding="utf-8").write("\n".join(md))
    print(f"\n불일치 {n_dis}건 → 합의 안건 문서: {os.path.relpath(path, BASE)}")
    print("두 분이 이 문서로 토론 → '합의 결과' 칸 기입 → 그게 판례집 v1이 됩니다.")


if __name__ == "__main__":
    main()
