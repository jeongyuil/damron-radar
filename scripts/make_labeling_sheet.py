"""골든셋 라벨링 시트 생성 — 매칭된 발언 쌍을 블라인드 라벨링용 HTML로 변환.

- 모델 출력(type 등)은 보여주지 않는다 (앵커링 편향 방지)
- 각 항목: 유튜브 타임스탬프 링크 + 해당 구간 전사 원문 + 라벨 폼
- [내보내기] 버튼 → golden_<video_id>.json 다운로드

사용:
    python scripts/make_labeling_sheet.py <transcript.json> <structured_a.json> <structured_b.json> [--labeler 이름]
"""
import argparse
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_ab_structurer import overlap_ms, OUT_DIR, BASE

CRITERIA_HTML = """
<details open class="criteria">
<summary><b>📐 판단 기준 (PRD §6.2 + 판례) — 라벨링 전 필독</b></summary>
<p><b>결정 트리</b>: ① 참/거짓 판별이 <i>원리상</i> 가능한가? 아니오 → <b>opinion</b>
② 가능하다면, 화자가 근거(수치·출처·일시)를 제시했나? 예 → <b>fact</b>, 아니오 → <b>claim</b></p>
<table border="1" cellpadding="6" style="border-collapse:collapse">
<tr><th>상황</th><th>판정</th><th>이유</th></tr>
<tr><td>전언 — "~라고 캠프에서 말해요"</td><td><b>claim</b></td><td>전달된 명제는 검증 가능하나 화자 본인의 근거 없음. "실제 있었던 발언"이라는 사실성과 무관 — 기준은 <b>근거 제시 여부</b></td></tr>
<tr><td>수치 인용 — "392조원 투자된다고 해요"</td><td><b>claim</b></td><td>수치가 있어도 출처 미제시면 claim. "정부 발표에 따르면 392조"면 fact</td></tr>
<tr><td>관찰 가능한 행동 기술 — "공격 모드로 전환했다"</td><td><b>claim</b></td><td>검증 가능한 행동이나 근거 없음. 단 "전환은 판세가 뒤집혔다는 신호다"처럼 해석이 핵심이면 opinion</td></tr>
<tr><td>예측 — "서울시장 선거가 시험대가 될 것"</td><td><b>opinion</b></td><td>미래는 검증 불가</td></tr>
<tr><td>평가 — "품격이 국민 보기 어떨까"</td><td><b>opinion</b></td><td>가치판단</td></tr>
<tr><td>fact+opinion 혼재</td><td>핵심 기능 쪽</td><td>발언의 주된 기능이 정보 전달이면 fact/claim, 평가면 opinion</td></tr>
</table>
<p><b>sentiment</b>: 발언 <i>대상</i>에 대한 화자의 감정 (내용의 긍부정 아님).
<b>stance</b>: 관련 <i>이슈</i>에 대한 입장 −2~+2 — 이슈가 찬반 명제가 아니거나 기준점이 모호하면
비워두고(null) 메모에 "극성 앵커 불명"이라 적어주세요 (이슈 정의 개선 안건이 됩니다).</p>
<p>⚠️ 애매하면 억지로 고르지 말고 <b>type을 비운 채 메모에 이유</b>를 남기세요 — 그게 판례집이 됩니다.</p>
</details>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript")
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--labeler", default="유일")
    args = ap.parse_args()
    tr = json.load(open(args.transcript, encoding="utf-8"))
    A = json.load(open(args.a, encoding="utf-8"))
    B = json.load(open(args.b, encoding="utf-8"))
    vid = tr["video_id"]
    labeler = args.labeler

    # compare와 동일한 그리디 시간 매칭
    pairs, used_b = [], set()
    for a in A["utterances"]:
        best, best_ov = None, 0
        for j, b in enumerate(B["utterances"]):
            if j in used_b:
                continue
            ov = overlap_ms(a, b)
            if ov > best_ov:
                best, best_ov = j, ov
        dur = max(1, a["end_ms"] - a["start_ms"])
        if best is not None and best_ov / dur >= 0.3:
            pairs.append((a, B["utterances"][best]))
            used_b.add(best)

    items = []
    for i, (a, b) in enumerate(pairs, 1):
        s_ms = min(a["start_ms"], b["start_ms"])
        e_ms = max(a["end_ms"], b["end_ms"])
        text = " ".join(s["text"] for s in tr["segments"]
                        if s["start_ms"] >= s_ms - 2000 and s["start_ms"] < e_ms + 2000)
        items.append({"idx": i, "start_ms": s_ms, "end_ms": e_ms,
                      "start_s": s_ms // 1000, "transcript": text})

    rows = []
    for it in items:
        mm, ss = divmod(it["start_s"], 60)
        rows.append(f"""
