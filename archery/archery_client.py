"""Archery 只读 SQL 查询客户端（纯标准库，无第三方依赖）。

接口：
- POST /mcp/query/                 只读 SQL 查询，Bearer Token 认证
- GET  /api/v1/instance/           实例列表（Django REST 分页，next 为完整 URL）
- POST /api/v1/instance/resource/  库 / 表 / 字段元数据

注意：服务端不实现标准 MCP(JSON-RPC) 协议，/mcp/query/ 只是普通 JSON 接口，
需要自己包一层才能给支持 MCP 的客户端用（见 archery_mcp_server.py）。

环境变量（均为必填）：
    ARCHERY_ROOT   Archery 服务根地址，如 http://archery.domain.com
    ARCHERY_TOKEN  Bearer Token（ark_ 开头）
"""

from __future__ import annotations

import difflib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional



class ArcheryError(Exception):
    """接口返回的业务错误（status=1）。"""

    def __init__(self, message: str, http_status: Optional[int] = None, kind: str = "ERROR"):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.kind = kind


def _classify(message: str) -> str:
    if "频率" in message or "rate limit" in message.lower():
        return "RATE_LIMITED"
    if "不含" in message or "含有" in message:
        return "STAR_BLOCKED"
    if "不支持的查询语法" in message or "语法" in message:
        return "NOT_READ_ONLY"
    if "权限" in message or "无法校验" in message:
        return "NO_PERMISSION"
    if "不存在" in message:
        return "NOT_FOUND"
    if "未开启 MCP" in message or "can_mcp" in message:
        return "MCP_DISABLED"
    if "Token 无效" in message or "身份认证" in message:
        return "AUTH"
    return "ERROR"


