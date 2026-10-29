# 安全说明

## 支持范围

安全修复优先进入最新发布版本。旧版本可能只提供升级建议。

## 报告方式

请使用 GitHub 仓库的 Private vulnerability reporting 功能报告漏洞。不要在公开 Issue、讨论或日志中粘贴：

- API Key、访问令牌或真实模型地址。
- 本机用户名、绝对路径和私人项目 ID。
- 原片、角色素材、审核记录或可信知识内容。

报告应包含受影响版本、复现步骤、预期影响和可行的缓解方案。

## 本地服务边界

- Workbench 与 AI 网关默认只监听 `127.0.0.1`。Workbench 的 `--host` 若不是回环地址会直接拒绝启动，必须显式加 `--allow-remote` 才会继续（并打印警告）。

### Workbench（默认 8765）的请求模型

- **Host 校验**：只接受 `Host: 127.0.0.1:<端口>`、`localhost:<端口>`、`[::1]:<端口>`，其他一律 403，用于阻断 DNS rebinding。
- **每进程令牌**：启动时用 `secrets.token_urlsafe` 生成随机令牌，只存在内存中、每次重启都会变化。令牌注入到 `/` 返回的片库页面，也可由同源页面通过 `GET /api/session` 读取（无 CORS 头，跨站页面读不到）。
- **写操作**（`POST`/`PUT`/`PATCH`/`DELETE`）必须携带 `X-Workbench-Token`，服务端用 `hmac.compare_digest` 比较；缺失或错误返回 403。
- **Origin 校验**：写请求若带 `Origin`，必须是本服务自身来源（`http://127.0.0.1:<端口>` 等），`null` 与其他站点返回 403。
- **内容类型**：写请求拒绝 `text/plain`、`application/x-www-form-urlencoded`、`multipart/form-data`（HTML 表单可跨站发送的类型），返回 415；JSON 接口要求 `application/json`。视频上传使用原始请求体（`video/*` 或 `application/octet-stream`）。
- **磁盘空间**：上传前检查保存位置剩余空间，不足“文件大小 + 1 GB 处理余量”时返回 507。
- 使用脚本调用写接口时，先 `GET /api/session` 取令牌再放进 `X-Workbench-Token` 头。

### AI 网关（默认 8766）

- 只接受本地片库的精确来源（默认 `http://127.0.0.1:8765`、`http://localhost:8765`、`http://[::1]:8765`，可用 `--workbench-port` / `--allowed-origin` 调整），同样校验 `Host`，`POST` 必须是 `application/json`。
- 默认拒绝 `Origin: null`；仅在 `--allow-file-origin` 下放行，用于 `file://` 示例页。
- 网关暂无每进程令牌（审片页与网关跨端口，无法在不把令牌写进生成 HTML 的情况下安全下发），依赖来源白名单 + Host + 内容类型防护。
- API Key 请用 `AI_ADVISOR_API_KEY` 环境变量提供；`--api-key` 已弃用，命令行参数会暴露在进程列表中。

### 其他

- `AI_ADVISOR_ASSET_ROOT` 应指向权限最小的项目目录，不要直接指向整个主目录。
- 浏览器页面中不得保存服务端 API Key。
- 修改监听地址或通过反向代理暴露服务时，使用者必须自行增加身份验证、TLS 和访问控制。
