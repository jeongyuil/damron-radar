# Snowflake 샘플 데이터 검토 (2026-09-09)

검토: 유일 · 대상: 성환 셋업 `OPENVOICE.RAW` (YOUTUBE_VIDEOS 130 · YOUTUBE_TRANSCRIPTS 20 · YOUTUBE_COMMENTS 200)
방법: 읽기 전용 접속 후 DESCRIBE·집계 쿼리, `data/channels_final_v1.0.csv`(선정 31곳)와 대조.

## 요약

셋업 자체는 정상 동작한다(접속·조회 OK, 중복·NULL 없음, video_id 참조 무결). 그러나 **샘플이 PRD 원칙 4가지와 어긋나** 이 상태로 파이프라인을 이어가면 안 된다. 수정 우선순위 순:

| # | 문제 | 근거 | PRD/결정 |
|---|---|---|---|
| 1 | **전사에 타임스탬프가 없다** | START_SECONDS·DURATION_SECONDS 20건 전부 0, RAW_TRANSCRIPT 키가 `text`·`videoId`뿐 | §5·§7 utterances.start_ms/end_ms·source_url_ts — 타임스탬프 링크는 B2B 신뢰의 근간. 청킹 스펙(step3_chunking_spec)도 타임스탬프 전제 |
| 2 | **전사가 10,000자에서 잘린다** | FULL_TEXT 최대값이 정확히 10000 (5건이 상한에 걸림, 2시간짜리도 10000자) | §4.3 상한은 "3시간 초과분 절단"이지 글자수가 아님. 발언 유실 |
| 3 | **수집 채널이 register와 다르다** | 21개 채널 중 9개(27건)가 선정 31곳 밖. register 31곳 중 19곳은 샘플에 없음 | D5 채널 선정 확정(7/27). 파이프라인은 `channels_final_v1.0.csv` 대상 |
| 4 | **댓글 테이블은 비범위** | YOUTUBE_COMMENTS 200건 | PRD §1 비범위: 댓글 수집·분석은 Phase 2 검토. 발언(화자) 데이터에 집중 |

## 상세

### 1. YOUTUBE_VIDEOS (130건, 2026-08-30 하루치)

- 무결성: video_id 중복 0, 핵심 컬럼 NULL 0. `RAW_METADATA` VARIANT에 원본 보존 — 좋음.
- **채널 대조**: register 안 12곳 103건 / 밖 9곳 27건.
  - 밖: 뉴탐사(14), 여의도튜브·머니투데이(4), 이준석 채널(2), 뿌꾸삼촌(2), 신인균의 국방TV, 윤PD TV, 삼프로TV 정치, 보수미, 시사타파TV.
  - 뉴탐사·시사타파·이준석 채널은 심사에서 탈락/보류된 곳이고, 여의도튜브는 경제 채널(비범위 도메인). 실사 문서 `channel_review_draft_2026-07-26.md` 참조.
  - 없음 19곳: 펜앤드마이크·매일신문·김태우TV·홍카콜라·박성태의 뉴스쇼·채널A TOP10·CBS 한판승부·JTBC 장르만 여의도·국회방송·매불쇼·오마이TV·서울의소리·김용민TV·고발뉴스·이동형TV·취재편의점·정치 읽어주는 여자·박시영TV·사장남천동. 중립 축 5곳이 통째로 빠져 있어 이 샘플로는 관점 분포(§8 ②) 산출 불가.
- **BIAS 컬럼**: `진보(추정)`·`보수(추정)`·`성향 확인 필요` 같은 임시값. 성향 라벨은 register의 `성향축`(보수/진보/중립)과 `orientation_basis`를 channels 테이블에서 가져와야지, 영상 행에 추정값을 박으면 안 된다(§3.2 내부 균형 관리용, §7 channels.orientation_label). 이큐채널은 register 보수인데 샘플은 "확인 필요"로 불일치.
- **길이 분포**: 3분 미만 36건(28%) — §4.3 "3분 미만 쇼츠는 전사 생략" 규칙 적용 필요(D1). 3시간 초과 7건 — 절단 규칙(`truncated=true`) 필요.
- 채널 편중: 스픽스 54건(42%). 하루치라 그렇겠지만 §8 지표는 채널 가중이 없으므로 확인.
- `DESCRIPTION_SUMMARY`가 130건 중 98건에서 RAW와 다름 — 무엇으로 요약했는지(LLM? 절단?) 명시 필요. 자산 DB에 들어갈 값이면 prompt_version·model_id 기록 대상.

