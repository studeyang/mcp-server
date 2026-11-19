"""MySQL MCP Server (stdio) —— 通用版，可被任意项目复用。

连接信息完全由配置决定，三种来源按优先级取用：
    1. MYSQL_MCP_CONFIG 环境变量指定的 JSON 配置文件
    2. 从 CLAUDE_PROJECT_DIR（Claude Code 注入的项目根）向上查找的
       .claude/mcp/mysql-mcp/mysql_mcp_config.json（项目配置）
    3. 本脚本同目录的 mysql_mcp_config.json（全局兜底）
    也可用环境变量 MYSQL_URL/MYSQL_HOST/... 直接连。

每个项目只需要在自己的 .claude/mcp/mysql-mcp/ 下放一份 mysql_mcp_config.json，
脚本本身保持不变。配置文件格式：

    {
      "default": "sit2",
      "connections": {
        "sit2": {
          "url": "jdbc:mysql://host:3306/db?useSSL=false&useUnicode=true&characterEncoding=UTF-8",
          "username": "xxx",
          "password": "xxx",
          "allow_write": false,
          "max_rows": 5000
        }
      }
    }

环境变量：
    MYSQL_MCP_CONFIG     配置文件路径（优先于自动查找）
    MYSQL_CONNECTION     未指定 connection 参数时用哪个连接（配置文件 default 优先）
    MYSQL_URL            jdbc:mysql://host:3306/db?... / mysql://host:3306/db / host:3306/db
    MYSQL_HOST / MYSQL_PORT / MYSQL_DATABASE / MYSQL_USER / MYSQL_PASSWORD
    MYSQL_NAME           连接别名（仅展示用）
    MYSQL_ALLOW_WRITE    设为 true/1 时允许写操作（默认只读）
    MYSQL_MAX_ROWS       单次查询硬上限，默认 5000
    MYSQL_TIMEOUT        连接与读超时秒数，默认 20

工具：
    mysql_query             执行 SQL（默认只读，写操作需 allow_write=true）
    mysql_list_databases    列出当前账号可见的库
    mysql_list_tables       列出某库的表（支持模糊过滤）
    mysql_describe_table    查看表结构
    mysql_show_indexes      查看表索引
    mysql_search_columns    按字段名模糊搜索，定位相关表
    mysql_list_connections  列出已配置的连接（密码打码）
    mysql_connection_info   查看当前生效的连接
    mysql_ping              测试连通性
    mysql_compare_envs      同一条只读 SQL 在多个环境执行并对比

启动：
    python -m pip install "mcp>=2" pymysql
    python mysql_mcp_server.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

import pymysql
from pymysql.cursors import DictCursor

try:
    from mcp.server.mcpserver import MCPServer  # mcp 2.x
except ModuleNotFoundError:  # pragma: no cover - 兼容 mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore

DEFAULT_PORT = 3306
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_ROWS = 5000

# 只读白名单：这些关键字开头的语句允许执行
READ_ONLY_KEYWORDS = ("select", "show", "describe", "desc", "explain", "with", "table")

# 明确的写操作关键字（allow_write 关闭时给出清晰报错）
WRITE_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "replace",
    "merge",
    "create",
    "alter",
    "drop",
    "truncate",
    "rename",
    "grant",
    "revoke",
    "call",
    "lock",
    "unlock",
    "set",
    "reset",
    "load",
    "handler",
    "import",
    "kill",
    "do",
    "use",
)

# 库表标识符白名单：拼进 SQL 前必须校验，避免注入
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_$]+$")

# 项目级配置的相对路径（相对项目根目录）
PROJECT_CONFIG_RELATIVE = Path(".claude") / "mcp" / "mysql-mcp" / "mysql_mcp_config.json"


# --------------------------------------------------------------------------
# 配置解析
# --------------------------------------------------------------------------


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _project_root() -> Path:
    """返回项目根目录。

    优先使用 Claude Code 注入给 MCP 子进程的 CLAUDE_PROJECT_DIR 环境变量（v2.1.139+，
    用户级/项目级注册都会注入）；没有时才回退到当前工作目录（本地直接运行脚本时）。
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env:
        path = Path(env).expanduser()
        return path if path.is_absolute() else (Path.cwd() / path).resolve()
    return Path.cwd()


