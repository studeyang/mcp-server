"""把 archery 的 /mcp/query/ 包成标准 MCP(stdio) 服务器。

Archery 服务端不实现 JSON-RPC/MCP 协议，只提供一个普通 HTTP 接口
（POST /mcp/query/），所以这里用 MCP Python SDK 做一层薄封装，
让 Claude Desktop / IDE / WorkBuddy 等 MCP 客户端都能直接接。

启动：
    python -m pip install "mcp>=1.2"     # 1.x(FastMCP) 与 2.x(MCPServer) 都兼容
    ARCHERY_ROOT=http://archery.domain.com ARCHERY_TOKEN=ark_xxx python archery_mcp_server.py

客户端配置（stdio，ARCHERY_ROOT 与 ARCHERY_TOKEN 均为必填，无默认值）：
    {"mcpServers": {"archery-sqlquery": {
        "command": "python",
        "args": ["<abs>/archery_mcp_server.py"],
        "env": {"ARCHERY_ROOT": "http://archery.domain.com", "ARCHERY_TOKEN": "ark_xxx"}
    }}}

工具：
    archery_list_instances  列出实例
    archery_list_databases  列出某实例下的库
    archery_list_tables     列出某库下的表
    archery_list_columns    列出某表字段
    archery_query           执行只读 SQL（自动展开 SELECT *）
"""

from __future__ import annotations

import json
from typing import Any

from archery_client import ArcheryClient, ArcheryError


def _make_server() -> Any:
    """兼容 mcp 1.x 的 FastMCP 与 2.x 的 MCPServer。"""
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP("archery-sqlquery")
    except ModuleNotFoundError as exc:
        if "renamed" not in str(exc) and "No module named" not in str(exc):
            raise
    from mcp.server.mcpserver import MCPServer  # mcp 2.x

    return MCPServer("archery-sqlquery")


mcp = _make_server()
client = ArcheryClient()


def _fail(exc: ArcheryError) -> str:
    return json.dumps({"ok": False, "kind": exc.kind, "message": exc.message}, ensure_ascii=False)


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def archery_list_instances(keyword: str = "") -> str:
    """列出 Archery 中可查询的数据库实例。keyword 可按实例名模糊过滤。"""
    try:
        items = client.list_instances()
        if keyword:
            low = keyword.lower()
            items = [i for i in items if low in str(i.get("instance_name", "")).lower()]
        slim = [
            {
                "id": i.get("id"),
                "instance_name": i.get("instance_name"),
                "db_type": i.get("db_type"),
                "type": i.get("type"),
            }
            for i in items
        ]
        return _dump({"ok": True, "count": len(slim), "instances": slim})
    except ArcheryError as exc:
        return _fail(exc)


def archery_list_databases(instance_name: str) -> str:
    """列出某实例下的所有数据库名。instance_name 必须带端口，如 es-friendsdata-s1n:3309。"""
    try:
        inst = client.resolve_instance(instance_name)
        return _dump({"ok": True, "instance": inst["instance_name"], "databases": client.list_databases(inst)})
    except ArcheryError as exc:
        return _fail(exc)


def archery_list_tables(instance_name: str, db_name: str) -> str:
    """列出某库下的所有表名。"""
    try:
        inst = client.resolve_instance(instance_name)
        return _dump({"ok": True, "db": db_name, "tables": client.list_tables(inst, db_name)})
    except ArcheryError as exc:
        return _fail(exc)


def archery_list_columns(instance_name: str, db_name: str, table_name: str) -> str:
    """列出某表的字段名（写 SQL 前建议先取字段）。"""
    try:
        inst = client.resolve_instance(instance_name)
        return _dump({"ok": True, "table": table_name, "columns": client.list_columns(inst, db_name, table_name)})
    except ArcheryError as exc:
        return _fail(exc)


def archery_query(instance_name: str, db_name: str, sql: str, limit: int = 100) -> str:
    """执行只读 SQL（select/with/show）。两点注意：
    1) 服务端禁止 SELECT *，会被直接拦截；本工具会自动展开为显式列名后重试。
    2) instance_name 必须带端口，不能省略。
    返回 columns / rows / affected_rows / cost_ms。
    """
    try:
        result = client.query_expand_star(instance_name, db_name, sql, limit)
        return _dump({"ok": True, **result})
    except ArcheryError as exc:
        return _fail(exc)


# 注册工具：两种 API 的装饰器一致，描述取自 docstring
for _fn in (
    archery_list_instances,
    archery_list_databases,
    archery_list_tables,
    archery_list_columns,
    archery_query,
):
    mcp.tool(name=_fn.__name__, description=_fn.__doc__)(_fn)


if __name__ == "__main__":
    transport = "stdio"
    try:
        mcp.run(transport=transport)
    except TypeError:  # mcp 1.x 的 run() 不接受 transport
        mcp.run()
