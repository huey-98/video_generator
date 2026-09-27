# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

AI 视频生成器：基于阿里百炼平台·通义万相视频模型的 Gradio Web 工具（Python 3.12）。
调用百炼异步任务 API 生成视频，支持 4 个模型切换、参考图/参考视频素材、历史记录与任务续查。

## 常用命令

```bash
# 安装依赖（Windows 虚拟环境）
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 启动（自动打开浏览器 http://127.0.0.1:7860）
.venv/Scripts/python.exe main.py
.venv/Scripts/python.exe main.py --no-browser   # 不自动打开浏览器
.venv/Scripts/python.exe main.py --port 7861    # 换端口
```

- 运行需要 `.env` 中的 `DASHSCOPE_API_KEY`；业务空间专属域名用 `DASHSCOPE_BASE_URL` 覆盖
- 没有测试套件和 lint 配置；验证方式 = 启动 Web UI 实际跑任务。**提交即扣费**，
  调试时用最短时长 + 最低分辨率

## 核心设计：配置驱动的模型注册表

**平台 API 变更时改 `config.yaml`，不改代码**——这是本项目最重要的架构原则。

`config.yaml` 的 `models:` 段是模型注册表，每个模型声明自己的能力：时长范围、
分辨率参数名（`size` 或 `resolution`，**模型之间不同**）、分辨率选项、参考素材规格
（`refs` 段）、支持的额外参数（`extra_params`）、单价（`price_per_second`，填了界面才显示费用预估）。

数据流（分层，各层可独立理解）：

1. `src/config.py` — 加载 `.env` + `config.yaml` → `AppConfig`（含 `list[ModelSpec]`）
2. `src/registry.py` — 把 YAML 解析为 `ModelSpec` / `RefSpec` / `RefKind` 数据类
3. `src/payload.py` — 按 `ModelSpec` 能力组装请求体（与 UI 解耦，可单独验证）
4. `src/client.py` — 原生 HTTP 异步任务客户端（刻意不用 dashscope SDK，避免 SDK 版本对新模型的兼容问题）
5. `ui/webui.py` — Gradio 界面，`model_controls()` 按 `ModelSpec` 动态显隐/适配控件

## 百炼 API 调用模式（src/client.py）

异步任务三步：

1. `POST /services/aigc/video-generation/video-synthesis`，**必须带请求头
   `X-DashScope-Async: enable`** → 得到 task_id
2. `GET /tasks/{task_id}` 轮询至 `SUCCEEDED`（间隔/超时在 config.yaml 的 `api:` 段）
3. 下载 `output.video_url`（URL 仅 24 小时有效，成功后须立即下载到 `outputs/`）

请求体结构：`{"model": ..., "input": {...}, "parameters": {...}}`。
参考素材放 `input.media` 数组：

```json
{"type": "reference_image | reference_video | first_frame", "url": "https://... 或 data:...;base64,..."}
```

## 本地文件 → 临时URL（本地文件怎么变成平台能用的地址）

`config.yaml` 里每类参考素材有个 `upload_mode`：

- `oss`（**当前所有模型统一用此**）：本地文件先上传到百炼**免费**临时空间，换 `oss://` 临时URL
- `base64`（备用路径，代码保留）：本地文件直接内联 `data:...;base64,...`，
  平台规则变化时可在 config.yaml 按模型切回

上传流程见 `client.upload_file()`（2026-09-26 实测跑通）：取凭证 → OSS PostObject → `oss://`。
界面上的「📤 上传换取临时URL」按钮走 `make_uploader()`；生成时也会自动补传未上传的本地文件。

**三个必须记住的约束**：

- 上传时指定的模型**必须与后续调用模型一致**（平台文件与模型绑定），所以 `upload_file` 要传 `m.id`
- 临时 URL **48 小时**失效，且与主账号绑定
- 请求体含 `oss://` 时，**必须加请求头 `X-DashScope-OssResourceResolve: enable`**，
  否则平台不解析该地址。`client.has_oss_url()` 自动判断，`create_task()` 自动加

## 参考素材实测规则（2026-09-24 探测平台校验报错得出）

改动 refs 相关代码前必读，完整规则表见 README.md：