class ArcheryClient:
    def __init__(
        self,
        token: Optional[str] = None,
        root: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.token = token or os.environ.get("ARCHERY_TOKEN", "")
        # root 无默认值，必须显式传入或通过 ARCHERY_ROOT 配置
        self.root = (root or os.environ.get("ARCHERY_ROOT") or "").rstrip("/")
        self.timeout = timeout
        self._instances: Optional[List[Dict[str, Any]]] = None

    # ---------------------------------------------------------------- HTTP
    def _headers(self) -> Dict[str, str]:
        if not self.token:
            raise ArcheryError("缺少 token：设置 ARCHERY_TOKEN 或显式传入。", kind="AUTH")
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload: Optional[dict] = None, url: Optional[str] = None) -> Any:
        if not self.root:
            raise ArcheryError("缺少 root：设置 ARCHERY_ROOT 或显式传入 root。", kind="CONFIG")
        target = url or (self.root + path)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(target, data=body, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            message = raw
            try:
                data = json.loads(raw)
                message = str(
                    data.get("msg") or data.get("message") or data.get("detail") or raw
                ).strip()
            except (ValueError, AttributeError):
                pass
            # 注意：限流也返回 401 + "Token 调用频率超限"，不能一律当认证失败
            kind = _classify(message)
            if exc.code == 401 and kind != "RATE_LIMITED":
                raise ArcheryError(message or "认证失败。", 401, "AUTH") from None
            raise ArcheryError(message, exc.code, kind) from None

    # ------------------------------------------------------------ 元数据
    def list_instances(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if self._instances is not None and not refresh:
            return self._instances
        url: Optional[str] = self.root + "/api/v1/instance/"
        found: List[Dict[str, Any]] = []
        for _ in range(100):
            if not url:
                break
            payload = self._request("GET", "", url=url)
            found.extend(x for x in (payload.get("results") or []) if isinstance(x, dict))
            nxt = payload.get("next")
            url = urllib.parse.urljoin(self.root + "/", nxt) if nxt else None
        self._instances = found
        return found

    def resolve_instance(self, name: str) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ArcheryError("instance 不能为空。", kind="USAGE")
        instances = self.list_instances()
        if name.isdigit():
            for item in instances:
                if str(item.get("id")) == name:
                    return item
        low = name.lower()
        # 查询接口要求 instance_name 精确到 "host:port"，端口不能省
        for item in instances:
            if str(item.get("instance_name", "")).lower() == low:
                return item
        if ":" not in name:
            hits = [i for i in instances if str(i.get("instance_name", "")).lower().startswith(low + ":")]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                names = ", ".join(str(i.get("instance_name")) for i in hits)
                raise ArcheryError(f"实例名不唯一：{name}，候选：{names}", kind="AMBIGUOUS")
        near = difflib.get_close_matches(name, [str(i.get("instance_name", "")) for i in instances], n=3, cutoff=0.45)
        raise ArcheryError("找不到实例：" + name + (f"（相近：{', '.join(near)}）" if near else ""), kind="NOT_FOUND")

    def _resources(self, instance: Dict[str, Any], resource_type: str,
                   db_name: Optional[str] = None, tb_name: Optional[str] = None) -> List[str]:
        payload: Dict[str, Any] = {"instance_id": instance.get("id"), "resource_type": resource_type}
        if db_name:
            payload["db_name"] = db_name
        if tb_name:
            payload["tb_name"] = tb_name
        result = self._request("POST", "/api/v1/instance/resource/", payload)
        values = result.get("result")
        if values is None:
            values = result.get("results") or []
        return sorted([str(v) for v in values if v is not None], key=str.lower)

    def list_databases(self, instance: Dict[str, Any]) -> List[str]:
        return self._resources(instance, "database")

    def list_tables(self, instance: Dict[str, Any], database: str) -> List[str]:
        return self._resources(instance, "table", db_name=database)

    def list_columns(self, instance: Dict[str, Any], database: str, table: str) -> List[str]:
        return self._resources(instance, "column", db_name=database, tb_name=table)

    # -------------------------------------------------------------- 查询
    def query(self, instance_name: str, db_name: str, sql: str, limit: int = 100) -> Dict[str, Any]:
        """执行只读 SQL。返回 columns / rows / affected_rows / cost_ms。"""
        instance = self.resolve_instance(instance_name)  # 顺带校验名字，端口必须带
        payload = {
            "instance_name": instance["instance_name"],
            "db_name": db_name,
            "sql_content": sql,      # 注意：不是 "sql"
            "limit_num": limit,
        }
        resp = self._request("POST", "/mcp/query/", payload)
        status = resp.get("status")
        if status not in (0, "0", None):
            message = str(resp.get("msg") or resp.get("message") or "查询失败").strip()
            raise ArcheryError(message, kind=_classify(message))
        data = resp.get("data") or {}
        columns = [str(c) for c in (data.get("columns") or [])]
        rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in (data.get("rows") or [])]

        # 防御：服务端收不到 sql_content 时不报错，而是回一个 select 1 的假成功。
        # 真实 SQL 绝不该只返回单列 "1"，遇到就说明参数没生效，必须显式失败。
        if columns == ["1"] and rows in ([], [[1]]) and "select 1" not in sql.lower():
            raise ArcheryError(
                "查询被服务端忽略，返回了默认 select 1；请检查 sql_content 是否为有效的只读语句。",
                kind="EMPTY_QUERY",
            )

        cost = data.get("cost_time")
        return {
            "columns": columns,
            "rows": rows,
            "affected_rows": data.get("affected_rows"),
            "cost_ms": round(float(cost) * 1000, 3) if isinstance(cost, (int, float)) else None,
        }

    def query_expand_star(self, instance_name: str, db_name: str, sql: str, limit: int = 100) -> Dict[str, Any]:
        """服务端禁止 SELECT *，把 * 展开成显式列名后重试。

        仅支持简单形式：select * from <table> [where/limit...]
        """
        stripped = sql.strip().rstrip(";")
        low = stripped.lower()
        if not low.startswith("select"):
            return self.query(instance_name, db_name, sql, limit)
        star_at = low.find("select *")
        if star_at < 0:
            return self.query(instance_name, db_name, sql, limit)
        rest = stripped[len("select *"):].lstrip()
        if not rest.lower().startswith("from"):
            raise ArcheryError("无法自动展开 *：SQL 结构不受支持，请显式写列名。", kind="STAR_BLOCKED")
        after = rest[len("from"):].strip()
        table = after.split()[0].split(";")[0]
        table = table.split(".")[-1].strip("`")
        instance = self.resolve_instance(instance_name)
        columns = self.list_columns(instance, db_name, table)
        if not columns:
            raise ArcheryError(f"取不到 {table} 的字段列表，无法展开 *。", kind="STAR_BLOCKED")
        tail = after[len(after.split()[0]):]
        new_sql = "select " + ",".join(f"`{c}`" if not c.isidentifier() else c for c in columns) + " from " + table + tail
        return self.query(instance_name, db_name, new_sql, limit)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="archery 只读 SQL 查询")
    ap.add_argument("--instance", required=True, help='实例名，如 es-friendsdata-s1n:3309')
    ap.add_argument("--db", required=True)
    ap.add_argument("--sql", required=True)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--expand-star", action="store_true", help="自动展开 SELECT *")
    args = ap.parse_args()

    client = ArcheryClient()
    try:
        out = (client.query_expand_star if args.expand_star else client.query)(
            args.instance, args.db, args.sql, args.limit
        )
    except ArcheryError as exc:
        print(json.dumps({"ok": False, "kind": exc.kind, "message": exc.message}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    print(json.dumps({"ok": True, **out}, ensure_ascii=False, indent=2))
