"""골든셋 선발전 — 발언 분류 모델 후보 비교 (type / sentiment / stance_score).

사용법:
    uv run python scripts/golden_bench.py                       # 실행 가능한 후보 전부
    uv run python scripts/golden_bench.py --models qwen3.5-9b-8bit,claude-haiku-4-5
    uv run python scripts/golden_bench.py --add mymodel=http://localhost:8081/v1|org/Model-Name
    uv run python scripts/golden_bench.py --list                # 후보 목록·실행 가능 여부만 출력

입력:
    data/golden_<video_id>_<labeler>.json  — 분석가/엔지니어 라벨 (start_ms/end_ms + 정답)
    transcripts/golden/<video_id>.ko.json3 — YouTube 자동자막(json3). 없으면 yt-dlp로 받는다.
    → transcripts/golden/golden_<video_id>.jsonl 로 결합 (transcript 포함; transcripts/는 비커밋 §7.3)
    이미 transcript가 든 jsonl이 있으면 --jsonl 로 직접 지정.

후보 모델 (CANDIDATES): OpenAI 호환 로컬 서버(mlx_lm.server 등) 또는 Anthropic API.
    Anthropic 후보는 ANTHROPIC_API_KEY 없으면 자동 스킵. 새 후보는 CANDIDATES에 추가하거나 --add.

채점:
    type      accuracy · macro-F1(3클래스: fact/claim/opinion) · macro-F1(2클래스: fact/opinion — 골든셋에
              claim 표본 0건이라 병기. claim 예측은 두 클래스 모두에서 오답 처리)
    sentiment accuracy · macro-F1
    stance    MAE(정답·예측 모두 non-null) · null 일치율 · 방향(부호) 일치율(PRD §1 "입장 방향 정확도")

출력: reports/golden_bench_<video_id>.md (비교 표 + 오답 덤프) + reports/golden_bench_<video_id>_preds.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GOLDEN_DIR = ROOT / "transcripts" / "golden"
REPORT_DIR = ROOT / "reports"

TYPES = ("fact", "claim", "opinion")
SENTIMENTS = ("positive", "negative", "neutral")

# ---------------------------------------------------------------------------
# 후보 모델 레지스트리
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    name: str
    provider: str  # "openai" (OpenAI 호환 chat/completions) | "anthropic"
    model: str
    base_url: str = ""
    max_tokens: int = 512
    extra_body: dict = field(default_factory=dict)  # openai 전용 (chat_template_kwargs 등)
    note: str = ""
    default: bool = True  # False면 --models 로 지명했을 때만 실행

    def runnable(self) -> tuple[bool, str]:
        if self.provider == "anthropic":
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return False, "ANTHROPIC_API_KEY 없음 (.env)"
            return True, ""
        try:
            r = httpx.get(f"{self.base_url}/models", timeout=3)
            ids = [m.get("id") for m in r.json().get("data", [])]
            if ids and self.model not in ids:
                return False, f"서버에 없는 모델 (서빙 중: {ids})"
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, f"서버 응답 없음 {self.base_url} ({type(e).__name__})"


LOCAL = "http://localhost:8080/v1"
QWEN = "lmstudio-community/Qwen3.5-9B-MLX-8bit"

CANDIDATES: list[Candidate] = [
    Candidate(
        "qwen3.5-9b-8bit",
        "openai",
        QWEN,
        LOCAL,
        512,
        {"chat_template_kwargs": {"enable_thinking": False}},
        "현 임시 모델 · thinking off",
    ),
    Candidate(
        "qwen3.5-9b-8bit@think",
        "openai",
        QWEN,
        LOCAL,
        6000,
        {"chat_template_kwargs": {"enable_thinking": True}},
        "동일 모델 · thinking on — 2026-09-09 탈락: 건당 6~24분, 6K 토큰 상한 소진·오답. 지명 시에만 실행",
        default=False,
    ),
    Candidate("claude-haiku-4-5", "anthropic", "claude-haiku-4-5", note="D3 프로덕션 구조화 모델"),
    Candidate(
        "claude-sonnet-5", "anthropic", "claude-sonnet-5", note="D3 검증 모델 (adaptive thinking)"
    ),
]

# ---------------------------------------------------------------------------
# 프롬프트 — PRD §6.2 결정 트리
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """당신은 한국 정치·시사 유튜브 담론 분석가입니다. 주어진 발언 하나(자동자막 전사 — 오탈자·끊김이 있으나 원문 그대로입니다)를 세 축으로 분류합니다.

## 1. type — 발언 유형 (PRD §6.2 결정 트리, 반드시 이 순서로 판단)