def _find_project_config() -> Path | None:
    """从项目根向上查找项目级配置文件。"""
    start = _project_root()
    for directory in [start, *start.parents]:
        candidate = directory / PROJECT_CONFIG_RELATIVE
        if candidate.is_file():
            return candidate
    return None


def _config_path() -> Path | None:
    """确定配置文件路径，优先级：环境变量 > 项目自动查找 > 脚本同目录兜底。"""
    explicit = os.environ.get("MYSQL_MCP_CONFIG", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else (_project_root() / path).resolve()
    project = _find_project_config()
    if project:
        return project
    sibling = Path(__file__).resolve().with_name("mysql_mcp_config.json")
    return sibling if sibling.exists() else None


def load_config_file() -> dict[str, Any]:
    """读取 JSON 配置文件，返回完整内容（不存在则返回空 dict）。"""
    path = _config_path()
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # 配置坏了也要能启动，报错信息随工具返回
        return {"__error__": f"配置文件解析失败 {path}: {exc}"}


def parse_jdbc_url(url: str) -> dict[str, Any]:
    """把 JDBC / mysql:// / host:port/db 三种写法统一解析成连接参数。

    JDBC 的 host:port 之间是 ':'，标准 URL 也是 ':'，这里用正则单独抠出端口，
    避免 urlparse 把 host 吃掉。
    """
    raw = url.strip()
    raw = re.sub(r"^jdbc:", "", raw, flags=re.I)
    if raw.lower().startswith("mysql://"):
        raw = raw[len("mysql://"):]
    elif "://" in raw:
        raw = raw.split("://", 1)[1]

    m = re.match(r"^([^/?]+)(?:/([^?]*))?(\?.*)?$", raw)
    if not m:
        raise ValueError(f"无法解析连接串: {url}")
    hostpart, db_part, query_part = m.group(1), m.group(2) or "", m.group(3) or ""

    host, port = hostpart, DEFAULT_PORT
    if ":" in hostpart:
        h, _, p = hostpart.rpartition(":")
        if p.isdigit():
            host, port = h, int(p)

    # 连接串里可能带 user:pass@ 前缀（非标准但偶见）
    if "@" in host:
        host = host.rsplit("@", 1)[1]

    params = parse_qs(query_part.lstrip("?")) if query_part else {}
    params = {k.lower(): v[0] for k, v in params.items()}

    charset = params.get("characterencoding") or params.get("charset") or "utf8mb4"
    charset = charset.replace("-", "").replace("_", "")
    if charset.lower() in ("utf8", "utf"):
        charset = "utf8mb4"

    ssl_off = params.get("usessl", "").lower() in ("false", "0", "no")

    timeout = DEFAULT_TIMEOUT
    raw_timeout = params.get("connecttimeout")
    if raw_timeout:
        # JDBC 的 connectTimeout 单位是毫秒，这里统一换算成秒
        timeout = max(1, int(float(raw_timeout) / 1000))

    return {
        "host": host,
        "port": port,
        "database": unquote(db_part) if db_part else "",
        "charset": charset,
        "connect_timeout": timeout,
        "ssl_disabled": ssl_off,
        "raw_url": url.strip(),
    }


def _match_connection_key(connections: dict[str, Any], name: str) -> str | None:
    """连接名大小写不敏感匹配，返回配置文件中真实的 key。"""
    if not name:
        return None
    if name in connections:
        return name
    low = name.lower()
    for key in connections:
        if key.lower() == low:
            return key
    return None


def resolve_connection(name: str | None = None) -> dict[str, Any]:
    """按优先级合并配置：显式 name > 文件 default > 环境变量。

    返回 dict，包含 host/port/user/password/database 等实际可用参数。
    """
    cfg_file = load_config_file()
    cfg_error = cfg_file.get("__error__")

    connections: dict[str, Any] = cfg_file.get("connections") or {}
    env_url = os.environ.get("MYSQL_URL", "").strip()

    # 1) 环境变量直连（没有配置文件或对应条目时使用）
    env_conf = {
        "name": os.environ.get("MYSQL_NAME", "").strip() or "env",
        "url": env_url,
        "host": os.environ.get("MYSQL_HOST", "").strip(),
        "port": int(os.environ.get("MYSQL_PORT") or DEFAULT_PORT),
        "username": os.environ.get("MYSQL_USER", "").strip(),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": os.environ.get("MYSQL_DATABASE", "").strip(),
        "allow_write": _truthy(os.environ.get("MYSQL_ALLOW_WRITE", "false")),
        "max_rows": int(os.environ.get("MYSQL_MAX_ROWS") or DEFAULT_MAX_ROWS),
        "timeout": int(os.environ.get("MYSQL_TIMEOUT") or DEFAULT_TIMEOUT),
    }

    # 2) 选定目标连接名
    target = (
        name
        or os.environ.get("MYSQL_CONNECTION", "").strip()
        or str(cfg_file.get("default") or "")
    ).strip()

    entry: dict[str, Any] | None = None
    key = _match_connection_key(connections, target)
    if key:
        entry = dict(connections[key])
        entry.setdefault("name", key)
    elif name:
        # 显式点了名却找不到，必须报错，不能静默回退到环境变量
        if not connections:
            raise KeyError(
                f"指定了连接 '{name}'，但未找到可用配置文件"
                f"（当前: {_config_path() or '无'}）。请检查 MYSQL_MCP_CONFIG。"
            )
        raise KeyError(
            f"配置文件中没有名为 '{name}' 的连接，可用: {', '.join(sorted(connections))}"
        )

    if entry is not None:
        entry.setdefault("port", DEFAULT_PORT)
        entry.setdefault("timeout", DEFAULT_TIMEOUT)
        entry.setdefault("max_rows", DEFAULT_MAX_ROWS)
        entry.setdefault("allow_write", False)
        conf = entry
    else:
        if cfg_error:
            raise RuntimeError(cfg_error)
        conf = env_conf

    conf.setdefault("name", "default")
    conf.setdefault("port", DEFAULT_PORT)
    conf.setdefault("timeout", DEFAULT_TIMEOUT)
    conf.setdefault("max_rows", DEFAULT_MAX_ROWS)
    conf.setdefault("allow_write", False)

    # 3) 从 url 补全/覆盖 host/port/database/charset
    parsed: dict[str, Any] = {}
    url = str(conf.get("url") or "").strip()
    if url:
        try:
            parsed = parse_jdbc_url(url)
        except Exception as exc:
            raise RuntimeError(f"连接串解析失败: {exc}") from exc

    conf["host"] = conf.get("host") or parsed.get("host", "")
    conf["port"] = int(conf.get("port") or parsed.get("port") or DEFAULT_PORT)
    conf["database"] = conf.get("database") or parsed.get("database", "")
    conf["charset"] = conf.get("charset") or parsed.get("charset") or "utf8mb4"
    conf["timeout"] = int(conf["timeout"] or DEFAULT_TIMEOUT)
    conf["max_rows"] = int(conf["max_rows"] or DEFAULT_MAX_ROWS)
    conf["allow_write"] = _truthy(conf.get("allow_write"))
    if "ssl_disabled" not in conf and parsed:
        conf["ssl_disabled"] = parsed.get("ssl_disabled", False)

    if not conf.get("host"):
        raise RuntimeError(
            "未配置数据库地址。请设置 MYSQL_URL 或 MYSQL_HOST，"
            "或提供包含 connections 的配置文件（MYSQL_MCP_CONFIG）。"
        )

    conf["username"] = conf.get("username") or conf.get("user") or ""
    conf["password"] = conf.get("password") or ""
    return conf


def get_connection(conf: dict[str, Any]) -> pymysql.connections.Connection:
    kwargs: dict[str, Any] = {
        "host": conf["host"],
        "port": conf["port"],
        "user": conf["username"],
        "password": conf["password"],
        "charset": conf["charset"],
        "connect_timeout": conf["timeout"],
        "read_timeout": conf["timeout"],
        "cursorclass": DictCursor,
        "autocommit": True,
    }
    if conf.get("database"):
        kwargs["database"] = conf["database"]
    if conf.get("ssl_disabled"):
        kwargs["ssl_disabled"] = True
    try:
        return pymysql.connect(**kwargs)
    except TypeError:
        # 老版本 pymysql 不支持 ssl_disabled
        kwargs.pop("ssl_disabled", None)
        return pymysql.connect(**kwargs)


# --------------------------------------------------------------------------
# SQL 安全判定
# --------------------------------------------------------------------------


class ReadOnlyViolation(Exception):
    """只读模式下命中了写操作语句。"""

    def __init__(self, message: str, statement: str):
        super().__init__(message)
        self.message = message
        self.statement = statement


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"(--|#)[^\n]*", " ", sql)
    return sql.strip()


