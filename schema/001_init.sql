-- 담론레이더 자산 DB 스키마 v1
-- 근거: PRD v1.1 §7 (데이터 모델). 이 파일 변경은 두 사람 합의 필수 (PRD §2 결정 원칙).
-- 설계 원칙:
--   * 발언(utterances)이 원자 — 모든 상품은 이 테이블과 집계 뷰에서만 나온다
--   * 저작권 대응: 원본 영상·음성·전사 전문은 여기 저장하지 않는다 (§7.3)
--     → quote_excerpt VARCHAR(200) 하드 리밋이 그 구현 강제 장치
--   * 파생 스냅샷(stances 등)은 validity 기간 패턴 — 리포트 소급 검증 가능해야 함

CREATE EXTENSION IF NOT EXISTS vector;

-- ── 채널 ────────────────────────────────────────────────────────────
CREATE TABLE channels (
    channel_id        TEXT PRIMARY KEY,          -- YouTube channel ID (UC...)
    name              TEXT NOT NULL,
    handle            TEXT,
    actor_type        TEXT NOT NULL CHECK (actor_type IN ('politician','commentator','media','expert')),
    orientation_label TEXT CHECK (orientation_label IN ('conservative','progressive','neutral')),
    orientation_basis TEXT,                      -- 라벨 산정 근거 (자기선언·소속·기존 연구 분류)
    domain            TEXT NOT NULL DEFAULT 'politics',  -- 경제 확장 대비 (§7.2)
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','removed')),
    added_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at        TIMESTAMPTZ,
    removal_reason    TEXT
);