### 2. YOUTUBE_TRANSCRIPTS (20건)

- 130건 중 20건만 전사. 어떤 기준으로 20건인지 불명(길이 23초 쇼츠 3건 포함, 2시간 라이브 2건 포함).
- 타임스탬프 없음(위 #1). 자막 API(json3)든 Whisper든 세그먼트 단위 시각이 나온다 — 세그먼트를 버리고 텍스트만 합친 것으로 보임. **세그먼트 보존 필수**: `[{start_ms, end_ms, text}]`를 VARIANT로.
- 10,000자 절단(위 #2). 전사 소스(caption/stt)도 컬럼에 없음 — §7 videos.transcript_source 필요.
- `LINE_TEXT`·`FULL_TEXT` 중복 컬럼(17건은 서로 다름 — LINE_TEXT는 첫 줄?). 용도 정리 필요.
- 보관 정책: 2026-07-29 결정 — 개발 단계 전사 전문 보관은 허용하되 **자산 DB와 물리적 분리**, 외부 제공 금지, 자문 후 일괄 파기 가능 구조. 지금은 RAW 스키마에 videos와 나란히 있음. 최소 별도 스키마(예: `OPENVOICE.TRANSCRIPTS_TMP`)로 분리하고 retention을 짧게.

### 3. YOUTUBE_COMMENTS (200건)

- 10개 영상 × 20건. 스키마·적재는 깔끔하다.
- 그러나 PRD §1 비범위. 댓글에는 작성자 핸들·채널ID(개인 식별자)가 그대로 들어 있어 법률 자문(D6) 전에는 수집 자체가 리스크. 테이블은 두더라도 **수집 중단 + 기존 200건 삭제** 권장. Phase 2에서 다시 결정.

### 4. 계정·비용

- 사용자 1명(OPENVOICE)이 ACCOUNTADMIN, 비밀번호가 Slack 평문 공유됨. 요청: ① 비밀번호 교체 ② 분석가용 롤(RAW 읽기 + 검수 테이블 쓰기)과 사용자 분리 ③ 비밀번호는 각자 `.env`에만.
- COMPUTE_WH X-Small auto-suspend 켜져 있음 — 좋음. SNOWFLAKE_LEARNING_WH·STREAMLIT_WH는 auto-suspend 꺼짐 — 안 쓰면 suspend/삭제.
- Snowflake 채택은 엔지니어 재량(PRD §2 결정 원칙)이라 이의 없음. 다만 비용 원칙("3개월 뒤 버려도 아깝지 않은 도구") 기준으로 월 예상 크레딧을 비용 대시보드(§12)에 한 줄 넣어 주면 좋겠다. Cortex Search 상시 서빙 비용 우려는 step3_chunking_spec 부록 C에 정리돼 있음.

## 성환님께 요청 (우선순위)

1. 전사 세그먼트(타임스탬프) 보존 + 글자수 절단 제거 + transcript_source 컬럼. 이게 안 되면 Step 3·§8 전부 막힘.
2. 수집 대상을 `data/channels_final_v1.0.csv` 31곳으로 고정, BIAS는 영상 행이 아니라 channels 테이블(register 그대로)에서.
3. 댓글 수집 중단·삭제(Phase 2 재논의).
4. 3분 미만 전사 생략 · 3시간 초과 절단 플래그(§4.3).
5. 전사 테이블을 별도 스키마로 분리(7/29 보관 정책).
6. 비밀번호 교체 + 분석가용 롤 분리.

이 6개가 반영된 샘플(하루치면 충분)로 다시 검토하겠다. 그 다음이 3주차 "테스트 데이터 평가"고, 골든셋 벤치(`docs/golden_bench_2026-09-09.md`)는 Snowflake 전사가 타임스탬프를 갖추면 그 데이터로 바로 돌릴 수 있게 되어 있다.
