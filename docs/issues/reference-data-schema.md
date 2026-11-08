# feat: 전처리 기준 데이터와 RAG 저장 스키마 추가

정식 작업 이슈: https://github.com/SKNETWORKS-FAMILY-AICAMP/SKN34-FINAL-1TEAM/issues/61

- 0019_reference_data로 7개 테이블·99개 컬럼과 vector 확장을 추가했다.
- 기존 볼륨·기존 35개 테이블의 데이터는 유지했고 새 7개 테이블은 모두 0행이다.
- Python pgvector 0.5.1, PostgreSQL pgvector 0.8.7을 사용한다.
- 전체 회귀 393개, 추가 ORM 사례를 포함한 신규 스키마 테스트 22개 통과. 모델 차이·린트·이미지·Compose 검사 통과.
- 전처리 적재·개발 DB 시드·임베딩 생성·Git commit/push는 하지 않았다. CI는 검증 대기다.
- 상세 결과: [적용 검증](../reference-schema-implementation-validation.md)

GitHub 앱 연동의 403 때문에 처음 로컬에 보존한 이슈다. 이후 기존 Git 인증으로 실제 등록을 확인했다. 앱의 권한 설정은 변경하지 않았다.
