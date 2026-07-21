"""
The adapters the global search runs, fixed at import time.

A plain tuple rather than app discovery or a settings-driven plugin list: the
set of searchable content types is a product decision, and it should be
readable in one place instead of assembled at runtime. Adapters are stateless,
so module-level instances are safe to share.

The glossary is deliberately absent. It is a separate content model with its
own search surface, and mixing 142 term definitions per language into a result
list of roughly 60 editorial objects would drown them.
"""
from __future__ import annotations

from search.adapters.base import SearchAdapter
from search.adapters.comparisons import ComparisonSearchAdapter
from search.adapters.guides import GuideSearchAdapter
from search.adapters.prompts import PromptSearchAdapter
from search.adapters.tools import ToolSearchAdapter
from search.adapters.usecases import UseCaseSearchAdapter

#: One adapter per searchable content type.
#:
#: The order here does not influence result order - the service sorts every
#: result together, once. It only fixes the order adapters run in, which is
#: observable when one of them fails.
SEARCH_ADAPTERS: tuple[SearchAdapter, ...] = (
    ToolSearchAdapter(),
    GuideSearchAdapter(),
    PromptSearchAdapter(),
    UseCaseSearchAdapter(),
    ComparisonSearchAdapter(),
)
