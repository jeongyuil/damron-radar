"""담론레이더 파이프라인 (PRD §2 Step 1~5).

Step 1 collector   — 업로드 감지·메타데이터 (YouTube Data API, 1h 폴링)
Step 2 transcriber — 자막 확보 or STT (Groq Whisper), 전사는 한시 보관 후 파기
Step 3 structurer  — LLM 발언 추출 (Haiku 4.5 + Batch + structured outputs)
Step 4 metrics     — 일간 배치 06:00 KST (급등·관점분포·입장변화)
Step 5 검수·출고    — 분석가 검수 → 주간 리포트

모듈 구조는 엔지니어(성환) 재량으로 변경 가능. 단 §7 스키마·§8 지표 정의는 합의 필수.
"""
