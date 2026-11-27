"""공종 폴더에서 기본 설정과 전용 실행·검색·프롬프트를 읽는다."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.agent.v3.answer import KB, Part, Task
    from app.agent.v3.trades import Slot, TradeSpec
    from app.knowledge.bm25 import Result

ROOT = Path(__file__).parent

type TradeRunner = Callable[[Task, KB], Part]


class QuestionInterpreter(Protocol):
    def __call__(self, question: str, *, context: str = "") -> Awaitable[object]: ...


@dataclass(frozen=True)
class Specialist:
    retrieve: Callable[[KB, str], Result] | None = None
    prompt_path: Path | None = None
    run: Callable[[Task, KB, TradeRunner], Part] | None = None
    vector_query: Callable[[str], str | None] | None = None
    interpret_question: QuestionInterpreter | None = None


def _folders(root: Path) -> list[Path]:
    entry_files = ("spec.py", "agent.py", "retrieval.py", "prompt_v1.md")
    return [
        path
        for path in sorted(root.iterdir())
        if path.is_dir()
        and re.fullmatch(r"[a-z][a-z0-9_]*", path.name)
        and any((path / name).is_file() for name in entry_files)
    ]


def load_specs(
    root: Path, spec_type: type[TradeSpec], slot_type: type[Slot]
) -> tuple[TradeSpec, ...]:
    out = []
    for path in _folders(root):
        if not (path / "spec.py").is_file():
            continue
        spec = import_module(f".{path.name}.spec", __name__).build(spec_type, slot_type)
        if spec.code != path.name or spec.code == "common":
            raise ValueError(f"공종 설정 코드가 폴더와 일치해야 합니다: {path.name}")
        out.append(spec)
    return tuple(out)


def load_specialists(root: Path, known: Mapping[str, TradeSpec]) -> dict[str, Specialist]:
    out = {}
    for path in _folders(root):
        code = path.name
        if code not in known:
            raise ValueError(f"새 공종 폴더에 spec.py가 필요합니다: {code}")
        retrieval = (
            import_module(f".{code}.retrieval", __name__)
            if (path / "retrieval.py").is_file()
            else None
        )
        agent = import_module(f".{code}.agent", __name__) if (path / "agent.py").is_file() else None
        prompt = path / "prompt_v1.md"
        out[code] = Specialist(
            retrieve=retrieval.retrieve if retrieval else None,
            vector_query=getattr(retrieval, "vector_query", None),
            run=getattr(agent, "run", None),
            prompt_path=prompt if prompt.is_file() else None,
            interpret_question=getattr(agent, "interpret_question", None),
        )
    return out


@cache
def registered() -> dict[str, Specialist]:
    # 설정을 먼저 조립한 뒤 실행 모듈을 읽어 공통 타입과의 순환 import를 피한다.
    from app.agent.v3.trades import BY_CODE

    return load_specialists(ROOT, BY_CODE)