def statement_kind(sql: str) -> str:
    """返回首条语句的首关键字（小写）。"""
    cleaned = _strip_comments(sql).lstrip("( \t\r\n")
    m = re.match(r"[a-zA-Z]+", cleaned)
    return m.group(0).lower() if m else ""


def is_read_only(sql: str) -> bool:
    return statement_kind(sql) in READ_ONLY_KEYWORDS


def split_statements(sql: str) -> list[str]:
    """按分号拆语句（忽略字符串与注释内的分号）。"""
    statements: list[str] = []
    buf: list[str] = []
    in_str: str | None = None
    i = 0
    length = len(sql)
    while i < length:
        ch = sql[i]
        if in_str:
            buf.append(ch)
            if ch == "\\" and i + 1 < length:
                buf.append(sql[i + 1])
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < length and sql[i + 1] == "-":
            nl = sql.find("\n", i)
            if nl == -1:
                buf.append(sql[i:])
                break
            buf.append(sql[i:nl])
            i = nl
            continue
        if ch == ";":
            text = "".join(buf).strip()
            if text:
                statements.append(text)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _check_read_only(statements: list[str], allow_write: bool) -> None:
    if allow_write:
        return
    for st in statements:
        kind = statement_kind(st)
        if kind in WRITE_KEYWORDS and kind not in READ_ONLY_KEYWORDS:
            raise ReadOnlyViolation(
                f"拒绝执行：检测到写操作 '{kind.upper()}'，当前连接为只读模式。"
                "如确实需要写入，请在连接配置里设置 allow_write=true。",
                st,
            )
        if not is_read_only(st):
            raise ReadOnlyViolation(
                f"拒绝执行：语句类型 '{kind.upper() or 'UNKNOWN'}' 不在只读白名单内。",
                st,
            )


