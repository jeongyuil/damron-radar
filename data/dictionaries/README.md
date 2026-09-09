# data/dictionaries — 사전 (2주차 산출물, 분석가 소유)

PRD는 type/sentiment/stance를 **LLM이 분류**하는 구조(§6.2·§6.3)다. 여기 사전은 규칙 분류기가 아니라
아래 세 소비자를 위한 참조 자료다. 각 파일 헤더의 `scope`/`use` 컬럼이 소비자를 명시한다.

| 파일 | 소비자 | 원칙 |
|---|---|---|
| `stt_corrections_v0.csv` | ① 추출 후 엔티티 매칭(raw_name → canonical, §6.3 "미매칭은 raw_name 보존") ② 프롬프트 `[엔티티 사전]`의 STT 변형 힌트(고신뢰만) | **전사 텍스트는 수정하지 않는다**(step3 스펙 3a-0 "발화 무수정"). quote_excerpt는 오탈자 그대로, summary·targets에서 정규화 |
| `noise_patterns_v0.csv` | 3a-0 전처리(구간 제거) · 요약/임베딩 정규화 · 추출 제외 규칙 | `scope=preprocess_3a0`만 코드로 제거. `summary_embedding`은 quote에 적용 금지. 제거 구간은 로그 |
| `entities_additions_v0.csv` | (병합 완료 → `data/seeds/entities_seed_v1.csv`, 228건) | 이력용 |
| `type_decision_book_v0.md` | 검수 기준(§9) · 프롬프트 v2 판례 주입(상호 승인) | §6.2 각주 "판례집 자체가 자산" |
| `type_signal_lexicon_v0.csv` | 검수 큐 우선순위(사전 신호와 LLM 판정 불일치 건) | LLM 분류 대체 아님 |
| `stance_polarity_v0.csv` | 프롬프트 `[영상 컨텍스트]` 이슈별 부호 정의 · 검수 기준 | **§8 지표 해석 → 상호 승인**. 원칙 S1: 부호는 발언 **대상**이 아니라 **이슈** 기준(판례집 §2-1) |
| `sentiment_lexicon_v0.csv` | 검수 큐 우선순위 | 반어·인용 예외 표기 |

버전 규칙은 prompts/와 동일: 기존 버전 파일 수정 금지, 새 버전 생성.
출처 코퍼스: 파일럿 전사 7편 + 골든셋 자막 1편 + Snowflake 전사 20건 (2026-09-09, 약 21만 자).

## 검증 (골든셋 wBbMtXMTU8w 13건, Qwen3.5-9B thinking off, 2026-09-09)

`scripts/golden_bench.py --with-dict` 로 판례집 규칙(R1~R14)과 이슈 부호 규칙을 프롬프트에 주입한 전/후 비교.
결과는 `reports/golden_bench_wBbMtXMTU8w.md` 표의 `qwen3.5-9b-8bit` vs `qwen3.5-9b-8bit+dict` 행.
어휘 사전(D·E2)은 프롬프트에 넣지 않는다 — 검수 큐 우선순위용(신호만으로 골든셋 8/13, 분류기 아님).
