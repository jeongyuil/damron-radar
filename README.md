# 담론레이더 (damron-radar)

정치·시사 유튜브 30개 채널의 발언을 수집·구조화해 이슈별 통계·입장 지표·발언 아카이브를
주간 리포트로 제공하는 B2B 데이터 파이프라인. **Phase 1 (0~3개월): 매출이 아니라 증거.**

- 기준 문서: PRD v1.1 (기획 저장소 보관 · 스키마 §7·지표 §8은 두 사람 합의 없이 변경 금지)
- 결정 사항: [docs/decision_log.md](docs/decision_log.md) · 진행 상황: [docs/kickoff_W1-2.md](docs/kickoff_W1-2.md)
- Day 90 게이트 (2026-10-25): 30채널 상시 가동 · 샘플 리포트 3부 · 유료 파일럿 의향 3곳+

## 빠른 시작

```bash
cp .env.example .env        # 키 채우기 (1Password 공유 볼트 참조)
docker compose up -d        # Postgres(+pgvector, 스키마 자동 적용) + Metabase(:3000)
uv sync                     # Python 3.12 의존성
uv run pytest               # (테스트는 추후)
```

## 구조

```
pipeline/   Step 1~5 파이프라인 코드 (소유: 성환)
schema/     DB 스키마 — PRD §7. 변경은 상호 승인 PR로만
prompts/    LLM 프롬프트 — 파일명 버전관리, 기존 버전 수정 금지
data/       채널 후보·시드 (이슈 31·엔티티 211) — 소유: 유일
docs/       결정 로그·주차 체크리스트
tests/
```

## 파이프라인 (PRD §2)

```
[1 수집] YouTube API 1h 폴링 ──▶ [2 전사] 자막 or Groq Whisper ──▶ [3 구조화] Haiku 4.5
                                   (전사 전문 90일 후 파기)          + Batch + 캐싱
──▶ [4 지표화] 일배치 06:00 KST ──▶ [5 검수·출고] 분석가 검수 → 주간 리포트 (월 08:00)
```

## 원칙 (전문은 PRD)

- **발언(utterances)이 원자** — 모든 상품은 이 테이블에서 나온다
- **원본 비저장** — 영상·음성·전사 전문은 자산 DB에 저장하지 않는다. 요약·지표·140자 인용·타임스탬프 링크만
- **추적 가능성** — 모든 수치는 2단계 내에 근거 발언·원문 링크 도달
- **쿼터 원칙** — `search.list` 사용 금지 (100 units), `playlistItems.list`(1 unit) 폴링
- **LLM 배치** — 구조화 Haiku 4.5 / 검증 Sonnet 5 / 월 15만원 상한 (decision_log D3)

## 팀

| | 소유 |
|---|---|
| 성환 (엔지니어) | pipeline/ · schema/ 구현 · 인프라 · 비용 모니터링 |
| 유일 (분석가) | data/ · prompts/ 품질 · 검수 · 리포트 · 잠재고객 |

리뷰 규칙: `schema/` `prompts/` 및 지표 정의 변경 PR만 상호 승인 필수. 나머지는 각자 머지.
