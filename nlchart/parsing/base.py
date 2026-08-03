from typing import Protocol

from ..spec import ChartSpec


class ParseError(ValueError):
    pass


class NLParser(Protocol):
    def parse(self, text: str) -> ChartSpec: ...
