"""골든셋 채점 — 사람 라벨(golden) 대비 각 모델 출력의 정확도 산출.

사용:
    python scripts/score_golden.py <golden.json> <structured_1.json> [structured_2.json ...]

PRD §1 게이트: 발언 유형(type) 일치율 85% / 입장 방향 90%.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_ab_structurer import overlap_ms


def score(golden, out):
    utts = out["utterances"]
    n_type = n_type_ok = n_sent = n_sent_ok = 0
    stance_pairs = []
    dir_ok = dir_n = 0
    for g in golden["labels"]:
        if not g.get("type"):
            continue
        # 골든 구간과 가장 겹치는 발언 매칭
        best, best_ov = None, 0
        for u in utts:
            ov = overlap_ms(g, u)
            if ov > best_ov:
                best, best_ov = u, ov
        if best is None or best_ov / max(1, g["end_ms"] - g["start_ms"]) < 0.3:
            continue
        n_type += 1
        n_type_ok += (best.get("type") == g["type"])
        if g.get("sentiment"):
            n_sent += 1
            n_sent_ok += (best.get("sentiment") == g["sentiment"])
        gs, ms = g.get("stance_score"), best.get("stance_score")
        if gs is not None and ms is not None:
            stance_pairs.append(abs(gs - ms))
            if gs != 0:
                dir_n += 1
                dir_ok += (gs * ms > 0)  # 방향(부호) 일치
    return {
        "matched": n_type,
        "type_acc": n_type_ok / n_type if n_type else None,
        "sentiment_acc": n_sent_ok / n_sent if n_sent else None,
        "stance_mae": sum(stance_pairs) / len(stance_pairs) if stance_pairs else None,
        "stance_dir_acc": dir_ok / dir_n if dir_n else None,
    }


def main():
    golden = json.load(open(sys.argv[1], encoding="utf-8"))
    n_labeled = sum(1 for x in golden["labels"] if x.get("type"))
    print(f"골든셋: {golden['video_id']} — 라벨 {n_labeled}건 (라벨러: {golden.get('labeler')})\n")
    print(f"{'모델':<45} {'매칭':>4} {'type':>7} {'sent':>7} {'stanceMAE':>10} {'방향':>7}")
    for path in sys.argv[2:]:
        out = json.load(open(path, encoding="utf-8"))
        s = score(golden, out)
        fmt = lambda v, pct=True: ("—" if v is None else (f"{v:.0%}" if pct else f"{v:.2f}"))
        name = f"{out['backend']}" + (" (thinking)" if out.get("thinking") else "")
        print(f"{name:<45} {s['matched']:>4} {fmt(s['type_acc']):>7} {fmt(s['sentiment_acc']):>7}"
              f" {fmt(s['stance_mae'], pct=False):>10} {fmt(s['stance_dir_acc']):>7}")
    print("\n게이트 참고선(PRD §1): type 85% / 입장 방향 90%")


if __name__ == "__main__":
    main()
