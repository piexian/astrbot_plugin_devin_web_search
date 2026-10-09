# Devin 联网搜索 (astrbot_plugin_devin_web_search)

使用免费的 Devin 账号，让 AstrBot 获得 Devin 的 web search 能力。

通过浏览器 OAuth 登录复用 Devin（Windsurf / Exa 后端）账号会话执行搜索，token 自动写入插件配置，提供 LLM 函数工具与手动命令。非官方插件。

## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.10 | |
| AstrBot | >= v4.9.2 | 基础功能（指令 + LLM Tool） |

**平台支持**: 全平台（无限制）

## 功能

- `/devin login`（管理员）- 发送授权链接，浏览器登录后把一次性 code 发回会话即完成登录，token 自动写入插件配置
- `/devin status` - 查看登录状态与过期时间；`/devin logout`（管理员）清除 token；`/devin cancel` 取消在途登录
- `/devin search <query>` - 手动执行搜索
- LLM Tool (`devin-web-search`) - 供 LLM 自动调用的联网搜索工具，启停由 AstrBot 本体控制
- 双服务节点容灾（server.codeium.com / server.self-serve.windsurf.com）：单节点网络错误、可配置状态码、200 空结果均自动切换下一节点；全部节点 401/403 判定会话被吊销

## 安装

### 俩种方式

1. 在 AstrBot 插件市场搜索 `Devin联网搜索` 点击安装
2. 在插件界面右下角点击加号选择从链接安装输入 ` https://github.com/piexian/astrbot_plugin_devin_web_search  `

## 配置

### 凭据设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `devin_session_token` | list | 是 | 会话凭据（支持多个），`/devin login` 自动追加，或手动添加（devin token，需带 `devin-session-token$` 前缀，不自动补齐） |

> 手填时不校验格式，前缀填错会直接返回 401。多个凭据按顺序使用，过期的自动跳过。
> 也可以去[Devin](https://app.devin.ai/org/piexian/settings/devin-api?tab=pats)控制台获取cli密钥填入
### 连接设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `search_hosts` | list | 否 | 搜索服务地址列表（默认: 双官方节点），前一个失败自动切换后一个 |
| `timeout_seconds` | int | 否 | 请求超时（默认: 20 秒） |
| `proxy` | string | 否 | HTTP 代理地址（例如: http://127.0.0.1:7890） |

### 重试设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `retryable_status_codes` | list | 否 | 重试状态码（默认: [429, 500, 502, 503, 504]） |
| `max_retries` | int | 否 | 单节点重试次数（默认: 3） |

> 等待时间优先遵守服务端 `Retry-After` 头，否则指数退避 1/2/4 秒（上限 8 秒）。401/403 始终按会话失效处理；HTTP 200 空结果同样切换下一节点，全部节点都空才返回空结果。

### 输出设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `max_results` | int | 否 | 返回条数，范围 1-10（默认: 5） |
| `show_sources` | bool | 否 | 结果中显示来源 URL（默认: true） |


## 使用

### 指令

```
/devin login            # 发起登录，浏览器授权后把 code 发回会话
/devin status           # 查看登录状态与过期时间
/devin search 最新的 AI 新闻
/devin logout           # 清除 token（管理员）
/devin cancel           # 取消在途登录（管理员）
```

登录流程：

1. 发送 `/devin login`，插件回复授权链接（仅管理员可发起）
2. 浏览器打开链接完成 Devin 登录，复制页面上的一次性 code
3. 把 code 直接发回会话即完成登录，token 写入插件配置

> 等待期间支持在会话内发送 `cancel` 取消，默认 300 秒超时自动清理。

### LLM Tool

当 LLM 需要搜索实时信息时，会自动调用 `devin-web-search` 工具。未登录或会话失效时返回引导文案，提示重新执行 `/devin login`。

## 输出示例

```
Python 3.12 的主要新特性包括:

1. 更好的错误消息 - 改进了语法错误提示
2. 类型参数语法 - 支持泛型类型参数
3. 性能提升 - 解释器启动更快

来源:
  1. Python 3.12 Release Notes
     https://docs.python.org/3/whatsnew/3.12.html
  2. ...

(耗时: 2345ms)
```

## 项目结构

```
astrbot_plugin_devin_web_search/
├── main.py              # 插件主入口（命令注册、登录会话控制）
├── tools/
│   ├── devin_oauth.py   # PKCE 生成、授权链接与 code 兑换
│   ├── devin_session.py # 凭据状态与配置读写
│   ├── devin_search.py  # 搜索请求、双节点容灾与结果解析
│   └── devin_tools.py   # LLM 函数工具定义
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置项 Schema
└── README.md
```

## 致谢

登录与搜索协议参考 [mimimaster/dsh-devin-search](https://github.com/mimimaster/dsh-devin-search)（MIT License）。

## 支持

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [Issues](https://github.com/piexian/astrbot_plugin_devin_web_search/issues)

## 更新日志

查看 [CHANGELOG.md](https://github.com/piexian/astrbot_plugin_devin_web_search/blob/master/CHANGELOG.md) 了解版本更新历史。