- `type` 合法取值只有 `reference_image` / `reference_video` / `first_frame` 三个；
  `first_frame` 不能单独出现，UI 未开放该类型
- `wan2.7-r2v`：media 数组**图+视频合计 ≤5**，至少 1 个素材；官方文档确认 media.url
  同时支持 公网URL/oss临时URL/base64 三种形式（2026-09-27 查证），本工具统一走 oss 上传
- `wan3.0-video` / `-prime`：图 ≤10、视频 ≤5 **分别计数**，**不收 base64**——本地文件
  必须先上传换成 `oss://` 临时URL（见下节）
- 素材体积上限（平台）：参考图 ≤20MB、参考视频 ≤100MB；上传接口 ≤1GB
- 地址顺序 = **本地文件在前、URL 在后**，`collect_refs()` 与 `ref_manifest()` 必须保持一致
  （2026-09-26 修复过一个两者顺序相反的 bug，改动这块务必同步核对）
- 提示词用「图1」「视频1」指代素材，图片与视频各自从 1 开始计数、序号互不占用
  （界面实时显示编号对照表，`ref_manifest()`）
- 参考图平台硬要求 ≥240×240，UI 提交前已做本地预检（`MIN_REF_SIDE`）

## UI 层要点（ui/webui.py）

- `generate()` 是 generator，逐步 yield 状态（提交 → 轮询 → 下载），界面实时刷新进度
- 本地参考素材上传后立即预览：缩略图用 `gr.Gallery`（`ref_gallery()` 生成
  `(路径, 说明)` 元组，说明带「图1」「视频1」编号），图片视频都支持（按 MIME 识别）
- 点缩略图 → 右下角悬浮窗看原图/放原视频：`make_dock_handler()` + `.ref-dock` 样式。
  Gallery 要设 `allow_preview=False` 关掉自带的居中放大弹窗（**实测不影响 select 事件**）

### Gradio 6 踩过的坑（改 UI 前必读）

- ⚠️ **`css=` 必须传给 `launch()`**，传给 `Blocks()` 会被**静默忽略**（只打一条
  UserWarning，样式完全不生效）。所以 `CUSTOM_CSS` 由 `main.py` 在 launch 时传入
- 缩略图尺寸靠 `grid-template-columns: repeat(auto-fill, minmax(96px,96px))` 强制：
  Gradio 默认格子最小 `min_width=160`，且 `fit_columns=True` 时少量图片会被拉伸铺满整行
- 隐藏 `gr.File` 自带的文件名列表用 `.ref-uploader .file-preview-holder`；
  上传入口 `.upload-container` 是**独立元素**，不受影响（已实测确认仍可继续添加文件）
- 媒体尺寸约束优先用 Gradio 原生 `height` 参数（如 `height="44vh"`，接受 CSS 字符串），
  比用 CSS 猜组件内部 DOM 结构可靠得多
- 验证 UI 改动的低成本办法：Edge 无头模式截图
  `msedge --headless=new --screenshot=x.png --window-size=1400,900 URL`
  （CDP 还能驱动真实点击，见本项目用过的 `websockets` + `Input.dispatchMouseEvent`）
- **任务一创建就把 meta.json 落盘**（status=RUNNING）：超时/关页面/中断后可用 task_id
  在「任务续查」面板取回已生成的视频，不重新生成、不重复计费
- 本地文件转 data URI 前用**魔数嗅探 MIME**（平台按内容判断类型，扩展名不可靠）；
  本地保护性上限：图 8MB / 视频 30MB
- 轮询允许连续 `POLL_MAX_ERRORS`（5）次网络错误才放弃；超时 ≠ 失败，任务往往仍在平台执行
- 历史记录 = 扫描 `outputs/*.json`，只显示 status=SUCCEEDED 且视频文件仍存在的条目

## 其他注意事项

- **生成很慢**：实测 t2v 10s/720P 约 5~6 分钟，r2v 5s/720P 约 29 分钟。轮询超时后
  优先引导用户用「任务续查」取回结果，而不是重新提交
- Windows 控制台出现 `ConnectionResetError` / `_call_connection_lost` 堆栈是 asyncio
  已知噪音（cpython#109383），`main.py` 已做屏蔽，不要当作 bug 去"修复"
- `outputs/` 与 `.env` 已 gitignore