# --------------------------------------------------------------------------
# 查询执行
# --------------------------------------------------------------------------


def _fetch(conf: dict[str, Any], sql: str, params: Any = None) -> list[dict[str, Any]]:
    """执行单条查询并一次性取回结果（供元数据类工具使用）。"""
    conn = None
    try:
        conn = get_connection(conf)
        with conn.cursor() as cur:
            cur.execute(sql, params) if params is not None else cur.execute(sql)
            return list(cur.fetchall())
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _run_statements(conf: dict[str, Any], sql: str, limit: int) -> dict[str, Any]:
    """执行一条或多条 SQL，返回统一结构；异常交给调用方转换。"""
    statements = split_statements(sql)
    if not statements:
        raise ValueError("SQL 为空")

    _check_read_only(statements, bool(conf.get("allow_write")))

    hard_cap = max(1, int(conf["max_rows"]))
    want = max(1, min(int(limit or 100), hard_cap))

    conn = None
    try:
        conn = get_connection(conf)
        results: list[dict[str, Any]] = []
        for st in statements:
            started = time.perf_counter()
            with conn.cursor() as cur:
                cur.execute(st)
                if cur.description:
                    rows = cur.fetchmany(want + 1)
                    truncated = len(rows) > want
                    if truncated:
                        rows = rows[:want]
                    columns = [d[0] for d in cur.description]
                    results.append(
                        {
                            "statement": st if len(st) < 300 else st[:300] + "...",
                            "columns": columns,
                            "rows": rows,
                            "row_count": len(rows),
                            "truncated": truncated,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                        }
                    )
                else:
                    results.append(
                        {
                            "statement": st if len(st) < 300 else st[:300] + "...",
                            "affected_rows": cur.rowcount,
                            "row_count": 0,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                        }
                    )

        payload: dict[str, Any] = {
            "ok": True,
            "connection": conf["name"],
            "database": conf.get("database") or None,
            "statement_count": len(statements),
            "results": results,
        }
        if len(results) == 1:
            payload.update(results[0])
            payload.pop("results", None)
        return payload
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str, indent=None)


