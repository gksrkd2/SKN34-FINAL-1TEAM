# v3 공종별 에이전트 구조와 추가 방법

## 현재 구조

공종별 기본 설정, 검색, 답변 지침을 각 공종 폴더에서 관리한다. 서버 프로세스가 공종 폴더를 찾아 연결한다. 현재 전용 폴더가 연결된 공종은 **도배**다. 목공·전기·철거 등은 공종 설정과 공통 상담 흐름을 사용한다.

작업 브랜치가 리베이스한 팀 `dev`(`4d14482`) 기준으로 추가·수정된 항목을 표시했다.

```text
backend/
├── app/
│   ├── agent/
│   │   ├── v3/
│   │   │   ├── turn.py              [수정] 분류 뒤 공종별 벡터 검색 준비
│   │   │   ├── gate.py
│   │   │   ├── orchestrator.py      [수정] 준비한 복합 질문 조각을 재사용
│   │   │   ├── guide.py
│   │   │   ├── trades.py            [수정] 공종별 기본 설정 관리
│   │   │   ├── trade_agent.py       [수정] 공종별 실행 함수 선택
│   │   │   ├── answer.py            [수정] 공종별 검색 함수 선택
│   │   │   ├── llm_mode.py          [수정] 공종별 답변 지침 적용
│   │   │   ├── schemas.py           [수정] 등록된 공종 코드 검증
│   │   │   ├── quote_explain.py
│   │   │   ├── schedule_agent.py
│   │   │   ├── prompts/
│   │   │   │   └── trade_v1.md
│   │   │   └── specialists/         [추가]
│   │   │       ├── __init__.py       공종 폴더 자동 등록
│   │   │       ├── _template/        새 공종 복사용 템플릿
│   │   │       │   ├── README.md          복사와 설정 방법
│   │   │       │   ├── agent.py           공종 답변 처리 시작점
│   │   │       │   ├── spec.py            공종 기본 설정 예시
│   │   │       │   ├── retrieval.py       기본 키워드 검색
│   │   │       │   └── prompt_v1.md       공종별 답변 지침 예시
│   │   │       └── dobae/
│   │   │           ├── agent.py           도배 에이전트 실행 시작점
│   │   │           ├── spec.py            도배 기본 설정
│   │   │           ├── retrieval.py       도배 키워드 검색
│   │   │           └── prompt_v1.md       도배 전용 답변 지침
│   │   └── cards/
│   │       ├── dobae.jsonl
│   │       └── mokgong.jsonl
│   ├── chat/
│   │   ├── router.py
│   │   ├── service.py               [수정] 질문 임베딩과 벡터 후보 조회 연결
│   │   └── tools.py                 [수정] 업무 도구 4종 정의
│   ├── knowledge/
│   │   ├── service.py               [수정] 활성 버전 색인과 벡터 후보 조회
│   │   ├── search3.py               [수정] 키워드·벡터 후보의 공통 결과 형식
│   │   └── cards.py                 [수정] 등록된 공종 기준으로 답변 카드 확인
│   ├── calc/
│   ├── project/
│   ├── visit/
│   ├── quote/
│   └── schedule/
└── tests/
    ├── test_v3_ai_api.py             [수정] 활성 자료 벡터 조회 검증
    └── test_v3_trade_specialists.py  [추가] 자동 등록·공종별 연결 테스트

evaluation/
└── v3/
    └── trades/
        └── dobae/                   [추가] 도배 평가 데이터
            └── questions.jsonl      평가 질문과 기대 결과
```

## 질문 처리 흐름

