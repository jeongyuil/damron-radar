"""구조화 A/B 평가 하네스 — 같은 전사를 두 백엔드로 구조화해 비교한다.

목적 (PRD §9 / D3): "Haiku 4.5(기본안) vs 로컬 Qwen3.6-27B(무료)" 을
프롬프트 v1 + 청킹 스펙 v1 그대로 적용해 일치율·품질을 측정.
골든셋(W7~9) 전까지는 모델 간 일치율 + 육안 점검 리포트가 산출물.

백엔드:
    local  — mlx_lm.server (OpenAI 호환, 127.0.0.1:8080). 비용 0
    haiku  — claude-haiku-4-5 (ANTHROPIC_API_KEY 필요, .env 또는 환경변수)

사용:
    python scripts/eval_ab_structurer.py run   <transcript.json> --backend local
    python scripts/eval_ab_structurer.py run   <transcript.json> --backend haiku
    python scripts/eval_ab_structurer.py compare <out_a.json> <out_b.json>

출력: eval_data/structured_<backend>_<video_id>.json (원본 비저장 원칙 — 커밋 금지)
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "eval_data")
PROMPT_PATH = os.path.join(BASE, "prompts", "structurer", "extract_utterances_v1.md")
ISSUES_CSV = os.path.join(BASE, "data", "seeds", "issues_seed_v0.csv")
ENTITIES_CSV = os.path.join(BASE, "data", "seeds", "entities_seed_v0.csv")

# 청킹 스펙 v1: ~8K 토큰 ≈ 한국어 16,000자, 오버랩 60초
CHUNK_CHARS = 16000
OVERLAP_MS = 60_000

LOCAL_URL = os.environ.get("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
LOCAL_MODEL = os.environ.get("LOCAL_LLM_MODEL", "lmstudio-community/Qwen3.6-27B-MLX-6bit")
HAIKU_MODEL = "claude-haiku-4-5"


# ---------- 프롬프트 구성 ----------

def load_seeds():
    import csv
    with open(ISSUES_CSV, encoding="utf-8-sig") as f:
        issues = "\n".join(
            f"- {r['issue_slug']}: {r['이슈명']} ({r['정의(논쟁 단위)'][:60]})"
            for r in csv.DictReader(f) if r.get("status") == "active")
    with open(ENTITIES_CSV, encoding="utf-8-sig") as f:
        entities = "\n".join(
            f"- {r['canonical_name']} [{r['kind']}] 별칭: {r.get('aliases(;구분)','')}"
            for r in csv.DictReader(f))
    return issues, entities


def load_prompt_template():
    """프롬프트 v1 파일에서 시스템/사용자 템플릿 추출 (파일은 수정하지 않는다)."""
    text = open(PROMPT_PATH, encoding="utf-8").read()
    sys_part = text.split("## 시스템 프롬프트", 1)[1].split("## 사용자 메시지", 1)[0]
    sys_part = sys_part.split("\n", 1)[1]  # 헤더 잔여 제거
    user_part = text.split("## 사용자 메시지", 1)[1].split("## 출력 스키마", 1)[0]
    user_part = user_part.split("\n", 1)[1]
    schema_note = (
        "\n\n출력은 아래 형태의 JSON 하나만 출력하세요. 설명·마크다운 금지.\n"
        '{"utterances": [{"start_ms": 0, "end_ms": 0, "speaker_label": "host|guest|unknown|인명",'
        ' "quote_excerpt": "원문 인용 140자 이내", "summary": "재서술 요지",'
        ' "type": "fact|claim|opinion", "sentiment": "positive|negative|neutral",'
        ' "stance_score": -2, "targets": [{"name": "정규명", "role": "subject|object"}],'
        ' "issues": [{"slug": "issue_slug", "confidence": 0.9}],'
        ' "verifiable_points": ["..."], "confidence": 0.9}]}'
    )
    return sys_part.strip() + schema_note, user_part.strip()


def make_chunks(segments):
    """세그먼트를 청킹 스펙대로 분할. [(own_start, own_end, text)] 반환."""
    chunks, cur, cur_len, own_start = [], [], 0, 0
    for seg in segments:
        line = f"[{seg['start_ms']}ms] {seg['text']}"
        if cur_len + len(line) > CHUNK_CHARS and cur:
            own_end = seg["start_ms"]
            chunks.append((own_start, own_end, cur))
            # 오버랩: own_end - 60초 이후 세그먼트를 다음 청크 앞에 포함
            cur = [s for s in cur if s["start_ms"] >= own_end - OVERLAP_MS]
            cur_len = sum(len(f"[{s['start_ms']}ms] {s['text']}") for s in cur)
            own_start = own_end
        cur.append(seg)
        cur_len += len(line)
    if cur:
        chunks.append((own_start, segments[-1]["end_ms"] + 1, cur))
    return chunks


def build_messages(tr, chunk_idx, chunk_total, own_start, own_end, segs,
                   sys_prompt, user_tmpl, issues, entities):
    sys_full = sys_prompt.replace("{{ISSUES}}", issues).replace("{{ENTITIES}}", entities)
    meta = f"title: {tr['title']}\nchannel_id: {tr['channel_id']}\npublished: {tr['published']}"
    body = "\n".join(f"[{s['start_ms']}ms] {s['text']}" for s in segs)
    user = (user_tmpl
            .replace("{{VIDEO_META}}", meta)
            .replace("{{CHUNK_INDEX}}", str(chunk_idx)).replace("{{CHUNK_TOTAL}}", str(chunk_total))
            .replace("{{OWN_START_MS}}", str(own_start)).replace("{{OWN_END_MS}}", str(own_end))
            .replace("{{PREV_CONTEXT}}", "(없음)")
            .replace("{{TRANSCRIPT_CHUNK}}", body))
    return sys_full, user


# ---------- 백엔드 호출 ----------

def call_local(sys_prompt, user_msg, thinking=False):
    payload = {"model": LOCAL_MODEL,
               "messages": [{"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user_msg}],
               "max_tokens": 8192, "temperature": 0.1,
               "chat_template_kwargs": {"enable_thinking": thinking}}
    req = urllib.request.Request(LOCAL_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        d = json.load(r)
    return d["choices"][0]["message"]["content"], d.get("usage", {})


def call_haiku(sys_prompt, user_msg):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        env = os.path.join(BASE, ".env")
        if os.path.exists(env):
            for line in open(env):
                if line.startswith("ANTHROPIC_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("ANTHROPIC_API_KEY가 없습니다 (.env 또는 환경변수).")
    payload = {"model": HAIKU_MODEL, "max_tokens": 8192,
               "system": sys_prompt,
               "messages": [{"role": "user", "content": user_msg}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "x-api-key": key,
                                          "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.load(r)
    text = "".join(b.get("text", "") for b in d.get("content", []))
    return text, d.get("usage", {})


def parse_utterances(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0)).get("utterances", [])
    except json.JSONDecodeError:
        return None


# ---------- 실행 ----------

def run(args):
    tr = json.load(open(args.transcript, encoding="utf-8"))
    issues, entities = load_seeds()
    sys_prompt, user_tmpl = load_prompt_template()
    chunks = make_chunks(tr["segments"])
    print(f"영상: {tr['title'][:50]} | 청크 {len(chunks)}개 | 백엔드 {args.backend}")

    all_utts, total_usage, t0 = [], {}, time.time()
    for i, (own_start, own_end, segs) in enumerate(chunks, 1):
        sp, um = build_messages(tr, i, len(chunks), own_start, own_end, segs,
                                sys_prompt, user_tmpl, issues, entities)
        t1 = time.time()
        if args.backend == "local":
            text, usage = call_local(sp, um, thinking=args.thinking)
        else:
            text, usage = call_haiku(sp, um)
        utts = parse_utterances(text)
        if utts is None:
            print(f"  청크 {i}: ⚠️ JSON 파싱 실패 (원문 {len(text)}자)")
            continue
        # 병합 규칙(스펙 §4): 책임 구간 밖 발언 폐기
        kept = [u for u in utts if own_start <= u.get("start_ms", -1) < own_end]
        all_utts.extend(kept)
        for k, v in usage.items():
            if isinstance(v, int):
                total_usage[k] = total_usage.get(k, 0) + v
        print(f"  청크 {i}/{len(chunks)}: 발언 {len(kept)}개 (원출력 {len(utts)}) | {time.time()-t1:.0f}초")

    out = {"video_id": tr["video_id"], "title": tr["title"],
           "backend": args.backend, "model": LOCAL_MODEL if args.backend == "local" else HAIKU_MODEL,
           "thinking": args.thinking if args.backend == "local" else None,
           "prompt_version": "extract_utterances_v1",
           "elapsed_sec": round(time.time() - t0, 1), "usage": total_usage,
           "utterances": all_utts}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"structured_{args.backend}_{tr['video_id']}.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n총 발언 {len(all_utts)}개 | {out['elapsed_sec']}초 | → {os.path.relpath(path, BASE)}")
    types = {}
    for u in all_utts:
        types[u.get("type", "?")] = types.get(u.get("type", "?"), 0) + 1
    print("유형 분포:", types)


# ---------- 비교 ----------

def overlap_ms(a, b):
    return max(0, min(a["end_ms"], b["end_ms"]) - max(a["start_ms"], b["start_ms"]))


def compare(args):
    A = json.load(open(args.a, encoding="utf-8"))
    B = json.load(open(args.b, encoding="utf-8"))
    ua, ub = A["utterances"], B["utterances"]
    print(f"A: {A['backend']}({A.get('model')}) 발언 {len(ua)}개 | "
          f"B: {B['backend']}({B.get('model')}) 발언 {len(ub)}개")

    # 시간 겹침 기반 그리디 매칭
    pairs, used_b = [], set()
    for a in ua:
        best, best_ov = None, 0
        for j, b in enumerate(ub):
            if j in used_b:
                continue
            ov = overlap_ms(a, b)
            if ov > best_ov:
                best, best_ov = j, ov
        dur = max(1, a["end_ms"] - a["start_ms"])
        if best is not None and best_ov / dur >= 0.3:
            pairs.append((a, ub[best]))
            used_b.add(best)

    n = len(pairs)
    recall_a = n / len(ua) if ua else 0
    recall_b = n / len(ub) if ub else 0
    type_agree = sum(1 for a, b in pairs if a.get("type") == b.get("type"))
    sent_agree = sum(1 for a, b in pairs if a.get("sentiment") == b.get("sentiment"))
    stance_pairs = [(a, b) for a, b in pairs
                    if a.get("stance_score") is not None and b.get("stance_score") is not None]
    stance_mae = (sum(abs(a["stance_score"] - b["stance_score"]) for a, b in stance_pairs)
                  / len(stance_pairs)) if stance_pairs else None

    print(f"\n매칭 발언 쌍: {n} (A 커버 {recall_a:.0%} / B 커버 {recall_b:.0%})")
    if n:
        print(f"type 일치율: {type_agree/n:.0%}  (게이트 참고선 85%)")
        print(f"sentiment 일치율: {sent_agree/n:.0%}")
    if stance_mae is not None:
        print(f"stance MAE: {stance_mae:.2f} (0=완전일치, 척도 -2~+2)")

    # 육안 점검용 마크다운
    md = [f"# 구조화 A/B 비교 — {A['title'][:60]}",
          f"- A: {A['backend']} ({A.get('model')}) — {len(ua)}발언, {A.get('elapsed_sec')}초",
          f"- B: {B['backend']} ({B.get('model')}) — {len(ub)}발언, {B.get('elapsed_sec')}초",
          f"- 매칭 {n}쌍 | type 일치 {type_agree}/{n} | sentiment 일치 {sent_agree}/{n}", ""]
    for i, (a, b) in enumerate(pairs[:60], 1):
        mark = "" if a.get("type") == b.get("type") else " ⚠️ type 불일치"
        md.append(f"## 쌍 {i} [{a['start_ms']//1000}s]{mark}")
        md.append(f"- **A** [{a.get('type')}/{a.get('sentiment')}/stance {a.get('stance_score')}] {a.get('summary','')}")
        md.append(f"- **B** [{b.get('type')}/{b.get('sentiment')}/stance {b.get('stance_score')}] {b.get('summary','')}")
        md.append("")
    path = os.path.join(OUT_DIR, f"compare_{A['video_id']}.md")
    open(path, "w", encoding="utf-8").write("\n".join(md))
    print(f"\n육안 점검 리포트 → {os.path.relpath(path, BASE)}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("transcript")
    r.add_argument("--backend", choices=["local", "haiku"], default="local")
    r.add_argument("--thinking", action="store_true", help="로컬 백엔드 추론 모드")
    r.set_defaults(fn=run)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.set_defaults(fn=compare)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
