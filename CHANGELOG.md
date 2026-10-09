# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-10-09

### Added

- **`devin_session_token` 支持多个凭据**：配置改为列表，`/devin login` 追加而非覆盖，使用时按顺序取第一个未过期的，过期的自动跳过；`/devin status` 显示可用数量与最晚过期时间。
- **凭据前端密码掩盖**：配置项标记 `secret`，面板中以密码框显示并支持显隐切换。
- **重试次数配置 `max_retries`**（默认 3）：等待时间优先遵守服务端 `Retry-After` 头（上限 60 秒），否则指数退避 1/2/4 秒（上限 8 秒）。
- 插件图标 `logo.png`（256x256，取自 Devin 官网品牌资源）。

### Changed

- **重试改为单节点内耗尽后再切换下一节点**（此前一次失败即切换）。
- `retryable_status_codes` 描述改为「重试状态码」，默认仍为 429/500/502/503/504。
- **移除 `enable_llm_tool`**：LLM 工具始终注册，启停交给 AstrBot 本体（面板工具开关）。
- 凭据说明只描述 devin token（需带 `devin-session-token$` 前缀，不自动补齐）。

### Fixed

- 修复登录换取 token 时的响应解包失败：`default_post_json` 改为三元组后漏改 `exchange_code`，导致登录一律报「无法连接 api.devin.ai」。

## [0.1.0] - 2026-10-09

首个版本。

### Added

- **OAuth 登录**：`/devin login`（管理员）发送授权链接，浏览器完成 Devin 登录后把一次性 code 发回会话即完成登录，token 自动写入插件配置。等待期间支持 `cancel` 取消、`status` 查看状态、`--restart` 重新生成链接，默认 300 秒超时。
- **LLM 工具 `devin-web-search`**：供模型自动调用的联网搜索。
- **手动命令**：`/devin search` 执行搜索、`/devin status` 查看状态、`/devin logout` 清除 token、`/devin cancel` 取消在途登录。
- **双服务节点容灾**：默认 `server.codeium.com` 与 `server.self-serve.windsurf.com`，单节点网络错误、可重试状态码、HTTP 200 空结果均切换下一节点；全部节点 401/403 判定会话被吊销。
- 配置项：`devin_session_token`、`search_hosts`、`max_results`、`timeout_seconds`、`retryable_status_codes`、`proxy`、`show_sources`、`enable_llm_tool`。
