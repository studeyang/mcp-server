# mcp-server

个人自建的 MCP（Model Context Protocol）服务集合，统一在这里维护。

## 服务一览

```
mcp-server/
├── mysql/                  MySQL 通用查询（只读）
│   ├── mysql_mcp_server.py
│   ├── mysql_mcp_config.example.json   配置模板
│   └── README.md
├── archery/                Archery 只读 SQL 查询封装
│   ├── archery_mcp_server.py
│   ├── archery_client.py
│   └── README.md
└── confluence/             Confluence 文档检索（第三方 mcp-atlassian）
    └── README.md
```

服务说明：

| 服务 | 说明 | 环境要求 | 依赖 |
|---|---|---|---|
| [`mysql`](mysql/README.md) | 直连 MySQL 执行查询，连接参数可配置，默认只读。10 个工具，支持多环境对比、索引/字段检索 | Python 3.10+ | `pymysql`, `mcp>=2` |
| [`archery`](archery/README.md) | 封装 Archery 的 HTTP 查询接口为标准 MCP | Python 3.10+ | 纯标准库 + `mcp>=2` |
| [`confluence`](confluence/README.md) | 通过第三方 `mcp-atlassian` 接入 Confluence Server（PAT 鉴权），无本地源码 | `uv`/`uvx`（自带 Python 3.12） | `mcp-atlassian`（uvx 自动拉取） |

## 安装

```bash
pip install -r requirements.txt
```

