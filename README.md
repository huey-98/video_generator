# AI 视频生成器

基于**阿里百炼平台 · 通义万相视频模型**的 Web 视频生成工具，Python 3.12 + Gradio。

## 功能

- 🎛️ **4 个模型自由切换**（切换后界面参数自动适配）：

  | 模型 ID | 说明 | 时长 | 分辨率 | 参考图 |
  |---|---|---|---|---|
  | `wan2.7-t2v-2026-06-12` | 文生视频 | 2~15 秒 | 720P / 1080P | 不支持 |
  | `wan2.7-r2v-2026-06-12` | 参考生视频（锁定主体外观） | 2~15 秒 | 720P / 1080P | **必传**，1~5 张，可本地上传 |
  | `wan3.0-video` | 新一代全能模型，原生音画同步 | 2~30 秒 | 480P / 720P / 1080P | 可选，≤10 张，**仅限图片 URL** |
  | `wan3.0-video-prime` | 万相3.0 高速版 | 2~30 秒 | 480P / 720P / 1080P | 可选，≤10 张，**仅限图片 URL** |

## 参考图使用说明（实测结论）

平台的参考素材字段结构为（2026-09-24 实测确认）：

```json
{
  "input": {
    "prompt": "提示词",
    "media": [
      {"type": "reference_image", "url": "https://... 或 data:image/png;base64,..."}
    ]
  }
}
```

- `type` 的合法取值只有三个：`reference_image`（参考图）、`reference_video`（参考视频）、`first_frame`（首帧）
- **`wan2.7-r2v` 支持本地上传**：界面上传的图片会自动转成 base64 data URI，平台会自行转存
- **`wan3.0-video` / `wan3.0-video-prime` 只接受 http/https 图片地址**，本地上传会被平台拒绝（界面已对这两个模型隐藏上传控件），需要先把图片传到图床再粘贴 URL

- ⏳ 生成过程实时显示任务状态与用时（异步任务：提交 → 轮询 → 下载）
- 🖼️ 参考图支持**本地上传**（自动转 base64）或**粘贴 URL** 两种方式
- 💰 费用预估（在 `config.yaml` 中填写模型单价后启用）
- 🔍 任务续查：任务一创建就记录 task_id，超时/关页面/中断后可在面板里查询，
  已完成的任务能直接下载回本地——**不必重新生成，不会重复计费**
- 📁 历史记录：每次生成的视频 + 参数元数据保存在 `outputs/`，点击即可回看
- 🌱 高级参数：负面提示词、随机种子、智能改写、水印

## 快速开始

```bash
# 1. 安装依赖（已在 .venv 中完成）
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 2. 配置 API Key：打开 .env，填入百炼 API Key
#    DASHSCOPE_API_KEY=sk-xxxx

# 3. 启动
.venv/Scripts/python.exe main.py
```

浏览器自动打开 http://127.0.0.1:7860 （`--no-browser` 可禁止自动打开，`--port 7861` 可换端口）。

## 目录结构

```
video_genetor/
├── main.py            # 启动入口
├── config.yaml        # 模型注册表 + 默认参数（改配置即可调模型，无需动代码）
├── .env               # API Key（不要提交到 git）
├── src/
│   ├── config.py      # 配置加载
│   ├── registry.py    # 模型能力描述
│   └── client.py      # 百炼 API 客户端（原生 HTTP 异步任务）
├── ui/webui.py        # Gradio 界面
└── outputs/           # 生成的视频（.mp4）+ 参数元数据（.json）
```

## 生成耗时参考（实测）

| 场景 | 实测耗时 |
|---|---|
| `wan2.7-t2v` 10 秒 720P | 约 5~6 分钟 |
| `wan2.7-r2v` 5 秒 720P + 1 张参考图 | **约 29 分钟** |

参考生视频（r2v）明显更慢，请预留足够时间。**超时 ≠ 失败**：任务往往仍在平台执行，
超时后 task_id 会自动填入界面底部的「任务续查」，过一会儿点「查询状态」即可把已生成好的
视频直接取回本地（不会重复计费）。

## 常见问题

1. **创建任务失败 / 提示地域相关错误**：API Key、模型、域名必须同属一个地域。
   北京地域默认 `https://dashscope.aliyuncs.com/api/v1`；若你的业务空间使用专属域名，
   在 `.env` 中设置 `DASHSCOPE_BASE_URL=https://{业务空间ID}.cn-beijing.maas.aliyuncs.com/api/v1`。
2. **生成耗时**：视频生成是异步任务，通常 1~5 分钟，界面会实时显示状态，请耐心等待。
3. **视频链接时效**：平台返回的视频 URL 仅 24 小时有效，程序已自动下载到 `outputs/`。
4. **参考图相关**：
   - 本地上传单张不超过 8MB，超限请改用图片 URL
   - 参考图**至少 240×240 像素**（平台硬性要求；界面已做提交前预检，不合规会直接拦下）
   - 提示 `only accept http/https` 说明当前模型不支持本地上传（wan3.0 系列），请改用图片 URL
   - 提示 `Field required: input.media` 之类的参数错误，说明平台字段结构又变了——
     把完整报错发给我，或在 `config.yaml` 中调整对应模型的 `ref_images` 配置（字段名/结构都在那里，无需改代码）
5. **费用**：按生成秒数计费，任务**提交即扣费**（失败通常返还，以平台规则为准）。
   测试时建议用「最短时长 + 最低分辨率」。价格见
   [百炼计费页](https://help.aliyun.cn/zh/model-studio/billing)，
   查到单价后可填入 `config.yaml` 中对应模型的 `price_per_second` 以启用费用预估。
6. **模型参数更新**：若百炼调整了某模型的参数（如参考图字段名、分辨率档位），
   直接修改 `config.yaml` 中对应模型的配置即可，无需改代码。

7. **控制台出现 `ConnectionResetError` / `Exception in callback
   _ProactorBasePipeTransport._call_connection_lost`**：这是 Windows 上 asyncio 的已知噪音
   （浏览器关闭/刷新连接时触发，[cpython#109383](https://github.com/python/cpython/issues/109383)），
   不影响生成。`main.py` 已做屏蔽处理。

## 参考文档

- [万相2.7 文生视频 API](https://help.aliyun.com/zh/model-studio/developer-reference/text-to-video)
- [万相2.7 参考生视频 API](https://help.aliyun.com/zh/model-studio/wan-video-to-video-api-reference)
- [Wan3.0 模型介绍](https://developer.aliyun.com/article/1757976)