① 이 발언의 핵심 명제를 제3자가 참/거짓으로 확인하는 것이 **원리상** 가능한가?
   - 아니오 → "opinion" (가치 판단·평가·예측·전망·해석·심경. 애초에 검증이 성립하지 않음)
   - 예 → ②로
② 화자가 그 명제의 **구체적 근거**(수치·출처·일시·조사·문서·직접 확인한 사실)를 함께 제시했는가?
   - 예 → "fact" (검증 가능한 명제 + 구체적 근거 동반)
   - 아니오 또는 불충분 → "claim" (검증 가능하지만 근거 미제시. "들었다"·"~라고 한다"처럼 전언만 있는 경우도 claim)

| 유형 | 정의 | 판별 질문 | 예시 |
|---|---|---|---|
| fact | 검증 가능한 명제 + 구체적 근거(수치·출처·일시) 동반 | 제3자가 참/거짓을 확인할 수 있고, 화자가 근거를 제시했는가 | "어제 발표된 ○○조사에서 지지율이 3%p 하락했다" |
| claim | 검증 가능한 명제이나 근거 미제시 또는 불충분 | 확인 가능한 명제지만 근거가 없는가 | "이 법안은 사실상 ○○의 요구로 만들어진 것이다" |
| opinion | 가치 판단·예측·해석 (참/거짓 판별 불가) | 애초에 검증이 성립하지 않는가 | "이건 국민을 무시하는 오만한 태도다" |

발언이 여러 문장이면 **화자가 말하려는 중심 명제** 기준으로 하나만 고릅니다. 사실 전달 뒤에 평가·전망이 붙으면 어느 쪽이 발언의 목적인지로 판단합니다.

## 2. sentiment — 발언 대상에 대한 화자의 감정
"positive" | "negative" | "neutral". 대상(인물·정당·정책·상황)을 긍정적으로/부정적으로 평가하는지. 담담한 상황 설명·중계·양비론은 neutral.

## 3. stance_score — 관련 이슈에 대한 화자의 입장
정수 -2(강한 반대) · -1(반대) · 0(중립/유보) · +1(지지) · +2(강한 지지).
[영상 컨텍스트]의 이슈 목록에 있는 이슈 중 이 발언이 다루는 이슈에 대해, 화자가 그 이슈(정책·행위·주체)를 지지/옹호/긍정 전망하는지(+), 반대/비판/부정 전망하는지(-). 발언이 어떤 이슈에도 입장을 드러내지 않으면(순수 상황 설명·질문·이슈 무관) null.

## 출력
JSON 객체 하나만 출력합니다. 설명·마크다운 금지.
{"type": "fact|claim|opinion", "sentiment": "positive|negative|neutral", "stance_score": -2|-1|0|1|2|null, "reason": "판단 근거 한 문장(결정 트리 ①②의 답 포함)"}"""

SYSTEM_PROMPT_V2 = """당신은 한국 정치·시사 유튜브 담론 분석가입니다. 주어진 발언 하나(자동자막 전사 — 오탈자·끊김이 있으나 원문 그대로입니다)를 세 축으로 분류합니다.

## 1. type — 발언 유형 (PRD §6.2 결정 트리, 반드시 이 순서로)

먼저 **중심 명제**를 정합니다. 발언에 사실 전달과 평가가 섞여 있으면 화자가 말하려는 **결론 문장**이 중심 명제입니다. 타인의 발언을 전달하는 경우, 그 발언 사실("X가 이렇게 말했다")이 아니라 화자가 그것을 근거로 무엇을 말하는지가 중심입니다.

① 중심 명제를 제3자가 참/거짓으로 확인하는 것이 **원리상** 가능한가?
   - 아니오 → "opinion". 평가·가치 판단·해석·심경·당위("~해야 한다")·**예측/전망("~할 것이다", "~하겠죠", 인과를 붙인 예측 포함)**·수사 의문·비유.
   - 예 → ②
② 화자가 **근거를 하나라도** 제시했는가? 근거란 다음 중 **하나 이상**입니다:
   (a) 출처·기관·발화자 특정("갤럽 조사", "연합뉴스", "박지원 의원이 말했다")
   (b) 구체 수치·날짜("3.4%", "24일", "247만 표")
   (c) 공개 자료 지칭(SNS 글, 기사 링크, 판결문, 법 조문, 공식 발표, 기자회견)
   (d) 화자의 직접 확인("통화해 보니", "취재해 보니", "제가 확인한 바로는")
   - 예 → "fact". **세 요소가 모두 필요하지 않습니다.** "이재명 대통령이 X에 모건스탠리 기사를 링크했다"는 (c)만으로 fact입니다.
   - 아니오 → "claim". 출처 없는 전언("~라고 한다", "들었다", "얘기가 나온다"), 근거 없는 단정("사실상 ~다", "배후는 ~다"), 화자 스스로 불확실한 수치("65% 됐었나?").

