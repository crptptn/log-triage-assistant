"""MCP-сервер (stdio) с инструментами только на чтение.

Подключается к Claude Desktop, MCP Inspector или любому MCP-клиенту:
    python -m triage mcp
Все инструменты помечены readOnlyHint=True и destructiveHint=False — клиент видит,
что они ничего не меняют. Ограничения и аудит — в triage/tools.py.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from triage.config import get_settings
from triage.tools import LogTools, ToolError, open_tools

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

server = MCPServer(
    name="log-triage",
    instructions=(
        "Инструменты для разбора инцидентов по логам тестового стенда. Только чтение. "
        "Время в логах — UTC. Окно выгрузки логов — не больше 15 минут."
    ),
)
_tools: LogTools | None = None


def get_tools() -> LogTools:
    global _tools
    if _tools is None:
        _tools = open_tools(get_settings())
    return _tools


def set_tools(tools: LogTools) -> None:
    """Для тестов: подставить инструменты с индексом в памяти."""
    global _tools
    _tools = tools


def _safe(fn, *args, **kwargs) -> dict:
    try:
        return fn(*args, **kwargs)
    except ToolError as exc:
        return {"error": str(exc)}


@server.tool(annotations=READ_ONLY)
def list_services() -> dict:
    """Список сервисов стенда, по которым есть логи, и дата логов."""
    return _safe(get_tools().list_services)


@server.tool(annotations=READ_ONLY)
def search_logs(
    query: str,
    service: str | None = None,
    since: str | None = None,
    until: str | None = None,
    errors_only: bool = False,
    limit: int = 5,
) -> dict:
    """Семантический поиск по фрагментам логов. Время в UTC: «14:30» или «2026-05-14 14:30»."""
    return _safe(get_tools().search_logs, query, service, since, until, errors_only, limit)


@server.tool(annotations=READ_ONLY)
def get_log_window(service: str, start: str, end: str, max_lines: int = 100) -> dict:
    """Строки лога одного сервиса за интервал не длиннее 15 минут (с маскированием секретов)."""
    return _safe(get_tools().get_log_window, service, start, end, max_lines)


@server.tool(annotations=READ_ONLY)
def search_runbooks(query: str, limit: int = 3) -> dict:
    """Поиск по регламентам: известные проблемы, как их подтвердить и что делать."""
    return _safe(get_tools().search_runbooks, query, limit)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
