"""골든셋 라벨링 템플릿 생성 — YouTube 자막을 발언 후보 구간으로 잘라 빈 라벨 칸을 붙인다.

사용법:
    uv run python scripts/golden_template.py MD-vsT5q_5U EjAL2y164Cc
    → transcripts/golden/label_template_<vid>.json  (구간 + 전사 + 빈 type/sentiment/stance)
      transcripts/golden/label_template_<vid>.md    (읽으면서 채우는 워크시트)

라벨링 후: transcript 필드를 뺀 파일을 data/golden_<vid>_<라벨러>.json 으로 저장
    uv run python scripts/golden_template.py --finalize transcripts/golden/label_template_<vid>.json 유일

구간 규칙: 자막 이벤트를 문장 종결(다./요./죠./까?)에서 끊어 20~90초 묶음. 라벨러가 병합·분할·삭제 자유.
기준: PRD §6.2 결정 트리 (①참/거짓 판별 원리상 가능? 아니오→opinion ②근거 제시? 예→fact 아니오→claim)
"""

from __future__ import annotations

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


def build(video_id: str) -> None:
    cap, meta = fetch(video_id)
    spans = chunk(segments(cap))
    tpl = {
        "video_id": video_id,
        "labeler": "",
        "labeled_at": None,
        "meta": meta,
        "guide": "PRD §6.2: ①제3자가 참/거짓 판별 원리상 가능? 아니오→opinion ②근거(수치·출처·일시) 제시? 예→fact 아니오→claim. "
        "sentiment=발언 대상에 대한 화자 감정 positive|negative|neutral. stance_score=관련 이슈 입장 -2~+2, 무관 null. "
        "구간은 병합·분할·삭제 자유 — 광고·인사말·잡담 구간은 삭제. 발언이 아닌 구간은 지운다.",
        "labels": [
            {
                "idx": i + 1,
                "start_ms": sp["start_ms"],
                "end_ms": sp["end_ms"],
                "transcript": sp["transcript"],
                "type": "",
                "sentiment": "",
                "stance_score": None,
                "issue": "",
                "note": "",
            }
            for i, sp in enumerate(spans)
        ],
    }
    jp = GOLDEN_DIR / f"label_template_{video_id}.json"
    jp.write_text(json.dumps(tpl, ensure_ascii=False, indent=1), encoding="utf-8")
    md = [
        f"# 라벨링 워크시트 — {meta.get('channel', '')} · {meta.get('title', '')}",
        f"video: https://youtube.com/watch?v={video_id} · 길이 {meta.get('duration_s', '?')}s · 구간 {len(spans)}개",
        "",
        "type: fact / claim / opinion · sentiment: positive / negative / neutral · stance: -2~+2 또는 null",
        "",
        "| # | 시작 | 전사 | type | sent | stance | issue/메모 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, sp in enumerate(spans):
        s = sp["start_ms"] // 1000
        md.append(
            f"| {i + 1} | [{s // 60}:{s % 60:02d}](https://youtube.com/watch?v={video_id}&t={s}s) | {sp['transcript']} |  |  |  |  |"
        )
    (GOLDEN_DIR / f"label_template_{video_id}.md").write_text("\n".join(md), encoding="utf-8")
    durs = [(sp["end_ms"] - sp["start_ms"]) / 1000 for sp in spans]
    print(
        f"{video_id}: {meta.get('channel', '')} · {meta.get('title', '')[:40]} · {meta.get('duration_s', '?')}s → 구간 {len(spans)}개 "
        f"(평균 {sum(durs) / len(durs):.0f}s) → {jp.relative_to(ROOT)} / .md"
    )


def finalize(path: Path, labeler: str) -> None:
    d = json.loads(path.read_text(encoding="utf-8"))
    labels = [
        {k: v for k, v in lab.items() if k != "transcript"}
        for lab in d["labels"]
        if lab.get("type")
    ]
    for i, lab in enumerate(labels):
        lab["idx"] = i + 1
    out = {
        "video_id": d["video_id"],
        "labeler": labeler,
        "labeled_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "labels": labels,
    }
    op = ROOT / "data" / f"golden_{d['video_id']}_{labeler}.json"
    op.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"{op.relative_to(ROOT)}: {len(labels)}건 (type 비어 있는 구간 {len(d['labels']) - len(labels)}개 제외)"
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--finalize":
        finalize(Path(args[1]), args[2])
    else:
        for vid in args:
            build(vid)
