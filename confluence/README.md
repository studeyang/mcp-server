# Confluence

## 介绍

基于第三方 [`mcp-atlassian`](https://github.com/sooperset/mcp-atlassian) 接入公司内网 Confluence Server，**本仓库不包含自建源码**，用 `uvx` 以隔离环境直接运行，升级只需改版本或加 `@latest`。

| 项目 | 值                                                        |
|---|----------------------------------------------------------|
| 服务地址 | `https://confluence.domain.com`（Confluence 7.18.3 Server） |
| 鉴权方式 | Personal Access Token（PAT，`Bearer`）                      |
| 空间过滤 | `J,005964`                                               |
| 运行方式 | `uvx --python 3.12 --from mcp-atlassian mcp-atlassian`   |

导出的工具（核心 5 个）：

| 工具 | 说明 |
|---|---|
| `confluence_search` | 按 CQL / 关键字搜索页面 |
| `confluence_get_page` | 读取页面正文（含可选评论） |
| `confluence_create_page` | 新建页面 |
| `confluence_update_page` | 更新页面（需带当前版本号） |
| `confluence_add_comment` | 给页面追加评论 |

> `mcp-atlassian` 实际还会导出附件、标签、页面历史等扩展工具，以上为核心高频项。

## 快速开始

**前置条件**：已安装 `uv`（Windows 下 `uvx` 通常在 `%USERPROFILE%\.local\bin\uvx.exe`）。

**配置**：把下面的 `mcpServers` 段拷贝到 `.mcp.json` 或 `~/.claude.json`，将 `<PAT>` 换成自己的 token（也可按「注意事项」改用 `env` 传入）：

```json
{
  "mcpServers": {
    "confluence": {
      "type": "stdio",
      "command": "C:/Users/005964/.local/bin/uvx.exe",
      "args": [
        "--python", "3.12",
        "--from", "mcp-atlassian",
        "mcp-atlassian",
        "--confluence-url", "https://confluence.domain.com",
        "--confluence-personal-token", "<PAT>",
        "--confluence-spaces-filter", "J,005964"
      ]
    }
  }
}
```

Linux / macOS 下把 `command` 换成 `uvx` 即可。

## 注意事项

- PAT 是明文凭证：只放在用户级配置（`~/.claude.json`）或项目里被 gitignore 的本地配置文件中，不要提交到仓库。
- `mcp-atlassian` 已把 `--confluence-personal-token` 标为 deprecated（命令行参数对本机其他用户可见），更稳妥的写法是放进 `env`：`"env": {"CONFLUENCE_PERSONAL_TOKEN": "<PAT>"}`，args 中不再传 token。
- token 有效期有限，过期后工具会返回鉴权失败，需重新生成并替换。
- `--confluence-spaces-filter` 决定可访问的空间范围，逗号分隔；不配置则按账号权限全量可见。