def _err(kind: str, message: str, **extra: Any) -> str:
    return _json({"ok": False, "kind": kind, "message": message, **extra})


def _to_error(exc: Exception, conf: dict[str, Any] | None = None) -> str:
    """把异常转换成结构化的错误返回。"""
    name = conf.get("name") if conf else None

    if isinstance(exc, ReadOnlyViolation):
        return _err("read_only", exc.message, statement=exc.statement[:200])
    if isinstance(exc, ValueError):
        kind = "empty" if "SQL 为空" in str(exc) else "invalid"
        return _err(kind, str(exc))
    if isinstance(exc, (KeyError, RuntimeError)):
        return _err("config", str(exc).strip("'\""))
    if isinstance(exc, pymysql.err.ProgrammingError):
        code = exc.args[0] if exc.args else ""
        msg = exc.args[1] if len(exc.args) > 1 else ""
        return _err("sql_error", f"[{code}] {msg}", connection=name)
    if isinstance(exc, pymysql.err.OperationalError):
        code = exc.args[0] if exc.args else 0
        msg = exc.args[1] if len(exc.args) > 1 else str(exc)
        kind = "connect_error" if code in (1045, 2003, 2005, 1049) else "db_error"
        hint = ""
        if code == 1045:
            hint = "（用户名或密码错误）"
        elif code == 2003:
            hint = "（无法连接到主机，检查 host/port 或网络/VPN）"
        elif code == 1049:
            hint = "（数据库不存在）"
        return _err(kind, f"[{code}] {msg}{hint}", connection=name)
    return _err("error", f"{type(exc).__name__}: {exc}", connection=name)


def _require_identifier(value: str, label: str) -> str:
    """校验库名 / 表名，防止拼进 SQL 时被注入。"""
    if not value:
        raise ValueError(f"缺少 {label}")
    if not IDENTIFIER_RE.match(value):
        raise ValueError(f"非法的 {label}: {value}")
    return value


# --------------------------------------------------------------------------
# MCP 工具
# --------------------------------------------------------------------------


server = MCPServer(
    name="mysql",
    version="1.0.0",
    instructions=(
        "通用 MySQL 查询工具。连接由配置决定（项目 .claude/mcp/mysql-mcp/mysql_mcp_config.json、"
        "MYSQL_MCP_CONFIG 环境变量或脚本同目录配置），调用工具时无需传入密码。"
        "用 connection 参数切换不同环境/连接；mysql_list_connections 可查看当前可用连接；"
        "mysql_compare_envs 可把同一条 SQL 同时打到多个环境对比。"
        "默认只读，仅允许 SELECT/SHOW/DESCRIBE/EXPLAIN/WITH/TABLE 开头语句。"
    ),
)


def mysql_query(sql: str, limit: int = 100, connection: str = "") -> str:
    """执行一条或多条 SQL 并返回结果。

    默认只读：只允许 SELECT / SHOW / DESCRIBE / EXPLAIN / WITH 开头的语句；
    写操作需要服务端连接配置 allow_write=true 才会放行。

    Args:
        sql: 要执行的 SQL，可含多条语句（用分号分隔）。
        limit: 单个结果集最多返回的行数，默认 100，硬上限见 max_rows。
        connection: 连接名（见 mysql_list_connections），留空使用配置文件里的 default。
    返回 JSON：ok / columns / rows / row_count / truncated / elapsed_ms。
    """
    conf = None
    try:
        conf = resolve_connection(connection or None)
        return _json(_run_statements(conf, sql, limit))
    except Exception as exc:
        return _to_error(exc, conf)


def mysql_list_databases(connection: str = "") -> str:
    """列出当前账号可见的所有数据库。"""
    return mysql_query("SHOW DATABASES", limit=500, connection=connection)