| 유형 | 정의 | 예시 |
|---|---|---|
| fact | 검증 가능한 명제 + 근거 (a)~(d) 중 하나 이상 | "어제 발표된 갤럽 조사에서 지지율이 3%p 하락했다" |
| claim | 검증 가능한 명제이나 근거 없음·불충분 | "이 법안은 사실상 ○○의 요구로 만들어진 것이다" |
| opinion | 참/거짓 판별 불가 (평가·예측·해석·당위) | "이건 국민을 무시하는 오만한 태도다", "금리가 오르면 집값은 폭락할 것이다" |

흔한 실수: 근거가 있는 사실 전달을 "수치·출처·일시가 다 없다"는 이유로 claim으로 내리지 마세요. 반대로, 예측은 근거가 붙어 있어도 opinion입니다.

## 2. sentiment — 발언 대상에 대한 화자의 감정
"positive" | "negative" | "neutral". 담담한 상황 설명·중계·양비론은 neutral.

## 3. stance_score — 발언이 다루는 **대상**에 대한 화자의 찬반
대상 = 이 발언이 평가하는 것(특정 인물의 발언·행위, 정책·법안, 주장). 그 대상을 지지·옹호하면 +, 반대·비판하면 −.
정수 −2(강한 반대, 단정·조롱·"배신") · −1(반대, 온건·조건부) · 0(양쪽 병기·유보) · +1(지지) · +2(강한 지지).
대상이 없는 순수 사실 전달·인사말·질문은 null. [영상 컨텍스트]의 이슈는 대상을 찾는 힌트일 뿐, 부호는 대상 기준입니다.

## 출력
JSON 객체 하나만 출력합니다. 설명·마크다운 금지. reason 안에서는 큰따옴표를 쓰지 마세요.
{"type": "fact|claim|opinion", "sentiment": "positive|negative|neutral", "stance_score": -2|-1|0|1|2|null, "reason": "중심 명제 + ①②의 답 + stance 대상, 한 문장"}"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(TYPES)},
        "sentiment": {"type": "string", "enum": list(SENTIMENTS)},
        "stance_score": {"type": ["integer", "null"], "enum": [-2, -1, 0, 1, 2, None]},
        "reason": {"type": "string"},
    },
    "required": ["type", "sentiment", "stance_score", "reason"],
    "additionalProperties": False,
}


DICT_DIR = ROOT / "data" / "dictionaries"
DICT_BLOCK = ""  # --with-dict 시 채워짐


def load_dict_block(issue_slugs: list[str]) -> str:
    """판례집 규칙(R1~R14) + 이슈별 stance 부호 규칙을 시스템 프롬프트 뒤에 붙일 블록."""
    import csv

    lines = ["", "## 판례 규칙 (data/dictionaries/type_decision_book_v0.md §1)"]
    book = (DICT_DIR / "type_decision_book_v0.md").read_text(encoding="utf-8")
    sec = book.split("## 1. 규칙")[1].split("## 2. 사례")[0]
    for row in sec.splitlines():
        if row.startswith("| R"):
            cells = [c.strip() for c in row.strip("|").split("|")]
            lines.append(f"- {cells[0]}: {cells[1]} → {cells[2]}")
    lines += ["", "## 이슈별 stance 부호 (data/dictionaries/stance_polarity_v0.csv)"]
    for r in csv.DictReader((DICT_DIR / "stance_polarity_v0.csv").open(encoding="utf-8")):
        if r["issue_slug"] in issue_slugs:
            plus = r["plus_means(+1 지지 / +2 강한 지지)"]
            minus = r["minus_means(-1 반대 / -2 강한 반대)"]
            lines.append(f"- {r['reference']}: + = {plus} / − = {minus}")
    lines.append("- 위 이슈 어느 것에도 입장을 드러내지 않으면 null.")
    return "\n".join(lines)


PROMPT_VERSION = "v1"


def system_prompt() -> str:
    base = SYSTEM_PROMPT_V2 if PROMPT_VERSION == "v2" else SYSTEM_PROMPT
    return base + DICT_BLOCK


def prompt_sha() -> str:
    import hashlib

    return hashlib.sha256(system_prompt().encode()).hexdigest()[:12]


