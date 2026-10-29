# AI 影像导师

AI 影像导师是当前镜头上下文感知的专业问答层。它面向“不知道术语，但能描述视觉现象”的使用者，不把普通聊天框当成专业功能。

同一个本地网关还提供结构化“AI 初拉”。导师回答问题；AI 初拉返回逐字段内容、可信度和证据。两者不能互相替代。

## 它解决什么问题

用户可以直接提问：

- “人物越来越大，但镜头好像没动，这叫什么？”
- “头发边缘有一圈冷光，是逆光还是轮廓光？”
- “画面整体冷紫、脸上偏暖，这个色调怎么写？”
- “这种黑位、高光和饱和度应该怎么调？”
- “bloom、辉光和光晕有什么区别？”

导师会自动附带当前镜头、前后镜、诊断项、全部拉片字段和媒体路径，并按“专业判断、判断依据、易混概念、建议写法、还需确认”组织回答。

## 为什么需要本地网关

生成的审片页仍然可以通过 `file://` 直接打开，但浏览器页面不应保存模型 API Key。`src/ai_advisor_server.py` 负责：

1. 从环境变量读取模型地址和密钥；
2. 接收当前镜头上下文；
3. 仅在配置的素材根目录内读取首中尾帧和接触表；
4. 调用模型并以 NDJSON 流式返回；
5. 只为本地片库页面（`http://127.0.0.1:<片库端口>`、`http://localhost:<片库端口>`）提供 CORS 响应，拒绝其他网站借本地端口调用；`file://` 页面需显式开启 `--allow-file-origin`；
6. 通过 `/api/analyze-shot` 返回可进入人机差异审核的结构化初判。

默认只监听 `127.0.0.1`，不会主动把端口暴露到局域网。

## 零配置体验

先生成示例页，再启动内置模拟导师：

```bash
make demo
make advisor-demo
```

`make advisor-demo` 会带上 `--allow-file-origin`，以便直接用 `file://` 打开的示例页调用模拟导师。该开关同时会放行任何网站沙箱 iframe 发出的 `Origin: null`，只建议在示例/模拟模式下使用；处理真实项目时请通过本地片库（`make workbench`）打开审片页。

打开 `examples/demo/review_demo.html`，先在“舒适度 → AI 影像导师”中选择“启用”，再点击镜头栏的“问 AI”或按 `A`。

AI 是功能开关，默认关闭：

- 关闭时不显示“问 AI”和“AI 导师”标签；
- 不执行健康检查，也不发起任何 AI 网络请求；
- 不启动网关也不影响视频、接触表、审核、导出和其他快捷键；
- 每个浏览器会独立保存是否启用。

模拟模式用于验证交互、流式显示和专业回答结构，不会调用外部模型。
模拟初拉不会执行视觉推理，所有字段均返回低可信度，并明确要求人工回看视频。

## OpenAI-compatible 服务

适用于提供 `/chat/completions` 流式接口的服务。模型名称由你使用的平台决定：

```bash
export AI_ADVISOR_PROVIDER=openai-compatible
export AI_ADVISOR_BASE_URL=https://your-provider.example/v1
export AI_ADVISOR_API_KEY=your-server-side-key
export AI_ADVISOR_MODEL=your-model-id
export AI_ADVISOR_ASSET_ROOT="$PWD/examples/demo"

python3 src/ai_advisor_server.py
```

API Key 只存在于启动服务的终端环境，不进入 HTML、YAML 或浏览器本地存储。
仓库已忽略 `.env` 和 `.env.*`；可以复制 `.env.example` 作为本机配置起点，并在启动前执行 `set -a; source .env; set +a`，但不要提交真实密钥。

## Ollama

```bash
export AI_ADVISOR_PROVIDER=ollama
export AI_ADVISOR_MODEL=your-installed-model
export AI_ADVISOR_ASSET_ROOT="$PWD/examples/demo"

python3 src/ai_advisor_server.py
```