def mysql_list_tables(database: str = "", pattern: str = "", connection: str = "") -> str:
    """列出某数据库下的表（含行数、引擎、注释）。

    Args:
        database: 库名，留空则使用连接默认库。
        pattern: 可选，表名模糊过滤（不区分大小写）。
        connection: 连接名（见 mysql_list_connections）。
    """
    conf = None
    try:
        conf = resolve_connection(connection or None)
        db = _require_identifier(database or conf.get("database") or "", "database")
        sql = (
            "SELECT TABLE_NAME, TABLE_ROWS, ENGINE, TABLE_COMMENT "
            "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s"
        )
        params: list[Any] = [db]
        if pattern:
            sql += " AND TABLE_NAME LIKE %s"
            params.append(f"%{pattern}%")
        sql += " ORDER BY TABLE_NAME"
        rows = _fetch(conf, sql, params)
        return _json({"ok": True, "connection": conf["name"], "database": db, "count": len(rows), "tables": rows})
    except Exception as exc:
        return _to_error(exc, conf)


def mysql_describe_table(table: str, database: str = "", connection: str = "") -> str:
    """查看表结构（字段名、类型、是否可空、键、默认值、注释）。

    Args:
        table: 表名。
        database: 库名，留空用默认库。
        connection: 连接名（见 mysql_list_connections）。
    """
    conf = None
    try:
        conf = resolve_connection(connection or None)
        db = _require_identifier(database or conf.get("database") or "", "database")
        tbl = _require_identifier(table, "table")
        sql = (
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, "
            "COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
            "ORDER BY ORDINAL_POSITION"
        )
        rows = _fetch(conf, sql, [db, tbl])
        if not rows:
            return _err("not_found", f"表 {db}.{tbl} 不存在或无字段信息")
        return _json({"ok": True, "connection": conf["name"], "database": db, "table": tbl, "columns": rows})
    except Exception as exc:
        return _to_error(exc, conf)


def mysql_show_indexes(table: str, database: str = "", connection: str = "") -> str:
    """查看表的索引信息（索引名、是否唯一、列顺序、索引类型）。

    Args:
        table: 表名。
        database: 库名，留空用默认库。
        connection: 连接名（见 mysql_list_connections）。
    """
    conf = None
    try:
        conf = resolve_connection(connection or None)
        db = _require_identifier(database or conf.get("database") or "", "database")
        tbl = _require_identifier(table, "table")
        sql = (
            "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, INDEX_TYPE, CARDINALITY "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX"
        )
        rows = _fetch(conf, sql, [db, tbl])
        if not rows:
            return _err("not_found", f"表 {db}.{tbl} 没有索引信息或不存在")
        return _json({"ok": True, "connection": conf["name"], "database": db, "table": tbl, "indexes": rows})
    except Exception as exc:
        return _to_error(exc, conf)


def mysql_search_columns(keyword: str, database: str = "", connection: str = "", limit: int = 100) -> str:
    """按字段名模糊搜索，快速定位相关表（排查字段归属时很有用）。

    Args:
        keyword: 字段名关键字，不区分大小写。
        database: 库名，留空用默认库。
        connection: 连接名（见 mysql_list_connections）。
        limit: 最多返回多少行，默认 100。
    """
    conf = None
    try:
        if not keyword or not keyword.strip():
            raise ValueError("缺少 keyword")
        conf = resolve_connection(connection or None)
        db = _require_identifier(database or conf.get("database") or "", "database")
        cap = max(1, min(int(limit or 100), int(conf["max_rows"])))
        sql = (
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND COLUMN_NAME LIKE %s "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION LIMIT %s"
        )
        rows = _fetch(conf, sql, [db, f"%{keyword.strip()}%", cap])
        return _json(
            {"ok": True, "connection": conf["name"], "database": db, "keyword": keyword,
             "count": len(rows), "columns": rows}
        )
    except Exception as exc:
        return _to_error(exc, conf)


