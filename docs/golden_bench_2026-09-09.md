# 골든셋 선발전 1차 — 개발 결과 공유 (2026-09-09)

작성: 유일 · 대상: 성환 · 관련 파일: `scripts/golden_bench.py`, `reports/golden_bench_wBbMtXMTU8w.md`

## 1. 목적

Step 3 구조화(발언 → type / sentiment / stance_score)에 쓸 모델을 골든셋으로 비교 선발하는
재사용 가능한 벤치 스크립트를 만들고, 현 임시 모델(Qwen3.5-9B-MLX-8bit)의 첫 성적을 낸다.
PRD §1 게이트: **발언 유형 일치율 85%+, 입장 방향 일치율 90%+**.

## 2. 만든 것

### 2.1 `scripts/golden_bench.py`

| 단계 | 내용 |
|---|---|
| 입력 결합 | `data/golden_<vid>_<라벨러>.json`(start_ms/end_ms + 정답)과 YouTube 자동자막(json3)을 타임스탬프로 결합 → `transcripts/golden/golden_<vid>.jsonl`. 라벨의 ms 경계가 자막 이벤트 경계와 정확히 일치해 손실 없이 매핑됨. 자막이 없으면 yt-dlp로 자동 다운로드(API 쿼터 0) |
| 프롬프트 | PRD §6.2 결정 트리를 시스템 프롬프트로 고정: ① 제3자가 참/거짓 판별 원리상 가능? 아니오→opinion ② 근거(수치·출처·일시) 제시? 예→fact, 아니오→claim. §6.2 정의 표·예시 포함. 발언 1건씩, temperature 0, 출력은 `{type, sentiment, stance_score, reason}` JSON |
| 후보 모델 | 레지스트리(`CANDIDATES`)에 등록. OpenAI 호환 로컬 서버(mlx_lm.server 등)와 Anthropic API 지원. 실행 불가 후보(키 없음·서버 없음)는 자동 스킵하고 표에 사유 표기. 새 후보는 레지스트리 추가 또는 `--add name=<base_url>\|<model>` |
| 채점 | type: accuracy · macro-F1 3클래스(fact/claim/opinion) · macro-F1 2클래스(fact/opinion — 골든셋에 claim 표본이 0건이라 병기, claim 예측은 양쪽 모두 오답) / sentiment: accuracy · macro-F1 / stance: MAE(정답·예측 모두 non-null) · null 일치율 · 방향(부호) 일치율 |
| 출력 | `reports/golden_bench_<vid>.md`(비교 표 + 혼동 행렬 + 오답 덤프) · `reports/golden_bench_<vid>_preds.json`(원시 예측·지연·토큰·프롬프트 해시). 후보를 따로 돌려도 같은 골든셋·같은 프롬프트면 한 표에 병합 |

오답 덤프에는 건별 타임스탬프 링크, 전사 200자(§7.3 하드리밋 준수), 모델이 밝힌 판단 근거, 라벨러 메모가 붙는다.

### 2.2 실행

```bash
caffeinate -i uv run python -u scripts/golden_bench.py            # 실행 가능한 기본 후보 전부
uv run python scripts/golden_bench.py --list                       # 후보·실행 가능 여부만
uv run python scripts/golden_bench.py --models claude-haiku-4-5    # 특정 후보만 (결과는 기존 표에 병합)
uv run python scripts/golden_bench.py --add exaone=http://localhost:8081/v1|LGAI/EXAONE-4.0
```

`caffeinate`가 붙은 이유: 1차 실행 때 맥이 절전에 들어가면서 로컬 서버 요청 하나에 16시간 멈춰 있었다
(httpx 타임아웃도 안 걸림). 로컬 모델 대상 장시간 실행은 반드시 절전 방지 + `-u`(출력 버퍼링 해제).

### 2.3 저장 위치 원칙

- 전사가 든 jsonl과 자막 원본은 `transcripts/golden/` — `.gitignore` 대상(§7.3, 2026-07-29 개발 단계 전사 보관 결정).
- 리포트의 인용은 200자 절단.

## 3. 1차 결과 — Qwen3.5-9B-MLX-8bit (thinking off)

골든셋: wBbMtXMTU8w(박성태의 뉴스쇼, 2026-08-05) 13건, 라벨러 성환. 정답 분포 fact 3 · claim 0 · opinion 10.

