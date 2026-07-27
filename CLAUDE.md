# CLAUDE.md — damron-radar 작업 컨텍스트

담론레이더: 정치·시사 유튜브 담론을 수집·구조화해 주간 리포트로 파는 B2B 데이터 서비스.
이 저장소는 **성환(엔지니어)·유일(분석가) 공유 제품 저장소**다. 개인 기획은 별도 저장소(open-voice).

## 팀·소유

- 성환(엔지니어): `pipeline/` `schema/` 구현, 인프라, 비용 모니터링
- 유일(분석가): `data/` `prompts/` 품질, 검수, 리포트, 잠재고객
- **리뷰 규칙**: `schema/`·`prompts/`·지표 정의 변경 PR만 상호 승인 필수. 나머지는 각자 머지.

## 절대 규칙 (자산 보호)

- **스키마(schema/, PRD §7)·지표 정의(§8) 변경은 두 사람 합의 없이 금지** — 이 둘이 자산
- **프롬프트는 버전 파일로만** — 기존 `prompts/**/*_vN.md` 수정 금지, 새 버전 생성 (utterances.prompt_version이 파일명 참조)
- **원본 비저장 원칙(§7.3)** — 영상·음성·전사 전문은 자산 DB/저장소에 넣지 않는다. 요약·지표·140자 인용·타임스탬프 링크만. quote_excerpt는 200자 하드리밋
- **쿼터 원칙(§4.1)** — YouTube `search.list`(100 units) 사용 금지, `playlistItems.list`(1 unit) 폴링
- **LLM 배치(D3)** — 구조화 Haiku 4.5+Batch+캐싱 / 검증 Sonnet 5 / 월 15만원 상한. 프로덕션은 API 키 종량제(구독 CLI 금지)
- **리스크 채널 배제 원칙** — 법적 분쟁·평판 리스크 중대 채널은 배제(예외는 decision_log에 사유 기록)

## 문서 지도

- `docs/decision_log.md` — 확정 결정 (D1~D10 + 리스크 원칙 등). **결정은 전부 여기 기록**
- `docs/kickoff_W1-2.md` — W1~2 할 일·진행
- `docs/channel_selection_sheet.md` — 채널 최종 선정 컨펌 시트 (D5)
- `docs/channel_review_draft_2026-07-26.md` — 채널 심사 근거·이력
- `data/channels_final_v1.0.csv` — **선정 채널 register (31곳)** — 파이프라인이 이걸 대상으로
- `schema/001_init.sql` — 자산 DB 스키마 v1

## 현재 상태 (2026-07-27)

- **채널 선정 완료**: 후보 59곳 조사·실사 → 선정 31곳(진보 14·보수 12·중립 5), 리스크 3곳 예외 통과
- **남은 D5 판단**: 좌우 동수 조정(진보 2곳 많음) + 중립 6 채우기(MBC 라디오 시사 승격 검토) + 경계 채널 최종 확인
- 시드: 이슈 31·엔티티 211 (`data/seeds/`)
- **다음 작업**: (엔지니어) 수집기 프로토타입 — `channels_final_v1.0.csv` 대상 YouTube API 폴링 → DB 적재 / (분석가) 법률 자문 예약, 샘플 리포트 목업

## 개발 환경

`docker compose up -d` (Postgres+pgvector, 스키마 자동적용 · Metabase :3000) / `uv sync` / 키는 `.env`(비커밋)
실사: `uv run python scripts/audit_channels.py` (YouTube API로 채널 지표 실측)