def mysql_list_connections() -> str:
    """列出配置文件中登记的所有连接（密码打码，不建立连接）。"""
    cfg = load_config_file()
    if cfg.get("__error__"):
        return _err("config", cfg["__error__"])
    connections: dict[str, Any] = cfg.get("connections") or {}
    items = []
    for name, entry in sorted(connections.items()):
        entry = entry or {}
        pwd = str(entry.get("password") or "")
        items.append(
            {
                "name": name,
                "url": entry.get("url"),
                "username": entry.get("username") or entry.get("user"),
                "password": (pwd[:2] + "*" * max(len(pwd) - 2, 0)) if pwd else "",
                "database": entry.get("database"),
                "allow_write": _truthy(entry.get("allow_write", False)),
                "max_rows": entry.get("max_rows", DEFAULT_MAX_ROWS),
            }
        )
    return _json(
        {
            "ok": True,
            "config_file": str(_config_path()) if _config_path() else None,
            "default": cfg.get("default"),
            "count": len(items),
            "connections": items,
        }
    )


def mysql_connection_info(connection: str = "") -> str:
    """查看当前生效的连接配置（密码打码），用于确认 MCP 接的是哪个库。"""
    conf = None
    try:
        conf = resolve_connection(connection or None)
        pwd = conf.get("password") or ""
        masked = (pwd[:2] + "*" * max(len(pwd) - 2, 0)) if pwd else ""
        return _json(
            {
                "ok": True,
                "name": conf["name"],
                "host": conf["host"],
                "port": conf["port"],
                "database": conf.get("database") or None,
                "username": conf["username"],
                "password": masked,
                "charset": conf["charset"],
                "allow_write": bool(conf["allow_write"]),
                "max_rows": conf["max_rows"],
                "config_file": str(_config_path()) if _config_path() else None,
            }
        )
    except Exception as exc:
        return _to_error(exc, conf)


def mysql_ping(connection: str = "") -> str:
    """测试数据库连通性，返回版本与当前库。"""
    conf = None
    try:
        conf = resolve_connection(connection or None)
        row = _fetch(conf, "SELECT VERSION() AS version, DATABASE() AS current_db")
        return _json({"ok": True, "connection": conf["name"], "reachable": True, **(row[0] if row else {})})
    except Exception as exc:
        return _to_error(exc, conf)


def mysql_compare_envs(sql: str, connections: str = "", limit: int = 50) -> str:
    """把同一条只读 SQL 同时打到多个环境执行，返回各环境结果便于对比。

    无论 allow_write 如何配置，本工具始终只允许只读语句。

    Args:
        sql: 只读 SQL（建议单条，便于对比）。
        connections: 用逗号分隔的连接名（如 "sit2,sit6"）；留空则对所有已配置连接执行。
        limit: 每个环境最多返回行数，默认 50。
    """
    try:
        cfg = load_config_file()
        all_names = list((cfg.get("connections") or {}).keys())
        names = [n.strip() for n in connections.split(",") if n.strip()] or all_names
        if not names:
            raise ValueError("没有可用的连接，请先配置 mysql_mcp_config.json")

        envs: dict[str, Any] = {}
        for name in names:
            conf = None
            try:
                conf = resolve_connection(name)
                statements = split_statements(sql)
                if not statements:
                    raise ValueError("SQL 为空")
                _check_read_only(statements, False)  # 对比工具强制只读
                envs[name] = _run_statements(conf, sql, limit)
            except Exception as exc:
                envs[name] = json.loads(_to_error(exc, conf))

        ok = any(v.get("ok") for v in envs.values())
        return _json({"ok": ok, "sql": sql, "envs": envs})
    except Exception as exc:
        return _to_error(exc)


for _fn in (
    mysql_query,
    mysql_list_databases,
    mysql_list_tables,
    mysql_describe_table,
    mysql_show_indexes,
    mysql_search_columns,
    mysql_list_connections,
    mysql_connection_info,
    mysql_ping,
    mysql_compare_envs,
):
    server.tool(name=_fn.__name__, description=_fn.__doc__)(_fn)


if __name__ == "__main__":
    # 启动前做一次配置校验，问题直接打到 stderr（不污染 stdio 协议）
    try:
        _conf = resolve_connection()
        print(
            f"[mysql-mcp] 使用连接 '{_conf['name']}' -> "
            f"{_conf['username']}@{_conf['host']}:{_conf['port']}/{_conf.get('database') or '-'} "
            f"(allow_write={_conf['allow_write']})，配置来源: {_config_path() or '环境变量'}",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"[mysql-mcp] 启动告警：{exc}", file=sys.stderr)
    server.run(transport="stdio")
