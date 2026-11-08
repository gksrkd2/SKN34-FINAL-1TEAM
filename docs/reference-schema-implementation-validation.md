# 기준 데이터·RAG 스키마 적용 검증

- 일자: 2026-11-09
- 작업 브랜치: feat-reference-data-schema
- 이슈: https://github.com/SKNETWORKS-FAMILY-AICAMP/SKN34-FINAL-1TEAM/issues/61
- 새 revision: 0019_reference_data (이전 0018_v2_checks)

## 적용 결과

- SQLAlchemy 모델과 migration으로 7개 테이블·99개 컬럼을 추가했다.
- PostgreSQL 17.11 Alpine 기반 이미지를 유지하며 vector 0.8.7을 구성했다. Python pgvector는 0.5.1이다.
- 개발 DB에 새 revision을 적용했고 재실행도 무변경으로 통과했다.
- rag_chunks, ref_dataset_versions, ref_dataset_events, ref_floorplan_rows, ref_unit_price_rows, ref_productivity_rows, ref_wage_rows는 모두 0행이다.
- 기존 35개 테이블은 적용 전후 건수와 전체 행의 정렬 해시가 모두 같았다. 기존 볼륨을 유지했고 서비스 테이블은 변경하지 않았다.
- 전처리 파일 읽기·적재와 OpenAI 호출은 구현·실행하지 않았다. migration 명령을 명시적으로 덮어써 개발 DB 시드를 실행하지 않았다.
- 검증용 합성 데이터와 고정 벡터는 분리된 일회성 PostgreSQL/Redis에서만 사용했다. 테스트 컨테이너는 검증 뒤 종료했다.

## 로컬에서 수행한 검사

| 검사 | 결과 |
| --- | --- |
| uv run --locked pytest -q | 전체 회귀 393개 통과, 479.44초. 마지막 ORM 사례 추가 전 실행 |
| uv run --locked pytest -q tests/test_reference_schema.py | ORM 벡터 왕복을 포함한 최종 신규 테스트 22개 통과 |
| uv run --locked ruff check . | 통과 |
| uv run --locked ruff format --check . | 174개 파일 통과 |
| alembic heads | 0019_reference_data 단일 head |
| alembic check | 개발·격리 테스트 DB 모두 새 차이 없음 |
| 테스트 fixture migration | 격리 DB upgrade → downgrade base → upgrade 통과 |
| 개발 DB alembic upgrade head | 적용·재실행 통과, 시드 명령 생략 |
| DB pgvector 이미지 빌드 | 소스 SHA-256 고정, Alpine 계열 유지, 통과 |
| 운영 API 이미지 빌드·import | 7개 모델/99개 컬럼·API import 통과. 실행 중인 API/worker는 교체하지 않음 |
| docker compose config --quiet | 통과 |
| python3 .github/scripts/check_repo.py / git diff --check | 통과 |

pytest와 테스트 DB의 alembic check는 별도 DB 호스트·비밀번호·Redis를 주입한 격리 컨테이너에서 실행했다. 위 두 pytest 결과는 서로 겹치므로 합산하지 않는다.

## 주요 검증 범위

- 파일 중복 적재 UNIQUE NULLS NOT DISTINCT와 계열별 활성본 부분 UNIQUE
- 버전 종류 복합 FK, 부모 지연 FK, 다른 버전 부모·고아·자기 부모 차단
- 20자리 소수 정밀도·음수 보정 보존, NaN·음수 임금·잘못된 근무시간 차단
- 미조사 NULL과 실제 0 구분, 직종·기준일·근무시간 자연키
- 잘못된 페이지 배열·빈 본문·영벡터 차단, SQL 및 ORM 벡터 왕복
- 최초·후속 이력 행의 상태·순번·actor 제약

상태 전이 service, 자료 수집/승인, CSV 매핑, RAG 검색과 임베딩 기능은 이번에 구현하지 않았다. 모델별 실제 벡터 차원·입력 해시·승인 상태 완전성 검사는 해당 기능 구현 시 필요하다.

## CI와 미수행 항목

- CI의 PostgreSQL 서비스 이미지를 pgvector 0.8.7 PG17로 바꾸고 로컬 DB 이미지 빌드 검사도 추가했다.
- 커밋·푸시·PR·배포는 요청 범위가 아니므로 수행하지 않았다. 새 변경의 실제 CI는 검증 대기다.
- AWS RDS 적용·운영 배포·실제 검색 품질/성능·임베딩 모델 선택·데이터 적재는 미수행이다.
- 적용 전 DB 백업은 WSL /tmp/mitjip-before-reference-schema-1e3278vo.dump에 권한 600으로 보관했다. 저장소에는 포함하지 않았다.

## GitHub 이슈 생성

GitHub 앱 연동은 두 저장소에서 403(Resource not accessible by integration)을 반환했다. 기존 Git 인증 계정의 팀 저장소 권한을 확인한 뒤 그 인증 경로로 이슈 #61을 생성·재조회했다. 인증 값은 출력하거나 파일에 기록하지 않았다. 앱의 OAuth/승인 설정은 변경하지 않았으므로 앱 연동의 403 자체를 수정했다고 보고하지 않는다.