| 지표 | 값 | PRD 게이트 |
|---|---|---|
| type 정확도 | 53.8% | 85% |
| type macro-F1 (3클래스 / 2클래스) | 0.25 / 0.37 | |
| sentiment 정확도 | 46.2% | |
| stance MAE (양쪽 non-null 7건) | 1.57 | |
| stance null 일치율 | 61.5% | |
| stance 방향 일치율 | 28.6% | 90% |
| 건당 지연 | 7.2s | |

type 혼동 행렬 (행=정답, 열=예측):

| 정답 \ 예측 | fact | claim | opinion |
|---|---|---|---|
| fact | 0 | 1 | 2 |
| opinion | 0 | 3 | 7 |

### 오답 패턴 (덤프 기준)

1. **fact 3건 전부 놓침.** 자막 전사에는 "시가 20억~35억", "2007년 친이친박 갈등" 같은 구체 근거가 있는데도 opinion이나 claim으로 분류. STT 오탈자 속에서 근거를 근거로 인식 못 함.
2. **정답에 없는 claim을 4건 생성.** "들었다", "핵심을 요약하면", "얘기하더라고요" 같은 전언·요약 표현을 결정 트리 ②의 "근거 미제시"로 읽음. §6.2 각주("들었다고만 말하면 claim")를 문자 그대로 과적용한 형태.
3. **stance 방향 대부분 뒤집힘.** 비판적 어조면 이슈와 무관하게 -1을 매김. 이슈별 부호 방향(무엇을 지지하면 +인지)이 프롬프트에 정의돼 있지 않은 것도 원인.
4. sentiment는 neutral 정답을 negative로 읽는 쪽으로 치우침(정답 neutral 7건 중 4건 negative 예측).

### 탈락·미실행 후보

| 후보 | 상태 | 사유 |
|---|---|---|
| Qwen3.5-9B thinking on | 탈락 | 1건째 6.4분 걸리고도 오답(opinion→fact), 2건째는 6,000토큰 상한을 3회 연속 소진해 답 없음. 레지스트리에 남겨두되 `--models`로 지명할 때만 실행 |
| claude-haiku-4-5 (D3 구조화) | 미실행 | `.env` ANTHROPIC_API_KEY 비어 있음 |
| claude-sonnet-5 (D3 검증) | 미실행 | 동일 |

## 4. 골든셋 자체에 대한 확인 요청

13건 중 stance 라벨이 발언 내용과 어긋나 보이는 건이 있어 확인 부탁:

- **#6, #7** (1228s~): 후보 발언 클립 몽타주 + 이어지는 비판인데 stance +2. 어느 이슈에 대한 +2인지?
- **#9** (1327s): sentiment negative인데 stance +1.
- **#10** (1392s): "당이 쪼개질 수도" 전망에 stance +1.

이슈별 부호 규칙(예: "전당대회" 이슈에서 + = 무엇을 지지)을 한 줄씩 정해 두면 stance 채점이 라벨 기준 논쟁에서 벗어난다.
claim 표본이 0건이라 type 채점도 반쪽이다 — 다음 라벨링 영상은 claim이 나올 만한 논객 채널 쪽을 권함.

## 5. 다음 단계 제안

1. (성환) `.env`에 ANTHROPIC_API_KEY 설정 후 `uv run python scripts/golden_bench.py --models claude-haiku-4-5,claude-sonnet-5` — 결과가 기존 표에 병합됨. Haiku 4.5가 D3 기본안이므로 이 수치가 실질 기준선.
2. (성환) 로컬 후보를 더 볼 거면 mlx_lm.server에 다른 모델을 띄우고 `--add`로 등록. 9B급 thinking 모드는 시간 대비 이득 없음이 확인됐으니 non-thinking 위주로.
3. (유일) 위 4번 stance 라벨 확인 + 이슈별 부호 규칙 정리 → 프롬프트 `[영상 컨텍스트]`에 주입.
4. (공동) 골든셋 2~3개 영상으로 확장한 뒤 결론. 13건은 1건 = 7.7%p라 순위 판단에 쓰기 어렵다.
5. 프롬프트가 안정되면 `prompts/structurer/` 버전 파일로 승격(현재는 스크립트 안의 `SYSTEM_PROMPT`, 해시가 preds.json에 기록됨).