-- ── 영상 ────────────────────────────────────────────────────────────
CREATE TABLE videos (
    video_id          TEXT PRIMARY KEY,
    channel_id        TEXT NOT NULL REFERENCES channels(channel_id),
    title             TEXT NOT NULL,
    description       TEXT,
    published_at      TIMESTAMPTZ NOT NULL,
    duration_sec      INTEGER,
    is_live           BOOLEAN NOT NULL DEFAULT false,
    truncated         BOOLEAN NOT NULL DEFAULT false,   -- 3시간 초과 절단 여부 (§4.3)
    caption_available BOOLEAN,
    transcript_source TEXT CHECK (transcript_source IN ('caption','stt','none')),
    view_count_d0     INTEGER,                  -- 수집 시점 스냅샷
    view_count_d1     INTEGER,                  -- D+1 재수집
    view_count_d7     INTEGER,                  -- D+7 재수집
    processing_status TEXT NOT NULL DEFAULT 'queued'
                      CHECK (processing_status IN ('queued','transcribed','structured','indexed','failed','skipped')),
    indexed_at        TIMESTAMPTZ,              -- 리드타임 지표 분자 (published_at → indexed_at)
    prompt_version    TEXT,
    model_id          TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_videos_channel ON videos(channel_id, published_at DESC);
CREATE INDEX idx_videos_status  ON videos(processing_status) WHERE processing_status NOT IN ('indexed','skipped');

-- ── 발언 ★ 원자 단위, 모든 지표의 근거 ──────────────────────────────
CREATE TABLE utterances (
    id             BIGSERIAL PRIMARY KEY,
    video_id       TEXT NOT NULL REFERENCES videos(video_id),
    channel_id     TEXT NOT NULL REFERENCES channels(channel_id),
    start_ms       INTEGER NOT NULL,
    end_ms         INTEGER NOT NULL,
    speaker_label  TEXT,
    source_url_ts  TEXT NOT NULL,               -- 타임스탬프 링크 (추적 가능성 원칙 §8)
    quote_excerpt  VARCHAR(200),                -- 짧은 인용 — 140자 원칙, 200자 하드 리밋 (§7.3)
    summary        TEXT NOT NULL,               -- 재서술 요지
    type           TEXT NOT NULL CHECK (type IN ('fact','claim','opinion')),
    sentiment      TEXT CHECK (sentiment IN ('positive','negative','neutral')),
    stance_score   NUMERIC(3,2) CHECK (stance_score BETWEEN -2 AND 2),  -- 발언 단위 stance
    verifiable_points TEXT[],                   -- 팩트체크 단서 큐 원료 (지표 ⑥)
    confidence     NUMERIC(3,2) CHECK (confidence BETWEEN 0 AND 1),
    embedding      VECTOR(1024),
    review_status  TEXT NOT NULL DEFAULT 'auto'
                   CHECK (review_status IN ('auto','sampled_ok','corrected','flagged')),
    prompt_version TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_utt_video   ON utterances(video_id);
CREATE INDEX idx_utt_channel ON utterances(channel_id, created_at DESC);
-- embedding 인덱스(ivfflat/hnsw)는 데이터 1만건 이상 쌓인 뒤 생성 (빈 테이블에 만들면 무의미)

-- ── 엔티티 (인물·정당·정책·사건·조직) ──────────────────────────────
CREATE TABLE entities (
    entity_id      BIGSERIAL PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('person','party','policy','event','org')),
    canonical_name TEXT NOT NULL UNIQUE,
    aliases        TEXT[] NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE utterance_targets (
    utterance_id BIGINT NOT NULL REFERENCES utterances(id) ON DELETE CASCADE,
    entity_id    BIGINT NOT NULL REFERENCES entities(entity_id),
    role         TEXT NOT NULL CHECK (role IN ('subject','object')),
    PRIMARY KEY (utterance_id, entity_id, role)
);
CREATE INDEX idx_ut_entity ON utterance_targets(entity_id);

-- ── 이슈 ────────────────────────────────────────────────────────────
CREATE TABLE issues (
    issue_id       BIGSERIAL PRIMARY KEY,
    slug           TEXT UNIQUE,                 -- 시드 CSV의 issue_slug
    name           TEXT NOT NULL,
    description    TEXT,                        -- 정의(논쟁 단위)
    category       TEXT,
    keywords       TEXT[] NOT NULL DEFAULT '{}',
    seed_or_discovered TEXT NOT NULL DEFAULT 'seed' CHECK (seed_or_discovered IN ('seed','discovered')),
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','merged','closed')),
    merged_into_id BIGINT REFERENCES issues(issue_id),
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE utterance_issues (
    utterance_id       BIGINT NOT NULL REFERENCES utterances(id) ON DELETE CASCADE,
    issue_id           BIGINT NOT NULL REFERENCES issues(issue_id),
    mapping_confidence NUMERIC(3,2),
    PRIMARY KEY (utterance_id, issue_id)
);
CREATE INDEX idx_ui_issue ON utterance_issues(issue_id);

-- ── 입장 (이슈 × 채널 × 날짜 — 파생 캐시, 재계산 가능해야 함 §7.2) ──
CREATE TABLE stances (
    channel_id     TEXT NOT NULL REFERENCES channels(channel_id),
    issue_id       BIGINT NOT NULL REFERENCES issues(issue_id),
    date           DATE NOT NULL,
    stance_score   NUMERIC(3,2) CHECK (stance_score BETWEEN -2 AND 2),  -- 신뢰도 가중 평균
    n_utterances   INTEGER NOT NULL,            -- 3 미만이면 "표본 부족" 표시 (지표 ②)
    confidence     NUMERIC(3,2),
    evidence_utterance_ids BIGINT[] NOT NULL DEFAULT '{}',
    validity_start_date DATE NOT NULL,          -- 유효기간 패턴 — 비중첩·전체 커버 (§7.2)
    validity_end_date   DATE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, issue_id, date)
);

-- ── 급등 감지 ────────────────────────────────────────────────────────
CREATE TABLE topics_daily (
    topic       TEXT NOT NULL,
    date        DATE NOT NULL,
    mention_cnt INTEGER NOT NULL,
    channel_cnt INTEGER NOT NULL,
    zscore      NUMERIC(6,2),                   -- 직전 14일 이동평균 대비 (지표 ①)
    is_spike    BOOLEAN NOT NULL DEFAULT false, -- z ≥ 2.0 AND channel_cnt ≥ 3
    PRIMARY KEY (topic, date)
);

-- ── 검수 (불변 로그 — 정확도 지표를 여기서 계산 §9) ─────────────────
CREATE TABLE reviews (
    id          BIGSERIAL PRIMARY KEY,
    target_type TEXT NOT NULL CHECK (target_type IN ('utterance','stance','issue','video')),
    target_id   TEXT NOT NULL,
    reviewer    TEXT NOT NULL,
    verdict     TEXT NOT NULL CHECK (verdict IN ('ok','corrected','rejected')),
    before      JSONB,                          -- 수정 전 스냅샷
    after       JSONB,                          -- 수정 후 스냅샷
    note        TEXT,
    is_goldenset BOOLEAN NOT NULL DEFAULT false, -- 골든셋 200건 + 분류기(M3) 학습 라벨 겸용
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_reviews_target ON reviews(target_type, target_id);

-- ── 운영 테이블 ─────────────────────────────────────────────────────
CREATE TABLE channel_list_changelog (
    id           BIGSERIAL PRIMARY KEY,
    channel_id   TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('added','removed','paused','resumed')),
    reason       TEXT NOT NULL,                 -- 사유 필수 기록 (§3.3)
    list_version TEXT NOT NULL,                 -- 리포트에 "채널 리스트 vX.Y 기준" 명시
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE pipeline_jobs (
    id         BIGSERIAL PRIMARY KEY,
    video_id   TEXT REFERENCES videos(video_id),
    job_type   TEXT NOT NULL CHECK (job_type IN ('collect_meta','transcribe','structure','embed','metrics','recollect_views')),
    status     TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
    attempts   INTEGER NOT NULL DEFAULT 0,      -- 지수 백오프 3회 후 failed → 검수 큐 노출 (§4.3)
    last_error TEXT,
    run_after  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_jobs_pending ON pipeline_jobs(run_after) WHERE status = 'queued';

CREATE TABLE cost_log (
    id            BIGSERIAL PRIMARY KEY,
    date          DATE NOT NULL,
    category      TEXT NOT NULL CHECK (category IN ('stt','llm_structure','llm_verify','embedding','youtube_api','infra')),
    model         TEXT,
    input_tokens  BIGINT,
    output_tokens BIGINT,
    cost_usd      NUMERIC(10,4),
    meta          JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_cost_date ON cost_log(date, category);
