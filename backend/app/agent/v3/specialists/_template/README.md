# 공종 에이전트 템플릿 사용법

## 새 공종 폴더 만드는 법

**템플릿 폴더를 통째로 복사해요. 공종이 목록에 있으면 기존 설정을 가져오고, 없다면 복사한 `spec.py`에 새로 작성해요.**

1. **폴더 이름을 정해요.** [공종 목록](../../trades.py)에서 목공을 찾으면 코드는 `mokgong`이에요. 목록에 없는 공종은 `acoustic_panel`처럼 겹치지 않는 이름을 정해요. 이름은 영문 소문자로 시작하고 소문자·숫자·밑줄을 써요. `common`은 공통 설정에 쓰는 이름이에요.
2. **템플릿을 통째로 복사해요.** 목공이라면 `specialists/_template/`을 복사해서 `specialists/mokgong/`을 만들어요.
3. **복사한 `spec.py`를 채워요.** [목록에 있으면 기존 설정을 옮기고](#목록에-있는-공종의-specpy-작성법), [목록에 없으면 새로 작성해요](#목록에-없는-공종의-specpy-작성법).
4. **서버를 다시 시작하고 질문해 봐요.** 목공이라면 “몰딩을 바꾸려면 뭘 확인해야 해요?”처럼 질문해요. 서버는 복사한 폴더의 설정을 쓰고, 처음 만든 공종은 목록에 자동으로 추가해요. 검색에 쓸 자료는 활성 상태로 등록돼 있어야 답변의 근거로 쓸 수 있어요.

### 어떤 파일을 바꾸면 되나요?

| 바꾸려는 것 | 파일 |
| --- | --- |
| 어떤 질문을 이 공종이 맡는지 | `spec.py` |
| 답변에 쓸 자료를 어떻게 찾는지 | `retrieval.py` |
| AI가 답변에 어떤 내용을 담는지 | `prompt_v1.md` |
| 질문을 어떤 순서로 처리하는지 | `agent.py` |

## 파일별 작성 예시

### 목록에 있는 공종의 `spec.py` 작성법

1. `trades.py`에서 `"mokgong"`을 찾아 목공의 `TradeSpec(...)` 안에 있는 값을 전부 복사해요.
2. `mokgong/spec.py`의 `return spec_type(...)` 안에 붙여 넣어요. 목공의 되묻는 질문처럼 `Slot(...)`으로 적힌 부분은 `slot_type(...)`으로 바꿔요.

이렇게 하면 분류 단어, 실측 항목, 되묻는 질문, 견적 연결 설정을 폴더에서도 그대로 쓸 수 있어요. `trades.py`에 원래 설정을 둔 채로 작업해도 서버는 같은 코드의 폴더 설정을 우선 사용해요. 실제로 옮긴 예시는 도배에서 볼 수 있어요. [목공 설정 원본](../../trades.py) · [도배 spec.py](../dobae/spec.py)

### 목록에 없는 공종의 `spec.py` 작성법

흡음 패널이라면 템플릿을 `acoustic_panel/`로 복사하고 아래처럼 채워요. 공종 코드는 폴더 이름에서 자동으로 가져와요. 질문 단어는 `|`로 구분하고, 자료 이름과 공사 순서는 실제 자료와 팀 기준에 맞춰요.

```python
from pathlib import Path


def build(spec_type, slot_type):
    return spec_type(
        code=Path(__file__).parent.name,
        ko="흡음 패널",
        depth="knowledge",  # 시공 정보 안내
        words=r"흡음\s*패널|흡음재",
        tags=("흡음 패널",),
        order=10,  # 공사 순서 예시. 작을수록 먼저
    )
```

견적 계산과 업체 연결은 해당 업무 기능이 지원하는 공종에서 사용할 수 있어요.

### `retrieval.py` · 검색 방법

기본은 키워드 검색이에요. 아래 예시는 고객이 붙여 쓴 ‘흡음패널’을 검색할 때 표현을 하나 더 넣어요. 출처 제한은 원래 질문으로 판단하도록 `policy_text`도 넘겨요. `trades`는 해당 공종 자료의 검색 순위에 반영돼요.

```python
from pathlib import Path

from app.knowledge import search3


def retrieve(kb, question):
    code = Path(__file__).parent.name
    query = f"흡음 패널 {question}" if "흡음패널" in question else question
    return search3.search(kb.ix, query, trades=[code], tags=kb.tags, k=8, policy_text=question)
```

벡터 검색이 필요한 공종은 `vector_query(question)`를 추가하고 `retrieve()`에서 후보를 사용해요. 템플릿과 현재 도배는 키워드 검색을 사용해요. [공종별 벡터 연결 방법](../../../../../../docs/v3/trade-specialists.md#공종-에이전트-추가-방법)

### `prompt_v1.md` · LLM 답변 지침

LLM 모드에서 답변 일부를 다시 쓸 때 공통 지침 뒤에 이 파일을 붙여요.

```markdown
# 흡음 패널 답변 지침 v1

- 흡음과 차음의 차이는 제공된 근거에 있을 때 설명해.
- 제품별 성능을 확인할 자료가 없으면 현장에서 확인할 조건을 안내해.
```

### `agent.py` · 답변 처리 순서

기본 답변 흐름을 그대로 쓰려면 아래처럼 호출해요. 이 공종에만 별도의 처리 순서가 필요할 때 수정해요.

```python
def run(task, kb, run_common):
    return run_common(task, kb)
```

## AI에 작업을 맡기는 방법

아래 요청문에서 대괄호를 채워 사용해요.

```text
공종 이름은 [목공]이고, 폴더로 쓸 공종 코드는 [mokgong]이야.
목표는 [수정 목적]이고, 예시 질문은 [질문]이야. 기대 결과는 [담당 공종과 찾을 근거 또는 답변]이야.
목록에 없는 공종이라면 질문에서 찾을 단어는 [단어], 자료 태그는 [실제 태그], 공정 순서는 [팀 기준 순서]야.

specialists/_template/README.md와 템플릿 파일을 읽어.
목록에 있는 공종은 trades.py에서 폴더 이름으로 쓸 코드를 확인해. 새 공종이면 요청한 코드를 사용해.
새 공종 폴더를 만들 때는 템플릿 전체를 복사하고 그 코드로 폴더명을 정해. 같은 폴더가 이미 있다면 현재 파일을 확인하고 필요한 부분을 수정해.
목록에 있는 공종은 trades.py의 해당 TradeSpec에 들어 있는 값을 복사본 spec.py의 spec_type(...)에 옮겨. Slot(...)은 slot_type(...)으로 바꾸고 설정값은 모두 보존해.
목록에 없는 공종은 요청 내용과 공종 자료를 기준으로 새 spec.py 값을 작성해.
spec.py의 코드와 폴더명이 일치하는지 확인하고, 아래 검증 항목에 따라 예시 질문을 확인해.
수정한 파일과 확인 결과를 알려줘.
```

AI는 작업할 때 다음 연결을 확인해요.

- `spec.py`는 `build(spec_type, slot_type)`, `retrieval.py`는 `retrieve(kb, question)`, `agent.py`는 `run(task, kb, run_common)`으로 연결돼요. LLM 답변 지침은 `prompt_v1.md` 이름으로 읽어요. 먼저 대상 공종 폴더에서 변경을 끝낼 수 있는지 확인해요.
- 키워드 검색 개선은 `search3.search()`를 재사용해요. 검색어를 보강하면 원래 질문을 `policy_text`로 넘겨요. 벡터가 필요하면 공통 조회 결과인 `kb.vector_rids`를 쓰고, 후보가 없을 때 키워드 결과로 답할 수 있게 해요.
- 분류 결과는 예시 질문으로 확인하고, 검색을 바꿨다면 어떤 근거가 검색되는지 확인해요. 프롬프트를 바꿨다면 LLM 모드에서 그 지침이 선택되는지 확인해요. 답변 처리 순서를 바꿨다면 기존 공통 흐름을 쓰는 질문도 확인해요. [공종 연결 테스트 예시](../../../../../tests/test_v3_trade_specialists.py)