<div class="card" id="card{it['idx']}">
  <h3>#{it['idx']} <a href="https://youtube.com/watch?v={vid}&t={it['start_s']}s" target="_blank">▶ {mm}:{ss:02d}</a>
      <span class="range">({it['start_ms']}ms ~ {it['end_ms']}ms)</span></h3>
  <p class="tx">{html.escape(it['transcript'])}</p>
  <div class="form">
    <b>type</b>
    <label><input type="radio" name="type{it['idx']}" value="fact">fact</label>
    <label><input type="radio" name="type{it['idx']}" value="claim">claim</label>
    <label><input type="radio" name="type{it['idx']}" value="opinion">opinion</label>
    &nbsp;|&nbsp; <b>sentiment</b>
    <label><input type="radio" name="sent{it['idx']}" value="positive">긍정</label>
    <label><input type="radio" name="sent{it['idx']}" value="negative">부정</label>
    <label><input type="radio" name="sent{it['idx']}" value="neutral">중립</label>
    &nbsp;|&nbsp; <b>stance</b>
    <select name="stance{it['idx']}">
      <option value="">null(이슈무관)</option>
      <option value="-2">-2</option><option value="-1">-1</option><option value="0">0</option>
      <option value="1">+1</option><option value="2">+2</option>
    </select>
    <input type="text" name="note{it['idx']}" placeholder="판례 메모 (경계 사례면 이유)" size="40">
  </div>
</div>""")

    doc = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<title>골든셋 라벨링 — {html.escape(tr['title'][:40])}</title>
<style>
 body {{ font-family: -apple-system, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }}
 .card {{ border: 1px solid #ccc; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }}
 .tx {{ background: #f6f6f6; padding: .8rem; border-radius: 6px; line-height: 1.6; }}
 .range {{ color: #999; font-size: .8em; font-weight: normal; }}
 .form label {{ margin-right: .5rem; }}
 #export {{ position: fixed; bottom: 1rem; right: 1rem; padding: .8rem 1.4rem;
            background: #2563eb; color: #fff; border: 0; border-radius: 8px; font-size: 1rem; cursor: pointer; }}
</style></head><body>
<h1>골든셋 라벨링 시트 — 라벨러: {html.escape(labeler)}</h1>
<p><b>{html.escape(tr['title'])}</b><br>
발언 {len(items)}건 — 각 항목의 ▶링크로 실제 발언을 확인하고 라벨을 선택하세요.
<b>다른 라벨러의 답을 보지 말고 독립적으로</b> 판단해주세요 (일치율 측정용).</p>
{CRITERIA_HTML}
{''.join(rows)}
<button id="export" onclick="exportJson()">완료 — JSON 내보내기</button>
<script>
function exportJson() {{
  const items = {json.dumps(items, ensure_ascii=False)};
  const out = items.map(it => {{
    const g = n => (document.querySelector(`[name="${{n}}${{it.idx}}"]:checked`) || {{}}).value || null;
    const stance = document.querySelector(`[name="stance${{it.idx}}"]`).value;
    return {{ idx: it.idx, start_ms: it.start_ms, end_ms: it.end_ms,
             type: g('type'), sentiment: g('sent'),
             stance_score: stance === '' ? null : parseInt(stance),
             note: document.querySelector(`[name="note${{it.idx}}"]`).value }};
  }});
  const missing = out.filter(o => !o.type).map(o => o.idx);
  if (missing.length && !confirm('type 미입력 항목: #' + missing.join(', #') + ' — 그래도 내보낼까요?')) return;
  const blob = new Blob([JSON.stringify({{video_id: "{vid}", labeler: "{labeler}",
    labeled_at: new Date().toISOString(), labels: out}}, null, 1)], {{type: 'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'golden_{vid}_{labeler}.json'; a.click();
}}
</script></body></html>"""

    path = os.path.join(OUT_DIR, f"labeling_{vid}_{labeler}.html")
    open(path, "w", encoding="utf-8").write(doc)
    print(f"라벨링 시트 → {os.path.relpath(path, BASE)} ({len(items)}건, 라벨러: {labeler})")


if __name__ == "__main__":
    main()