1. `POST /ai/turns`를 받은 `chat/service.py`가 대화 정보와 활성 지식 자료의 키워드 색인을 준비한다.
2. `turn.py`가 `gate.py`의 결과로 담당을 고른다. 단일 공종 질문은 해당 담당을 호출하고, 복합 요청은 `orchestrator.py`가 나눠 호출한 뒤 답변을 합친다.
3. `trade_agent.py`가 등록된 공종 전용 실행 함수를 선택한다. 도배의 `agent.py`는 현재 공통 상담을 호출한다.
4. LLM 모드에서 담당 공종의 `vector_query()`가 검색어를 반환하면, `turn.py`가 해당 공종의 검색어로 후보를 요청한다. `chat/service.py`가 임베딩을 만든 뒤 키워드 색인과 같은 활성 자료 버전에서 벡터 후보를 조회한다. 복합 요청은 공종별 질문 조각으로, 되물음은 원래 질문과 선택한 값을 합친 문장으로 조회한다. 카드로 바로 이어지는 되물음은 조회를 건너뛴다.
5. `answer.py`는 등록된 공종의 `retrieval.py`를 호출한다. 도배는 무료·LLM 모드에서 공통 `knowledge/search3.py`의 키워드 검색을 사용한다. 공통 답변 흐름은 검색 근거를 바탕으로 카드, 자료 발췌, 확인 안내 중 필요한 답을 고른다.
6. LLM 모드에서 일부 답변을 다시 쓸 때 `llm_mode.py`가 공통 지침에 등록된 공종 지침을 덧붙인다. 완성된 답은 검사하고 저장한 뒤 채팅으로 보낸다.

[전체 v3 에이전트 설계](agents.md) · [답변 카드 기준](adr/agent-07-answer-cards.md)

## 공종 에이전트 추가 방법

1. [공종 템플릿 사용법](../../backend/app/agent/v3/specialists/_template/README.md)에 따라 `_template/` 전체를 복사한다. 목록에 있는 공종은 등록된 코드로, 새 공종은 새 코드로 폴더명을 정한다. 목록에 있는 공종은 `trades.py`의 기존 설정을 복사한 `spec.py`로 옮긴다. 목록에 없는 공종은 이름, 분류 단어, 지식 태그, 공정 순서를 채운다. 같은 위치에서 다른 공종의 분류 단어와 겹치면 `route_priority`로 우선순위를 정할 수 있다.
2. 복사한 `retrieval.py`에서 검색, `prompt_v1.md`에서 답변 지침, `agent.py`에서 답변 처리 순서를 개발한다. 서버는 각 파일 이름을 기준으로 자동 연결한다.
3. 벡터 검색을 쓸 공종은 같은 `retrieval.py`에 `vector_query(question)`를 둔다. 이 함수가 반환한 검색어를 공통 백엔드가 조회한다. `None`을 반환하면 조회를 건너뛴다. `retrieve()`에서 `kb.vector_rids[(공종 코드, 질문)]`의 후보를 사용해 자료 범위와 순위를 정한다.
4. 서버를 새로 시작해 반영한다. [도배 평가 데이터](../../evaluation/v3/trades/dobae/questions.jsonl)와 [연결 테스트](../../backend/tests/test_v3_trade_specialists.py)를 참고해 새 공종의 검색·프롬프트와 기존 공통 동작을 검증한다.

답변 카드는 `backend/app/agent/cards/`에서 관리한다. 새 공종의 견적 계산과 업체 연결은 해당 업무 기능이 지원하는 범위를 따른다.

## 로컬 검증

- 에이전트 테스트 132개와 PostgreSQL·채팅 API 통합 테스트 35개를 통과했다.
- 임시 방수 공종 폴더를 만들어 공종 분류, 버튼 입력, 전용 프롬프트 선택을 확인했다.
- 벡터 검색을 켠 테스트 공종이 PostgreSQL의 활성 자료 벡터만 조회하는지 검증한다. 질문 임베딩은 테스트에서 정해진 벡터로 대체한다.
- 채팅 API의 공종 코드 타입을 다시 생성했다. 서버는 실제 등록된 코드인지 검증한다.

## 업무 도구의 위치와 범위

[chat/tools.py](../../backend/app/chat/tools.py)의 `WorkTool`에 도구 이름을 정의했다. 아래 네 가지의 AI 실행 연결은 후속이다.

| 도구 | 담당 범위 |
| --- | --- |
| 폼 자동 입력·관리 | 대화에서 양식에 넣을 값을 뽑고 빠진 필수 항목을 안내한다. |
| 실측 일정 조율·관리 | 고객 희망일과 업체 가능 기간을 확인해 방문 시간 선택지를 안내한다. |
| 업체 견적서 작성·관리 | 업체 견적 입력 초안을 만들고 고객의 견적 비교를 돕는다. |
| 프로젝트 진행 상태 관리 | 현재 단계와 공사 일정·추가 금액·완료 확인 등 다음 할 일을 안내한다. |
