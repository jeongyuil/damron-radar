"""골든셋 라벨링 도구 생성 — YouTube 자막을 발언 후보 구간으로 잘라 브라우저 라벨링 페이지를 만든다.

사용법:
    uv run python scripts/golden_template.py MD-vsT5q_5U:election-fraud-allegation EjAL2y164Cc:real-estate-policy
    → transcripts/golden/label_tool_<vid>.html  (브라우저에서 열기. vid 뒤 ':slug'는 issue 기본값)
      드롭다운으로 type/sentiment/stance, 삭제·병합·분할, 자동 저장(localStorage), 임시저장 파일,
      완료 시 골든셋 JSON(golden_<vid>_<라벨러>.json) 다운로드 — transcript 미포함.
    uv run python scripts/golden_template.py --import ~/Downloads/golden_<vid>_<라벨러>.json
    → 값 검증 후 data/ 로 복사. 그다음 golden_bench.py --golden data/golden_<vid>_<라벨러>.json

구간 규칙: 자막 이벤트를 문장 종결(다./요./죠./까?)에서 끊어 20~90초 묶음. 라벨러가 병합·분할·삭제 자유.
기준: PRD §6.2 결정 트리 (①참/거짓 판별 원리상 가능? 아니오→opinion ②근거 제시? 예→fact 아니오→claim)
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = ROOT / "transcripts" / "golden"
MIN_S, MAX_S = 20, 90
ENDER = re.compile(r"(다|요|죠|까|니다|네요|군요|거든요|잖아요)\s*[.?!]\s*$")


def fetch(video_id: str) -> tuple[Path, dict]:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    cap = GOLDEN_DIR / f"{video_id}.ko.json3"
    if not cap.exists():
        subprocess.run(
            [
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
            ],
            check=True,
        )
        alt = GOLDEN_DIR / f"{video_id}.ko-orig.json3"
        if not cap.exists() and alt.exists():
            cap = alt
    meta = {}
    try:
        out = subprocess.run(
            [
                "yt-dlp",
                "--print",
                "%(title)s\t%(channel)s\t%(duration)s\t%(upload_date)s",
                "--skip-download",
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip()
        t, c, d, u = out.split("\t")
        meta = {"title": t, "channel": c, "duration_s": int(d), "upload_date": u}
    except Exception as e:  # noqa: BLE001
        print(f"  메타 조회 실패({type(e).__name__}) — 제목 없이 진행")
    return cap, meta


def segments(cap: Path) -> list[dict]:
    ev = [e for e in json.loads(cap.read_text(encoding="utf-8"))["events"] if e.get("segs")]
    out = []
    for i, e in enumerate(ev):
        text = re.sub(r"\s+", " ", "".join(s.get("utf8", "") for s in e["segs"])).strip()
        if not text:
            continue
        end = (
            ev[i + 1]["tStartMs"] if i + 1 < len(ev) else e["tStartMs"] + e.get("dDurationMs", 3000)
        )
        out.append({"s": e["tStartMs"], "e": end, "t": text})
    return out


def chunk(segs: list[dict]) -> list[dict]:
    spans, cur = [], []
    for sg in segs:
        cur.append(sg)
        dur = (cur[-1]["e"] - cur[0]["s"]) / 1000
        text = " ".join(x["t"] for x in cur)
        if (dur >= MIN_S and ENDER.search(text)) or dur >= MAX_S:
            spans.append(cur)
            cur = []
    if cur:
        spans.append(cur)
    return [
        {"start_ms": sp[0]["s"], "end_ms": sp[-1]["e"], "transcript": " ".join(x["t"] for x in sp)}
        for sp in spans
    ]


def build(
    video_id: str, issue_default: str = "", plus_hint: str = "이슈 정의의 정책·노선 지지"
) -> None:
    cap, meta = fetch(video_id)
    spans = chunk(segments(cap))
    # HTML 라벨링 도구 (드롭다운 · 삭제/병합/분할 · 자동 저장 · 완료 시 골든셋 JSON 다운로드)
    issues = [
        r["issue_slug"]
        for r in csv.DictReader(
            (ROOT / "data" / "seeds" / "issues_seed_v0.csv").open(encoding="utf-8-sig")
        )
    ] + ["election-fraud-allegation"]
    html = (ROOT / "scripts" / "label_tool_template.html").read_text(encoding="utf-8")
    html = (
        html.replace("__VID__", video_id)
        .replace("__TITLE__", meta.get("title", "").replace("<", "&lt;"))
        .replace("__CHANNEL__", meta.get("channel", ""))
        .replace("__ISSUES__", "".join(f'<option value="{i}">' for i in issues))
        .replace("__ISSUE_DEFAULT__", issue_default)
        .replace("__PLUS__", plus_hint)
        .replace("__SPANS__", json.dumps(spans, ensure_ascii=False))
    )
    out = GOLDEN_DIR / f"label_tool_{video_id}.html"
    out.write_text(html, encoding="utf-8")
    durs = [(sp["end_ms"] - sp["start_ms"]) / 1000 for sp in spans]
    print(
        f"{video_id}: {meta.get('channel', '')} · {meta.get('title', '')[:40]} · {meta.get('duration_s', '?')}s "
        f"→ 구간 {len(spans)}개 (평균 {sum(durs) / len(durs):.0f}s) → {out.relative_to(ROOT)}"
    )


def import_json(path: Path) -> None:
    """HTML 도구가 내려준 golden_<vid>_<labeler>.json 을 검증해 data/ 로 복사."""
    d = json.loads(path.read_text(encoding="utf-8"))
    bad = []
    for lab in d["labels"]:
        st = lab.get("stance_score")
        if (
            lab.get("type") not in ("fact", "claim", "opinion")
            or lab.get("sentiment") not in ("positive", "negative", "neutral")
            or (st is not None and not -2 <= int(st) <= 2)
            or "transcript" in lab
        ):
            bad.append(f"#{lab.get('idx')} {lab}")
    if bad:
        sys.exit("값 오류:\n  " + "\n  ".join(bad))
    d.setdefault(
        "labeled_at", datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    op = ROOT / "data" / f"golden_{d['video_id']}_{d['labeler']}.json"
    op.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{op.relative_to(ROOT)}: {len(d['labels'])}건 (라벨러 {d['labeler']})")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--import":
        import_json(Path(args[1]))
    else:
        for spec in args:
            vid, _, issue = spec.partition(":")
            build(vid, issue_default=issue)
