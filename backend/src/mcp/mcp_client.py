"""
MCP Client
Dispatches calls to registered tool adapters (GitHub, Jira, AWS, etc.)
"""

import logging

logger = logging.getLogger(__name__)


class MCPClient:
    def __init__(self):
        self._adapters: dict = {}

    def register_adapter(self, tool_name: str, adapter) -> None:
        self._adapters[tool_name] = adapter
        print(f"🔌 MCP adapter registered: {tool_name}")

    async def call(self, tool: str, action: str, params: dict = None) -> dict:
        params = params or {}
        adapter = self._adapters.get(tool)
        if not adapter:
            raise RuntimeError(f"No adapter found for tool: '{tool}'. Register it first.")

        logger.info(f"📡 MCP Call → {tool}.{action}")
        try:
            result = await adapter.call(action, params)
            return {"success": True, "tool": tool, "action": action, "data": result}
        except Exception as e:
            logger.error(f"❌ MCP Error [{tool}.{action}]: {e}")
            return {"success": False, "tool": tool, "action": action, "error": str(e)}

    def available_tools(self) -> list:
        return list(self._adapters.keys())