如果模型支持视觉输入，服务会附带最多四张图片：首帧、中间帧、尾帧和逐帧接触表。静帧不能完全证明连续运镜，因此导师仍会结合用户描述和结构化字段追问。

## 外部工程素材路径

个人项目建议保存在仓库之外，并把素材根目录显式传给本地网关：

```bash
python3 src/ai_advisor_server.py \
  --provider mock \
  --asset-root "/absolute/path/to/lapian-projects"
```

换成真实模型时只需替换 provider、base URL、model 和服务端环境变量。

## 安全与审核边界

- AI 回答按镜头保存在浏览器本地，不自动修改原始拉片字段。
- AI 默认关闭，并与审片页面、素材生成器和审核状态解耦。
- “追加到人工备注”会明确标记为“待人工核对”。
- 图片路径必须位于 `AI_ADVISOR_ASSET_ROOT` 内，目录穿越会被拒绝。未显式配置该变量或
  `--asset-root` 时，网关会读取片库的用户保存位置设置，并随网页切换实时跟随；这使得
  “保存位置”不需要再额外手动同步 AI 素材目录。
- 浏览器来源只允许本地片库的精确来源：`http://127.0.0.1:<端口>`、`http://localhost:<端口>`、`http://[::1]:<端口>`，端口默认取 `LAPIAN_WORKBENCH_PORT` 或 8765，可用 `--workbench-port`（可重复）或 `--allowed-origin` 追加；其他来源（包括 `Origin: null`）返回 403，且不会回显到 CORS 头。
- `Origin: null` 仅在显式传入 `--allow-file-origin`（或 `AI_ADVISOR_ALLOW_FILE_ORIGIN=1`）时放行，并在启动时打印警告。
- `Host` 头必须是本机回环地址加网关端口，防止 DNS rebinding；`POST` 必须使用 `Content-Type: application/json`。
- API Key 只从 `AI_ADVISOR_API_KEY` 环境变量读取；`--api-key` 已弃用（会出现在 `ps` 等进程列表中），传入时会打印警告。
- 当前 AI 网关没有每进程令牌：审片页与网关跨端口（8765 → 8766），无法在不把令牌写入生成的 HTML 的前提下安全下发，因此依靠精确来源白名单 + Host 校验 + JSON 内容类型防护。
- 单张图片上限 6 MB，最多附带四张。
- AI 不应虚构焦段、光比、色温、LUT 或节点参数。
- 静帧只能提供构图、光色和空间证据；连续运镜仍应回看视频。

## 接口协议

### 影像导师

前端请求：

```json
{
  "shot_id": "SH018",
  "mode": "camera",
  "question": "人物像被吸到镜头前，这叫什么？",
  "conversation": [],
  "context": {
    "evidence": {},
    "diagnostics": [],
    "assets": {}
  }
}
```

服务以 NDJSON 返回：

```json
{"type":"meta","provider":"mock","model":"film-advisor-mock","image_count":4}
{"type":"delta","text":"### 专业判断\n"}
{"type":"delta","text":"需要区分推镜、变焦和主体位移。"}
{"type":"done","answer":"完整回答"}
```

### AI 初拉

请求：

```json
{
  "shot_id": "SH018",
  "fields": ["camera_motion", "timeline_beats"],
  "context": {
    "evidence": {},
    "diagnostics": [],
    "assets": {}
  },
  "trusted_memory": []
}
```

响应：

```json
{
  "shot_id": "SH018",
  "provider": "openai-compatible",
  "model": "your-model-id",
  "fields": {
    "camera_motion": {
      "value": "机位固定，主体自行靠近",
      "confidence": "medium",
      "evidence": "背景透视基本稳定；仍需回看连续视频"
    }
  },
  "uncertainties": ["需区分光学变焦和后期裁切"],
  "memory_used": ["K-SH002-camera"]
}
```

服务器只接受白名单字段。返回镜头编号必须与请求一致；页面会保存原始 AI 初判，人工采用或纠正后才写入最终字段。
