"""골든셋 라벨링 템플릿 생성 — YouTube 자막을 발언 후보 구간으로 잘라 빈 라벨 칸을 붙인다.

사용법:
    uv run python scripts/golden_template.py MD-vsT5q_5U EjAL2y164Cc
    → transcripts/golden/label_template_<vid>.json  (구간 + 전사 + 빈 type/sentiment/stance)
      transcripts/golden/label_template_<vid>.md    (읽으면서 채우는 워크시트)

라벨링은 .csv(스프레드시트) · .md(표) · .json 중 편한 걸로. 끝나면:
    uv run python scripts/golden_template.py --finalize transcripts/golden/label_template_<vid>.csv 유일
    (.md/.json도 동일. 값 검증 후 data/golden_<vid>_<라벨러>.json 생성 — transcript 제외)

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
    import csv

    with (GOLDEN_DIR / f"label_template_{video_id}.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as f:
        w = csv.writer(f)
        w.writerow(
            [
                "idx",
                "start_ms",
                "end_ms",
                "시작",
                "type",
                "sentiment",
                "stance_score",
                "issue",
                "note",
                "transcript",
            ]
        )
        for i, sp in enumerate(spans):
            st = sp["start_ms"] // 1000
            w.writerow(
                [
                    i + 1,
                    sp["start_ms"],
                    sp["end_ms"],
                    f"{st // 60}:{st % 60:02d}",
                    "",
                    "",
                    "",
                    "",
                    "",
                    sp["transcript"],
                ]
            )
    durs = [(sp["end_ms"] - sp["start_ms"]) / 1000 for sp in spans]
    print(
        f"{video_id}: {meta.get('channel', '')} · {meta.get('title', '')[:40]} · {meta.get('duration_s', '?')}s → 구간 {len(spans)}개 "
        f"(평균 {sum(durs) / len(durs):.0f}s) → {jp.relative_to(ROOT)} / .md"
    )


def _labels_from_csv(path: Path) -> tuple[str, list[dict]]:
    import csv

    vid = path.stem.replace("label_template_", "")
    labels = []
    for r in csv.DictReader(path.open(encoding="utf-8-sig")):
        labels.append(
            {
                "idx": int(r["idx"]),
                "start_ms": int(r["start_ms"]),
                "end_ms": int(r["end_ms"]),
                "type": r.get("type", "").strip(),
                "sentiment": r.get("sentiment", "").strip(),
                "stance_score": r.get("stance_score", "").strip(),
                "issue": r.get("issue", "").strip(),
                "note": r.get("note", "").strip(),
            }
        )
    return vid, labels


def _labels_from_md(path: Path) -> tuple[str, list[dict]]:
    """워크시트 .md 표에서 읽기 — start/end ms는 같은 이름의 .json 템플릿에서 가져온다."""
    vid = path.stem.replace("label_template_", "")
    tpl = json.loads((path.parent / f"label_template_{vid}.json").read_text(encoding="utf-8"))
    by_idx = {lab["idx"]: lab for lab in tpl["labels"]}
    labels = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*(\d+)\s*\|", line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # | # | 시작 | 전사 | type | sent | stance | issue/메모 |
        if len(cells) < 7:
            continue
        idx = int(cells[0])
        src = by_idx.get(idx)
        if not src:
            continue
        issue_note = cells[6]
        issue, _, note = issue_note.partition("/")
        labels.append(
            {
                "idx": idx,
                "start_ms": src["start_ms"],
                "end_ms": src["end_ms"],
                "type": cells[3],
                "sentiment": cells[4],
                "stance_score": cells[5],
                "issue": issue.strip(),
                "note": note.strip(),
            }
        )
    return vid, labels


def finalize(path: Path, labeler: str) -> None:
    """채운 템플릿(.json / .csv / .md) → data/golden_<vid>_<labeler>.json (transcript 제외, type 빈 구간 제외)."""
    if path.suffix == ".csv":
        vid, labels = _labels_from_csv(path)
    elif path.suffix == ".md":
        vid, labels = _labels_from_md(path)
    else:
        d = json.loads(path.read_text(encoding="utf-8"))
        vid, labels = d["video_id"], d["labels"]
    out_labels, bad = [], []
    for lab in labels:
        t = str(lab.get("type", "")).strip().lower()
        if not t:
            continue
        sent = str(lab.get("sentiment", "")).strip().lower()
        st = lab.get("stance_score")
        st = None if st in (None, "", "null", "None") else int(float(st))
        if (
            t not in ("fact", "claim", "opinion")
            or sent not in ("positive", "negative", "neutral")
            or (st is not None and not -2 <= st <= 2)
        ):
            bad.append(f"#{lab['idx']} type={t} sentiment={sent} stance={st}")
        out_labels.append(
            {
                "idx": 0,
                "start_ms": lab["start_ms"],
                "end_ms": lab["end_ms"],
                "type": t,
                "sentiment": sent,
                "stance_score": st,
                "issue": lab.get("issue", ""),
                "note": lab.get("note", ""),
            }
        )
    if bad:
        sys.exit("값 오류 — 고치고 다시 실행:\n  " + "\n  ".join(bad))
    for i, lab in enumerate(out_labels):
        lab["idx"] = i + 1
    out = {
        "video_id": vid,
        "labeler": labeler,
        "labeled_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "labels": out_labels,
    }
    op = ROOT / "data" / f"golden_{vid}_{labeler}.json"
    op.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"{op.relative_to(ROOT)}: {len(out_labels)}건 (type 비어 있는 구간 {len(labels) - len(out_labels)}개 제외)"
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--finalize":
        finalize(Path(args[1]), args[2])
    else:
        for vid in args:
            build(vid)
