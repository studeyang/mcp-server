# Archery

## 介绍

Archery 提供 **HTTP JSON 接口，没有真正的 MCP(JSON-RPC 2.0) 协议接口**。

| 接口 | 方法 | 用途 |
|---|---|---|
| `/mcp/query/` | POST | 执行只读 SQL |
| `/api/v1/instance/` | GET | 实例列表（Django 分页，`next` 是完整 URL） |
| `/api/v1/instance/resource/` | POST | 库 / 表 / 字段元数据 |

认证统一使用 `Authorization: Bearer ark_xxx`。需配置两个环境变量：`ARCHERY_ROOT`（服务根地址，必填、无默认值）和 `ARCHERY_TOKEN`。

使用注意事项：

- 实例名必须带端口，如 `es-friendsdata-s1n:3309`；主从库是不同实例，不能互相代用。
- `SELECT *` 会被安全策略拦截，需要先拉字段列表再展开成显式列名（展开后的列按字母序，不是表物理顺序）。
- 实例需打 `can_mcp` 标签才能走该通道。
- 只支持 select / show 等只读语句，`desc` 也会被拦截，请用 `show columns` 或元数据接口。
- 不要使用 CTE（`with ...`），权限校验会失败，改写子查询或分两步查。
- 限流是突发式的，超限返回 **HTTP 401** 和 `Token 调用频率超限`，容易被误判为 token 失效，遇到时退避 3–5 秒重试。

## 快速开始

**CLI 调用**（纯标准库，无需 pandas / requests）：

```bash
export ARCHERY_ROOT=http://archery.domain.com
export ARCHERY_TOKEN=ark_xxx
python archery_client.py --instance "es-friendsdata-s1n:3309" --db es_friendsdata \
    --sql "select * from fd_customer_info;" --expand-star --limit 100
```

**作为 MCP 服务器接进 IDE**：

```bash
python -m pip install "mcp>=1.2"
```

配置：

```json
{
  "mcpServers": {
    "archery-sqlquery": {
      "command": "python",
      "args": [
        "D:/github/mcp-server/archery/archery_mcp_server.py"
      ],
      "env": {
        "ARCHERY_TOKEN": "ark_xxx",
        "ARCHERY_ROOT": "http://archery.domain.com"
      }
    }
  }
}
```

导出 5 个工具：`archery_list_instances` / `archery_list_databases` / `archery_list_tables` / `archery_list_columns` / `archery_query`。
