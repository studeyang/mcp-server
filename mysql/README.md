# MySQL MCP Server（通用版）

## 介绍

把任意 MySQL 连接暴露成标准 MCP（stdio）工具。脚本本身**不绑定任何项目/库**，连接信息完全由配置决定，可被任意项目复用。

**连接配置解析优先级**：

1. `MYSQL_MCP_CONFIG` 环境变量指定的 JSON 文件
2. 从 `CLAUDE_PROJECT_DIR`（Claude Code 注入的项目根）向上查找的 `.claude/mcp/mysql-mcp/mysql_mcp_config.json`
3. 脚本同目录的 `mysql_mcp_config.json`（全局兜底）
4. 都没有时，回退到环境变量直连（`MYSQL_URL` / `MYSQL_HOST` 等）

**工具列表**：

| 工具 | 说明 |
|---|---|
| `mysql_query` | 执行 SQL。参数 `sql`、`limit`(默认100)、`connection` |
| `mysql_list_databases` | 列出当前账号可见的库 |
| `mysql_list_tables` | 列某库的表（含行数/引擎/注释），参数 `database`、`pattern` |
| `mysql_describe_table` | 查表结构，参数 `table`、`database` |
| `mysql_show_indexes` | 查表索引，参数 `table`、`database` |
| `mysql_search_columns` | 按字段名模糊搜索定位相关表，参数 `keyword`、`database` |
| `mysql_list_connections` | 列出所有已配置连接（密码打码） |
| `mysql_connection_info` | 查看生效连接（密码打码） |
| `mysql_ping` | 连通性测试，返回版本与当前库 |
| `mysql_compare_envs` | 同一条只读 SQL 同时打到多个环境对比，参数 `sql`、`connections`(逗号分隔，留空=全部)、`limit` |

**只读保护**：默认拒绝一切非只读语句，白名单为 `SELECT` / `SHOW` / `DESCRIBE` / `DESC` / `EXPLAIN` / `WITH` / `TABLE`（判首关键字前会先剥离注释）；需要写入须在连接配置里显式设 `allow_write: true`，`mysql_compare_envs` 始终强制只读。

## 快速开始

**环境要求**：Python 3.10+（`mcp>=2` 的最低要求）。

**安装依赖**：

```bash
python -m pip install "mcp>=2" pymysql
```

**接入方式**：每个项目只需在自己的 `.claude/mcp/mysql-mcp/` 放一份 `mysql_mcp_config.json`（格式见 `mysql_mcp_config.example.json`，连接名随意，如 `sit2` / `sit6` / `prd`）。

若未做用户级注册，可在项目级 `.mcp.json` 中配置：

```json
{
  "mcpServers": {
    "mysql": {
      "type": "stdio",
      "command": "python",
      "args": ["D:/github/mcp-server/mysql/mysql_mcp_server.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

> 连接密码明文存在 `mysql_mcp_config.json`，建议各项目把该文件加入 `.gitignore`。
