"""LLM 函数工具 devin-web-search。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent


@dataclass
class DevinWebSearchTool(FunctionTool):
    """调用 Devin（Exa 后端）执行联网搜索。"""

    plugin: Any = None
    name: str = "devin-web-search"
    description: str = (
        "Search the web using Devin web search. "
        "Use for up-to-date information, news, or facts beyond training data."
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Required. The search query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Optional. Maximum number of results, 1-10. Default 5.",
                },
            },
            "required": ["query"],
        }
    )

    async def run(
        self, event: AstrMessageEvent, query: str, max_results: int = 0
    ) -> str:
        plugin = self.plugin
        if plugin is None:
            return "Error: Devin plugin instance not available."
        return await plugin.run_tool_search(query, max_results=max_results)