def user_message(item: dict) -> str:
    ctx = item.get("context", {})
    lines = ["[영상 컨텍스트]"]
    if ctx.get("title"):
        lines.append(f"제목: {ctx['title']}")
    if ctx.get("channel"):
        lines.append(f"채널: {ctx['channel']}")
    if ctx.get("issues"):
        lines.append("이슈 목록: " + " / ".join(ctx["issues"]))
    lines.append("")
    lines.append(f"[발언 #{item['idx']} — {item['start_ms'] // 1000}s~{item['end_ms'] // 1000}s]")
    lines.append(item["transcript"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 골든셋 materialize (labels + captions → jsonl)
# ---------------------------------------------------------------------------


def ensure_captions(video_id: str) -> Path:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for cand in (GOLDEN_DIR / f"{video_id}.ko.json3", GOLDEN_DIR / f"{video_id}.ko-orig.json3"):
        if cand.exists():
            return cand
    print(f"  자막 없음 → yt-dlp로 다운로드: {video_id}")
    cmd = [
        "yt-dlp",
        "--no-simulate",
        "--skip-download",
        "--write-auto-subs",
        "--write-subs",
        "--sub-langs",
        "ko,ko-orig",
        "--sub-format",
        "json3",
        "-q",
        "-o",
        str(GOLDEN_DIR / f"{video_id}.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    subprocess.run(cmd, check=True)
    for cand in (GOLDEN_DIR / f"{video_id}.ko.json3", GOLDEN_DIR / f"{video_id}.ko-orig.json3"):
        if cand.exists():
            return cand
    sys.exit("자막 다운로드 실패 — transcripts/golden/<video_id>.ko.json3 을 수동으로 넣어주세요")


def video_meta(video_id: str) -> dict:
    """yt-dlp로 제목·채널만 (API 쿼터 0). 실패해도 진행."""
    try:
        out = subprocess.run(
            [
                "yt-dlp",
                "--print",
                "%(title)s\t%(channel)s\t%(upload_date)s",
                "--skip-download",
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip()
        title, channel, upload = out.split("\t")
        return {"title": title, "channel": channel, "upload_date": upload}
    except Exception:  # noqa: BLE001
        return {}


def caption_text(events: list[dict], start_ms: int, end_ms: int) -> str:
    parts = []
    for e in events:
        if not e.get("segs"):
            continue
        if start_ms <= e["tStartMs"] < end_ms:
            parts.append("".join(s.get("utf8", "") for s in e["segs"]).replace("\n", " "))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def materialize(golden_json: Path, issues: list[str]) -> Path:
    g = json.loads(golden_json.read_text(encoding="utf-8"))
    vid = g["video_id"]
    out = GOLDEN_DIR / f"golden_{vid}.jsonl"
    cap = ensure_captions(vid)
    events = json.loads(cap.read_text(encoding="utf-8"))["events"]
    meta = video_meta(vid)
    ctx = {**meta, "issues": issues}
    with out.open("w", encoding="utf-8") as f:
        for lab in g["labels"]:
            row = {
                "video_id": vid,
                "labeler": g.get("labeler"),
                "idx": lab["idx"],
                "start_ms": lab["start_ms"],
                "end_ms": lab["end_ms"],
                "transcript": caption_text(events, lab["start_ms"], lab["end_ms"]),
                "gold": {
                    "type": lab["type"],
                    "sentiment": lab["sentiment"],
                    "stance_score": lab["stance_score"],
                },
                "note": lab.get("note", ""),
                "context": ctx,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  골든셋 jsonl 생성: {out.relative_to(ROOT)} ({len(g['labels'])}건, 자막 {cap.name})")
    return out


# ---------------------------------------------------------------------------
# 모델 호출
# ---------------------------------------------------------------------------


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        # 폴백: reason 안의 따옴표 등으로 JSON이 깨진 경우 필드만 정규식으로 회수
        f = {}
        for key, pat in (
            ("type", r'"type"\s*:\s*"(fact|claim|opinion)"'),
            ("sentiment", r'"sentiment"\s*:\s*"(positive|negative|neutral)"'),
            ("stance_score", r'"stance_score"\s*:\s*(-?[0-2]|null)'),
            ("reason", r'"reason"\s*:\s*"(.*?)"?\s*\}?\s*$'),
        ):
            mm = re.search(pat, text, flags=re.DOTALL)
            if mm:
                f[key] = None if mm.group(1) == "null" else mm.group(1)
        if "type" in f and "sentiment" in f:
            f.setdefault("stance_score", None)
            f["reason"] = (f.get("reason") or "")[:300] + " [json-repair]"
            return f
        raise


def call_openai_compat(c: Candidate, item: dict) -> tuple[dict, dict]:
    body = {
        "model": c.model,
        "temperature": 0,
        "max_tokens": c.max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user_message(item)},
        ],
        **c.extra_body,
    }
    r = httpx.post(f"{c.base_url}/chat/completions", json=body, timeout=600)
    r.raise_for_status()
    d = r.json()
    msg = d["choices"][0]["message"]
    content = msg.get("content") or ""
    if not content.strip() and msg.get("reasoning"):
        # thinking이 max_tokens를 다 먹은 경우 — 답이 없다
        raise ValueError(
            f"content 비어 있음 (finish={d['choices'][0].get('finish_reason')}, thinking만 출력)"
        )
    usage = d.get("usage", {})
    return parse_json(content), {
        "in": usage.get("prompt_tokens"),
        "out": usage.get("completion_tokens"),
    }


def call_anthropic(c: Candidate, item: dict) -> tuple[dict, dict]:
    import anthropic  # 지연 import — 키 없는 환경에서도 스크립트가 뜨도록

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=c.model,
        max_tokens=max(c.max_tokens, 2000),
        system=system_prompt(),
        messages=[{"role": "user", "content": user_message(item)}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        raise ValueError("refusal")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text), {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}


def normalize(pred: dict) -> dict:
    t = str(pred.get("type", "")).strip().lower()
    s = str(pred.get("sentiment", "")).strip().lower()
    st = pred.get("stance_score")
    if isinstance(st, str):
        st = None if st.lower() in ("null", "none", "") else int(float(st))
    if st is not None:
        st = max(-2, min(2, round(float(st))))
    return {
        "type": t if t in TYPES else f"?{t}",
        "sentiment": s if s in SENTIMENTS else f"?{s}",
        "stance_score": st,
        "reason": str(pred.get("reason", ""))[:300],
    }


def run_candidate(c: Candidate, items: list[dict], retries: int = 2) -> list[dict]:
    preds = []
    for it in items:
        t0 = time.time()
        err, pred, usage = None, None, {}
        for attempt in range(retries + 1):
            try:
                raw, usage = (call_anthropic if c.provider == "anthropic" else call_openai_compat)(
                    c, it
                )
                pred = normalize(raw)
                break
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:200]
                if attempt < retries:
                    time.sleep(1.5)
        dt = time.time() - t0
        preds.append(
            {
                "idx": it["idx"],
                "pred": pred,
                "error": err if pred is None else None,
                "latency_s": round(dt, 1),
                "usage": usage,
            }
        )
        tag = f"{pred['type']}/{pred['sentiment']}/{pred['stance_score']}" if pred else f"ERR {err}"
        print(f"    #{it['idx']:>2} {dt:5.1f}s  {tag}")
    return preds


# ---------------------------------------------------------------------------
# 채점
# ---------------------------------------------------------------------------


def _sign(x: int) -> int:
    return (x > 0) - (x < 0)


def macro_f1(golds: list[str], preds: list[str], classes: tuple[str, ...]) -> tuple[float, dict]:
    per = {}
    for cl in classes:
        tp = sum(1 for g, p in zip(golds, preds) if g == cl and p == cl)
        fp = sum(1 for g, p in zip(golds, preds) if g != cl and p == cl)
        fn = sum(1 for g, p in zip(golds, preds) if g == cl and p != cl)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per[cl] = {"p": prec, "r": rec, "f1": f1, "support": tp + fn}
    return sum(v["f1"] for v in per.values()) / len(classes), per


def score(items: list[dict], preds: list[dict]) -> dict:
    pm = {p["idx"]: p for p in preds}
    pairs = [(it, pm[it["idx"]]["pred"]) for it in items if pm[it["idx"]]["pred"]]
    n_err = sum(1 for p in preds if p["pred"] is None)
    n = len(pairs)
    if n == 0:
        return {"n": 0, "errors": n_err}

    g_type = [it["gold"]["type"] for it, _ in pairs]
    p_type = [p["type"] for _, p in pairs]
    type_acc = sum(g == p for g, p in zip(g_type, p_type)) / n
    f1_3, per3 = macro_f1(g_type, p_type, TYPES)
    # 2클래스: 골든셋에 존재하는 클래스만 (claim 표본 0건). claim 예측은 양쪽 모두 오답으로 남는다.
    present = tuple(c for c in TYPES if c in g_type)
    f1_2, _ = macro_f1(g_type, p_type, present)
    confusion = {g: {p: 0 for p in TYPES} for g in TYPES}
    for g, p in zip(g_type, p_type):
        if g in TYPES and p in TYPES:
            confusion[g][p] += 1

    g_sent = [it["gold"]["sentiment"] for it, _ in pairs]
    p_sent = [p["sentiment"] for _, p in pairs]
    sent_acc = sum(g == p for g, p in zip(g_sent, p_sent)) / n
    sent_f1, _ = macro_f1(g_sent, p_sent, SENTIMENTS)

    g_st = [it["gold"]["stance_score"] for it, _ in pairs]
    p_st = [p["stance_score"] for _, p in pairs]
    null_match = sum((g is None) == (p is None) for g, p in zip(g_st, p_st)) / n
    both = [(g, p) for g, p in zip(g_st, p_st) if g is not None and p is not None]
    mae = sum(abs(g - p) for g, p in both) / len(both) if both else None
    dir_match = sum(_sign(g) == _sign(p) for g, p in both) / len(both) if both else None
    exact = sum(g == p for g, p in both) / len(both) if both else None

    lat = [p["latency_s"] for p in preds]
    toks_out = [p["usage"].get("out") or 0 for p in preds]
    return {
        "n": n,
        "errors": n_err,
        "type_acc": type_acc,
        "type_f1_3": f1_3,
        "type_f1_2": f1_2,
        "type_classes_2": present,
        "type_per": per3,
        "confusion": confusion,
        "sent_acc": sent_acc,
        "sent_f1": sent_f1,
        "stance_mae": mae,
        "stance_n_both": len(both),
        "stance_null_match": null_match,
        "stance_dir_match": dir_match,
        "stance_exact": exact,
        "latency_avg": sum(lat) / len(lat),
        "latency_total": sum(lat),
        "tokens_out_avg": sum(toks_out) / len(toks_out),
    }


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------


def fmt(x, pct=False):
    if x is None:
        return "—"
    return f"{x * 100:.1f}%" if pct else f"{x:.2f}"


def yt_link(vid: str, ms: int) -> str:
    return f"https://youtube.com/watch?v={vid}&t={ms // 1000}s"


def render(
    items: list[dict], results: dict[str, dict], skipped: dict[str, str], golden_src: Path
) -> str:
    vid = items[0]["video_id"]
    n = len(items)
    gold_types = [it["gold"]["type"] for it in items]
    dist = {t: gold_types.count(t) for t in TYPES}
    L = []
    L.append(f"# 골든셋 선발전 — {vid}")
    L.append("")
    L.append(
        f"- 생성: {datetime.now(UTC).astimezone():%Y-%m-%d %H:%M} · 골든셋: `{golden_src.relative_to(ROOT)}` "
        f"({n}건, 라벨러 {items[0].get('labeler')}) · 영상: {items[0]['context'].get('title', '')}"
    )
    L.append(
        f"- 정답 type 분포: fact {dist['fact']} · claim {dist['claim']} · opinion {dist['opinion']} "
        f"→ **claim 표본 0건**이므로 3클래스 macro-F1은 claim F1=0 고정(예측하면 오답만 발생). 2클래스(fact/opinion) 병기."
    )
    L.append(
        "- 프롬프트: PRD §6.2 결정 트리(①참/거짓 판별 원리상 가능? 아니오→opinion ②근거 제시? 예→fact 아니오→claim) 시스템 프롬프트, temperature 0, 발언 1건씩."
    )
    L.append("- 표본 13건: 1건 차이 = type/sentiment 7.7%p. 순위는 참고용, 결론은 골든셋 확장 후.")
    L.append("")
    L.append("## 모델별 비교")
    L.append("")
    L.append(
        "| 모델 | 비고 | type acc | type F1(3cls) | type F1(2cls) | sent acc | sent F1 | stance MAE (n) | stance null 일치 | stance 방향 일치 | 평균 지연 | 오류 |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, r in results.items():
        s, c = r["score"], r["cand"]
        if s["n"] == 0:
            L.append(f"| {name} | {c.note} | — | — | — | — | — | — | — | — | — | {s['errors']} |")
            continue
        L.append(
            f"| **{name}** | {c.note} | {fmt(s['type_acc'], 1)} | {fmt(s['type_f1_3'])} | {fmt(s['type_f1_2'])} "
            f"| {fmt(s['sent_acc'], 1)} | {fmt(s['sent_f1'])} | {fmt(s['stance_mae'])} ({s['stance_n_both']}) "
            f"| {fmt(s['stance_null_match'], 1)} | {fmt(s['stance_dir_match'], 1)} | {s['latency_avg']:.1f}s | {s['errors']} |"
        )
    for name, why in skipped.items():
        L.append(f"| {name} | 스킵 | — | — | — | — | — | — | — | — | — | {why} |")
    L.append("")
    L.append("PRD §1 게이트: 발언 유형 분류 일치율 85%+, 입장 방향 일치율 90%+.")
    L.append("")

    for name, r in results.items():
        s = r["score"]
        if s["n"] == 0:
            continue
        L.append(f"### {name} — type 혼동 행렬 (행=정답, 열=예측)")
        L.append("")
        L.append("| 정답 \\ 예측 | fact | claim | opinion |")
        L.append("|---|---|---|---|")
        for g in TYPES:
            L.append(f"| {g} | " + " | ".join(str(s["confusion"][g][p]) for p in TYPES) + " |")
        L.append("")

    L.append("## 오답 케이스 덤프")
    L.append("")
    L.append(
        "type·sentiment 불일치 또는 stance 차이 ≥1(null 불일치 포함)인 건. 인용은 200자로 절단(§7.3)."
    )
    L.append("")
    for name, r in results.items():
        pm = {p["idx"]: p for p in r["preds"]}
        L.append(f"### {name}")
        L.append("")
        wrong = 0
        for it in items:
            p = pm[it["idx"]]
            g = it["gold"]
            if p["pred"] is None:
                L.append(f"- **#{it['idx']}** 오류: {p['error']}")
                wrong += 1
                continue
            pr = p["pred"]
            diffs = []
            if pr["type"] != g["type"]:
                diffs.append(f"type {g['type']}→{pr['type']}")
            if pr["sentiment"] != g["sentiment"]:
                diffs.append(f"sentiment {g['sentiment']}→{pr['sentiment']}")
            gs, ps = g["stance_score"], pr["stance_score"]
            if (gs is None) != (ps is None) or (gs is not None and abs(gs - ps) >= 1):
                diffs.append(f"stance {gs}→{ps}")
            if not diffs:
                continue
            wrong += 1
            L.append(
                f"- **#{it['idx']}** [{it['start_ms'] // 1000}s]({yt_link(vid, it['start_ms'])}) — {' · '.join(diffs)}"
            )
            L.append(f"  - 전사: {it['transcript'][:200]}…")
            L.append(f"  - 모델 근거: {pr['reason']}")
            if it.get("note"):
                L.append(f"  - 라벨러 메모: {it['note']}")
        if wrong == 0:
            L.append("- (오답 없음)")
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--golden",
        default=str(ROOT / "data" / "golden_wBbMtXMTU8w_성환.json"),
        help="라벨 JSON (video_id/labels)",
    )
    ap.add_argument("--jsonl", help="transcript가 이미 결합된 jsonl (있으면 --golden 무시)")
    ap.add_argument(
        "--issues",
        default="부동산 세제 개편안(정부 7·3 발표, 종부세·양도세·다주택 기준),민주당 전당대회(정청래·김민석·송영길 당대표 경선)",
        help="영상 컨텍스트 이슈 목록, 쉼표 구분",
    )
    ap.add_argument("--models", help="실행할 후보 이름, 쉼표 구분 (기본: 실행 가능한 전부)")
    ap.add_argument(
        "--add",
        action="append",
        default=[],
        help="OpenAI 호환 후보 추가: name=<base_url>|<model> (예: exaone=http://localhost:8081/v1|LGAI/EXAONE)",
    )
    ap.add_argument("--list", action="store_true", help="후보·실행 가능 여부만 출력")
    ap.add_argument("--rematerialize", action="store_true", help="jsonl 강제 재생성")
    ap.add_argument(
        "--no-merge", action="store_true", help="이전 실행 결과(preds.json)와 병합하지 않음"
    )
    ap.add_argument(
        "--prompt",
        default="v1",
        choices=["v1", "v2"],
        help="시스템 프롬프트 버전 (v2: 근거 요건 완화·예측=opinion·stance 대상 기준 명시). 후보명에 @v2 접미",
    )
    ap.add_argument(
        "--with-dict",
        action="store_true",
        help="판례집 규칙 + stance 부호 규칙을 프롬프트에 주입 (후보명에 +dict 접미)",
    )
    ap.add_argument(
        "--issue-slugs",
        default="real-estate-policy,dp-leadership",
        help="--with-dict 시 주입할 이슈 slug (쉼표)",
    )
    args = ap.parse_args()

    global DICT_BLOCK, PROMPT_VERSION
    PROMPT_VERSION = args.prompt
    if args.prompt != "v1":
        for c in CANDIDATES:
            c.name += f"@{args.prompt}"
            c.note += f" · 프롬프트 {args.prompt}"
    if args.with_dict:
        DICT_BLOCK = load_dict_block([x.strip() for x in args.issue_slugs.split(",")])
        for c in CANDIDATES:
            c.name += "+dict"
            c.note += " · 사전 주입"
    for spec in args.add:
        name, rest = spec.split("=", 1)
        base, model = rest.split("|", 1)
        CANDIDATES.append(Candidate(name, "openai", model, base, 512, {}, "--add"))

    if args.list:
        for c in CANDIDATES:
            ok, why = c.runnable()
            flag = "" if c.default else "  [기본 제외 — --models 로 지명]"
            print(f"  {'✅' if ok else '⏭ '} {c.name:28s} {c.provider:9s} {c.model}  {why}{flag}")
        return

    if args.jsonl:
        jsonl = Path(args.jsonl)
    else:
        golden = Path(args.golden)
        vid = json.loads(golden.read_text(encoding="utf-8"))["video_id"]
        jsonl = GOLDEN_DIR / f"golden_{vid}.jsonl"
        if args.rematerialize or not jsonl.exists():
            print("[1/3] 골든셋 결합")
            materialize(golden, [s.strip() for s in args.issues.split(",") if s.strip()])
    items = [json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    empty = [it["idx"] for it in items if not it.get("transcript")]
    if empty:
        print(f"  ⚠️ transcript 비어 있는 건: {empty}")
    print(f"  골든셋 {len(items)}건 로드: {jsonl}")

    wanted = [s.strip() for s in args.models.split(",")] if args.models else None
    results: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    print("[2/3] 후보 모델 실행")
    for c in CANDIDATES:
        if wanted and c.name not in wanted:
            continue
        if not wanted and not c.default:
            print(f"  ⏭  {c.name}: 기본 실행 제외 ({c.note})")
            continue
        ok, why = c.runnable()
        if not ok:
            print(f"  ⏭  {c.name}: {why}")
            skipped[c.name] = why
            continue
        print(f"  ▶ {c.name} ({c.model})")
        preds = run_candidate(c, items)
        results[c.name] = {"cand": c, "preds": preds, "score": score(items, preds)}
        s = results[c.name]["score"]
        if s["n"]:
            print(
                f"    → type {fmt(s['type_acc'], 1)} · sent {fmt(s['sent_acc'], 1)} · stance MAE {fmt(s['stance_mae'])}"
            )

    if not results:
        sys.exit("실행된 후보가 없습니다 (--list 로 상태 확인)")

    print("[3/3] 리포트")
    vid = items[0]["video_id"]
    REPORT_DIR.mkdir(exist_ok=True)
    md = REPORT_DIR / f"golden_bench_{vid}.md"
    raw = REPORT_DIR / f"golden_bench_{vid}_preds.json"
    # 이전 실행 결과 병합 — 후보를 따로따로 돌려도 표 한 장에 모인다 (같은 골든셋·같은 프롬프트일 때만)
    if raw.exists() and not args.no_merge:
        prev = json.loads(raw.read_text(encoding="utf-8"))
        same = prev.get("golden") == str(jsonl.relative_to(ROOT))
        for name, m in prev.get("models", {}).items():
            if name in results:
                continue
            if not same:
                print(f"  ⚠️ 이전 결과 {name} 폐기 (골든셋 변경)")
                continue
            c = Candidate(
                name,
                m["provider"],
                m["model"],
                note=m.get("note", "").replace(" (이전 실행)", "") + " (이전 실행)",
            )
            results[name] = {
                "cand": c,
                "preds": m["preds"],
                "score": score(items, m["preds"]),
                "prompt_sha": m.get("prompt_sha", ""),
            }
        skipped = {k: v for k, v in skipped.items() if k not in results}
    md.write_text(render(items, results, skipped, jsonl), encoding="utf-8")
    raw = REPORT_DIR / f"golden_bench_{vid}_preds.json"
    raw.write_text(
        json.dumps(
            {
                "generated": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
                "golden": str(jsonl.relative_to(ROOT)),
                "prompt_sha": prompt_sha(),
                "models": {
                    name: {
                        "model": r["cand"].model,
                        "provider": r["cand"].provider,
                        "note": r["cand"].note,
                        "score": {
                            k: v
                            for k, v in r["score"].items()
                            if k not in ("type_per", "confusion")
                        },
                        "preds": r["preds"],
                    }
                    for name, r in results.items()
                },
                "skipped": skipped,
            },
            ensure_ascii=False,
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"  {md.relative_to(ROOT)}\n  {raw.relative_to(ROOT)}")
    print()
    # 콘솔 요약 표
    print(
        f"{'모델':30s} {'type acc':>9s} {'F1(3)':>6s} {'F1(2)':>6s} {'sent acc':>9s} {'st MAE':>7s} {'null일치':>8s} {'방향일치':>8s}"
    )
    for name, r in results.items():
        s = r["score"]
        if s["n"]:
            print(
                f"{name:30s} {fmt(s['type_acc'], 1):>9s} {fmt(s['type_f1_3']):>6s} {fmt(s['type_f1_2']):>6s} "
                f"{fmt(s['sent_acc'], 1):>9s} {fmt(s['stance_mae']):>7s} {fmt(s['stance_null_match'], 1):>8s} {fmt(s['stance_dir_match'], 1):>8s}"
            )


if __name__ == "__main__":
    main()
