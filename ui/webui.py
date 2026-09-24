"""Gradio Web 界面：模型切换 + 参数配置 + 生成进度 + 历史记录"""
from __future__ import annotations

import base64
import json
import mimetypes
import time
from datetime import datetime
from pathlib import Path

import gradio as gr
from PIL import Image

from src.client import BailianClient, BailianError
from src.config import load_app_config
from src.payload import build_payload

MAX_REF_FILE_MB = 8  # 单张参考图大小上限（base64 内联会放大体积，超限建议改用 URL）
MIN_REF_SIDE = 240   # 平台要求参考图至少 240x240（实测报错：resolution must be at least 240x240）
POLL_MAX_ERRORS = 5  # 轮询时允许的连续网络错误次数

HISTORY_HEADERS = ["时间", "模型", "提示词", "时长(秒)", "分辨率", "文件"]


# 图片魔数 → MIME，平台会按内容判断文件类型，用魔数比扩展名可靠
IMAGE_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
]


def sniff_image_mime(raw: bytes, fallback_name: str) -> str:
    for magic, mime in IMAGE_MAGIC:
        if raw.startswith(magic):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return mimetypes.guess_type(fallback_name)[0] or "image/png"


def image_size(path: str) -> tuple[int, int]:
    """读取本地图片尺寸（用于提交前的合规检查）。"""
    try:
        with Image.open(path) as im:
            return im.size
    except Exception as e:
        raise BailianError(f"无法读取图片 {Path(path).name}：{e}")


def to_data_uri(path: str) -> str:
    """本地图片文件 → data URI（实测 wan2.7-r2v 支持，平台会自动转存到 OSS）。"""
    p = Path(path)
    if p.stat().st_size > MAX_REF_FILE_MB * 1024 * 1024:
        raise BailianError(f"参考图 {p.name} 超过 {MAX_REF_FILE_MB}MB，请压缩后重试，或改用图片 URL")
    raw = p.read_bytes()
    mime = sniff_image_mime(raw, p.name)
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_app() -> gr.Blocks:
    cfg = load_app_config()
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    _client_holder: dict = {}

    def get_client() -> BailianClient:
        if not cfg.api_key or cfg.api_key.startswith("sk-在这里"):
            raise BailianError(
                "未配置 API Key：请打开项目根目录的 .env 文件，"
                "把 DASHSCOPE_API_KEY 改为你的百炼 API Key 后重启程序。"
            )
        if "client" not in _client_holder:
            _client_holder["client"] = BailianClient(
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                poll_interval=cfg.poll_interval,
                task_timeout=cfg.task_timeout,
                download_timeout=cfg.download_timeout,
            )
        return _client_holder["client"]

    def model_choices() -> list:
        return [(f"{m.name}（{m.id}）", m.id) for m in cfg.models]

    def default_model_id() -> str | None:
        ids = [m.id for m in cfg.models]
        if cfg.default_model in ids:
            return cfg.default_model
        return ids[0] if ids else None

    # ---------------- 历史记录 ----------------

    def save_meta(meta: dict, base_name: str) -> None:
        meta_path = cfg.output_dir / f"{base_name}.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def scan_history() -> tuple[list, list]:
        """扫描 outputs 目录，返回 (表格行, 元数据列表)。"""
        rows, metas = [], []
        for meta_path in sorted(
            cfg.output_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("status") != "SUCCEEDED":
                continue
            fname = meta.get("file")
            if not fname or not (cfg.output_dir / fname).exists():
                continue
            prompt = meta.get("prompt", "")
            rows.append([
                meta.get("created_at", ""),
                meta.get("model", ""),
                prompt[:30] + ("…" if len(prompt) > 30 else ""),
                meta.get("duration", ""),
                meta.get("resolution", ""),
                fname,
            ])
            metas.append(meta)
        return rows, metas

    def find_meta_by_task_id(task_id: str) -> dict | None:
        """按 task_id 找回此前记录（超时/失败时存下的），用于补全模型、提示词等信息。"""
        for meta_path in cfg.output_dir.glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("task_id") == task_id:
                return meta
        return None

    # ---------------- 界面辅助 ----------------

    def cost_text(model_id: str, duration) -> str:
        try:
            m = cfg.get_model(model_id)
        except KeyError:
            return ""
        if m.price_per_second:
            est = m.price_per_second * float(duration or 0)
            return (
                f"💰 预估费用：约 **¥{est:.2f}**"
                f"（¥{m.price_per_second}/秒 × {int(duration)}秒，以实际账单为准）"
            )
        return (
            "💰 按生成秒数计费，提交即扣费"
            "（在 `config.yaml` 中填写 `price_per_second` 后可显示预估金额）"
        )

    def model_info_text(model_id: str) -> str:
        m = cfg.get_model(model_id)
        sizes = " / ".join(label for label, _ in m.size_options)
        lines = [
            f"`{m.id}`",
            "",
            m.description,
            "",
            f"- 时长：{m.duration_min}~{m.duration_max} 秒",
            f"- 分辨率：{sizes}",
        ]
        if m.ref_enabled:
            req = "**必传**" if m.ref_required else "可选"
            lines.append(f"- 参考图：{req}，最多 {m.ref_max} 张")
            if m.ref_url_scheme == "http":
                lines.append("- ⚠️ 该模型参考图**只支持 http/https 图片地址**，不能用本地上传")
            else:
                lines.append("- 参考图支持本地上传或图片 URL")
        return "\n".join(lines)

    def model_controls(model_id: str) -> tuple:
        """模型切换时，返回各控件的新状态（与 outputs 顺序一致）。"""
        m = cfg.get_model(model_id)
        size_choices = m.size_options
        return (
            model_info_text(model_id),                                     # model_info
            gr.update(visible=m.supports("negative_prompt")),              # neg_prompt
            gr.update(visible=m.ref_enabled),                              # ref_group
            # wan3.0 系列只接受 http(s) 地址，对它们隐藏本地上传控件
            gr.update(visible=m.ref_enabled and m.ref_url_scheme != "http"),  # ref_files
            gr.update(                                                     # duration
                minimum=m.duration_min,
                maximum=m.duration_max,
                value=m.duration_default,
                label=f"视频时长（{m.duration_min}~{m.duration_max} 秒，按秒计费）",
            ),
            gr.update(                                                     # resolution
                choices=size_choices,
                value=size_choices[0][1] if size_choices else None,
            ),
            cost_text(model_id, m.duration_default),                       # cost_md
        )

    # ---------------- 生成主流程 ----------------

    def generate(model_id, prompt, neg, ref_files, ref_urls_text,
                 dur, res, seed_val, p_ext, wm):
        no_change = gr.update()
        # 输出顺序：status_md, video_out, history_df, history_state, resume_input
        empty_ret = ("", no_change, no_change, no_change, no_change)

        try:
            m = cfg.get_model(model_id)
        except KeyError as e:
            yield (f"❌ {e}",) + empty_ret[1:]
            return

        prompt = (prompt or "").strip()
        if not prompt:
            yield ("❌ 请输入提示词",) + empty_ret[1:]
            return

        # ---- 参考图整理：URL（每行一个）+ 本地文件转 base64 ----
        urls = [u.strip() for u in (ref_urls_text or "").splitlines() if u.strip()]
        files = [f for f in (ref_files or []) if f]
        if m.ref_enabled:
            if m.ref_required and not (urls or files):
                yield (f"❌ 「{m.name}」需要至少 1 张参考图"
                       "（上传本地图片或粘贴图片 URL）",) + empty_ret[1:]
                return
            if len(urls) + len(files) > m.ref_max:
                yield (f"❌ 参考图最多 {m.ref_max} 张"
                       f"（当前 {len(urls) + len(files)} 张）",) + empty_ret[1:]
                return
            # wan3.0 系列实测只接受 http/https 地址，本地上传的 base64 会被拒绝
            if files and m.ref_url_scheme == "http":
                yield (f"❌ 「{m.name}」只接受 http/https 图片地址，不支持本地上传。\n\n"
                       f"请把 {len(files)} 张本地图片先传到图床，再以 URL 形式粘贴到下方输入框"
                       "（每行一个）；或改用「万相2.7 参考生视频」（它支持本地上传）。",
                       ) + empty_ret[1:]
                return
            try:
                # 提交前先本地校验图片尺寸，避免平台返回 InvalidParameter 白跑一趟
                for f in files:
                    w, h = image_size(f)
                    if min(w, h) < MIN_REF_SIDE:
                        raise BailianError(
                            f"参考图 {Path(f).name} 尺寸为 {w}x{h}，平台要求至少 "
                            f"{MIN_REF_SIDE}x{MIN_REF_SIDE}，请换一张更清晰的图片"
                        )
                refs = urls + [to_data_uri(f) for f in files]
            except BailianError as e:
                yield (f"❌ {e}",) + empty_ret[1:]
                return
        else:
            refs = []

        # ---- 组装请求体 ----
        input_payload, parameters = build_payload(
            m,
            prompt=prompt,
            negative_prompt=neg,
            refs=refs,
            duration=int(dur),
            size=res,
            seed=seed_val,
            prompt_extend=p_ext,
            watermark=wm,
        )

        meta = {
            "model": m.id,
            "model_name": m.name,
            "prompt": prompt,
            "negative_prompt": input_payload.get("negative_prompt", ""),
            "duration": int(dur),
            "resolution": res,
            "parameters": parameters,
            "ref_count": len(refs),
            # 记录请求体（参考素材内容太长，只存数量），便于失败时排查
            "input_preview": {
                k: (f"<{len(v)} 个参考素材>" if k == m.ref_param else v)
                for k, v in input_payload.items()
            },
        }

        try:
            client = get_client()
        except BailianError as e:
            yield (f"❌ {e}",) + empty_ret[1:]
            return

        # ---- 1. 创建任务 ----
        yield (f"📤 正在提交任务…（模型 `{m.id}`，{int(dur)} 秒，{res}）",
               None, no_change, no_change, no_change)
        try:
            task_id, _ = client.create_task(m.id, input_payload, parameters)
        except BailianError as e:
            yield (f"❌ 创建任务失败：{e}", no_change, no_change, no_change, no_change)
            return
        meta["task_id"] = task_id

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"{ts}_{m.id}_{task_id[:8]}"

        # 任务一创建就先落盘：万一关掉页面、程序中断或轮询断开，task_id 也不会丢
        meta.update({
            "status": "RUNNING",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        save_meta(meta, base_name)

        # ---- 2. 轮询任务状态 ----
        t0 = time.time()
        err_count = 0
        while True:
            try:
                data = client.get_task(task_id)
                err_count = 0
            except BailianError as e:
                err_count += 1
                if err_count >= POLL_MAX_ERRORS:
                    meta.update({"status": "QUERY_FAILED", "error": str(e)})
                    save_meta(meta, base_name)
                    yield (f"❌ 连续 {POLL_MAX_ERRORS} 次查询失败：{e}\n\n"
                           f"task_id 已填入下方「任务续查」，可在下方点「查询状态」重试",
                           no_change, no_change, no_change, task_id)
                    return
                yield (f"⚠️ 查询失败（第 {err_count} 次，将重试）：{e}",
                       no_change, no_change, no_change, no_change)
                time.sleep(client.poll_interval)
                continue

            status = client.task_status(data)
            elapsed = time.time() - t0
            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "CANCELED"):
                output = data.get("output") or {}
                err_msg = f"{output.get('code', '')} {output.get('message', '')}".strip()
                meta.update({"status": status, "error": err_msg,
                             "response": data.get("output")})
                save_meta(meta, base_name)
                yield (f"❌ 任务 {status}\n\n{err_msg or '（平台未返回具体原因）'}\n\n"
                       f"task_id：`{task_id}`", no_change, no_change, no_change, task_id)
                return
            if elapsed > client.task_timeout:
                # 超时不等于失败：任务可能仍在平台执行，记录 task_id 以便续查
                meta.update({"status": "TIMEOUT", "timeout_sec": client.task_timeout})
                save_meta(meta, base_name)
                yield (f"⏱️ 等待超时（已轮询 {client.task_timeout} 秒），任务**可能仍在平台执行**。\n\n"
                       f"task_id 已自动填入下方「任务续查」，稍后点「查询状态」即可取回结果"
                       f"（task_id 24 小时内有效）",
                       no_change, no_change, no_change, task_id)
                return
            yield (f"⏳ 生成中… 状态 `{status}`，已用时 **{elapsed:.0f}** 秒\n\n"
                   f"- task_id：`{task_id}`\n- 模型：{m.name}（{int(dur)} 秒 / {res}）",
                   no_change, no_change, no_change, no_change)
            time.sleep(client.poll_interval)

        # ---- 3. 下载视频 ----
        try:
            video_url = client.extract_video_url(data)
        except BailianError as e:
            yield (f"❌ {e}\n\n原始返回：\n```json\n"
                   f"{json.dumps(data.get('output'), ensure_ascii=False)[:800]}\n```",
                   no_change, no_change, no_change, task_id)
            return

        yield ("📥 生成成功，正在下载视频到本地…",
               no_change, no_change, no_change, no_change)
        fname = f"{base_name}.mp4"
        try:
            dest = client.download_video(video_url, cfg.output_dir / fname)
        except BailianError as e:
            yield (f"⚠️ 视频下载失败：{e}\n\n"
                   f"视频链接 24 小时内有效，可手动下载：\n\n{video_url}\n\n"
                   f"（task_id 已填入「任务续查」，可稍后点「查询状态」重新下载）",
                   no_change, no_change, no_change, task_id)
            return

        elapsed = time.time() - t0
        meta.update({
            "status": "SUCCEEDED",
            "file": fname,
            "video_url": video_url,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": round(elapsed, 1),
            "usage": data.get("usage"),
        })
        save_meta(meta, base_name)

        rows, metas = scan_history()
        yield (f"✅ 生成完成！总用时 **{elapsed:.0f}** 秒\n\n"
               f"- 本地文件：`outputs/{fname}`\n- task_id：`{task_id}`",
               str(dest), rows, metas, no_change)

    # ---------------- 任务续查 ----------------

    def load_recent_pending() -> str:
        """取出最近一条尚未完成的任务 ID（关掉页面/超时/中断后可用它续查）。"""
        for meta_path in sorted(
            cfg.output_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("status") in ("RUNNING", "PENDING", "TIMEOUT") and meta.get("task_id"):
                return meta["task_id"]
        return ""

    def query_task(task_id_text):
        """按 task_id 查询任务：已完成的直接下载回本地，避免超时后重复生成（重复付费）。"""
        no_out = (gr.update(), gr.update(), gr.update())
        task_id = (task_id_text or "").strip()
        if not task_id:
            return ("❌ 请先填入 task_id（超时或失败时界面会自动填入）",) + no_out

        try:
            client = get_client()
            data = client.get_task(task_id)
        except BailianError as e:
            return (f"❌ 查询失败：{e}",) + no_out

        output = data.get("output") or {}
        status = client.task_status(data)
        info = (
            f"任务 `{task_id}`\n\n"
            f"- 状态：**{status}**\n"
            f"- 提交时间：{output.get('submit_time', '—')}\n"
            f"- 结束时间：{output.get('end_time') or '（仍在进行中）'}"
        )

        if status in ("FAILED", "CANCELED"):
            err = f"{output.get('code', '')} {output.get('message', '')}".strip()
            return (f"{info}\n\n❌ {err or '（平台未返回具体原因）'}",) + no_out

        if status != "SUCCEEDED":
            return (f"{info}\n\n⏳ 仍在生成中，过几分钟再点一次「查询状态」"
                    f"（task_id 24 小时内有效）",) + no_out

        try:
            video_url = client.extract_video_url(data)
        except BailianError as e:
            return (f"{info}\n\n❌ {e}",) + no_out

        # 平台通常不返回 model/prompt，用同 task_id 的历史记录补全，让历史列表更可读
        prev = find_meta_by_task_id(task_id) or {}
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        model = output.get("model") or prev.get("model") or "task"
        base_name = f"{ts}_{model}_{task_id[:8]}"
        fname = f"{base_name}.mp4"
        try:
            dest = client.download_video(video_url, cfg.output_dir / fname)
        except BailianError as e:
            return (f"{info}\n\n⚠️ 下载失败：{e}\n\n视频链接（24 小时有效）：\n{video_url}",
                    ) + no_out

        save_meta({
            "model": model,
            "model_name": prev.get("model_name") or "（任务续查）",
            "prompt": output.get("prompt") or prev.get("prompt", ""),
            "duration": output.get("duration") or prev.get("duration", ""),
            "resolution": output.get("resolution") or prev.get("resolution", ""),
            "task_id": task_id,
            "status": "SUCCEEDED",
            "file": fname,
            "video_url": video_url,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "usage": data.get("usage"),
        }, base_name)

        rows, metas = scan_history()
        return (f"{info}\n\n✅ 已下载到 `outputs/{fname}`（已存入历史记录）",
                str(dest), rows, metas)

    # ---------------- 历史回看 ----------------

    def refresh_history():
        rows, metas = scan_history()
        return rows, metas

    def on_history_select(metas, evt: gr.SelectData):
        if not metas or evt.index[0] >= len(metas):
            return gr.update(), gr.update()
        meta = metas[evt.index[0]]
        video_path = cfg.output_dir / meta["file"]
        detail = (
            f"📼 **历史记录** · {meta.get('created_at', '')}\n\n"
            f"> {meta.get('prompt', '')}\n\n"
            f"- 模型：`{meta.get('model', '')}`\n"
            f"- 时长：{meta.get('duration', '')} 秒 · "
            f"分辨率：{meta.get('resolution', '')} · "
            f"生成用时：{meta.get('elapsed_sec', '?')} 秒\n"
            f"- task_id：`{meta.get('task_id', '')}`"
        )
        return str(video_path), detail

    # ---------------- 界面布局 ----------------

    with gr.Blocks(title="AI 视频生成器 · 阿里百炼万相") as demo:
        gr.Markdown("# 🎬 AI 视频生成器\n基于阿里百炼 · 通义万相视频模型，支持多模型切换")
        if not cfg.api_key or cfg.api_key.startswith("sk-在这里"):
            gr.Markdown(
                "> ⚠️ **尚未配置 API Key**：请打开项目根目录的 `.env` 文件，"
                "填入你的百炼 API Key（`DASHSCOPE_API_KEY=sk-...`）后重启程序。"
            )

        with gr.Row():
            # 左侧：参数区
            with gr.Column(scale=5):
                model_dd = gr.Dropdown(
                    choices=model_choices(), value=default_model_id(),
                    label="选择模型", interactive=True,
                )
                model_info = gr.Markdown()
                prompt = gr.Textbox(
                    label="提示词（描述想要的视频画面）", lines=4,
                    placeholder="例如：一只小猫在月光下的屋顶上奔跑，城市霓虹灯在远处闪烁，电影级画质",
                )
                neg_prompt = gr.Textbox(
                    label="负面提示词（不希望出现的内容）", lines=2, visible=False,
                    placeholder="例如：模糊、变形、低画质",
                )
                with gr.Group(visible=False) as ref_group:
                    gr.Markdown("**参考素材**（用于参考生视频）")
                    ref_files = gr.File(
                        label="上传参考图片（可多张）",
                        file_count="multiple", file_types=["image"],
                    )
                    ref_urls = gr.Textbox(
                        label="或粘贴参考图片 URL（每行一个，可与本地图片混用）", lines=2,
                    )
                duration = gr.Slider(minimum=2, maximum=15, step=1, value=5, label="视频时长")
                resolution = gr.Dropdown(choices=[], value=None, label="分辨率")
                with gr.Accordion("高级参数", open=False):
                    seed = gr.Number(label="随机种子（-1 = 随机）", value=-1, precision=0)
                    prompt_extend = gr.Checkbox(
                        label="智能改写提示词（prompt_extend）",
                        value=bool(cfg.defaults.get("prompt_extend", True)),
                    )
                    watermark = gr.Checkbox(
                        label="添加水印",
                        value=bool(cfg.defaults.get("watermark", False)),
                    )
                cost_md = gr.Markdown()
                gen_btn = gr.Button("🚀 开始生成", variant="primary", size="lg")

            # 右侧：结果区
            with gr.Column(scale=6):
                video_out = gr.Video(label="生成结果", interactive=False)
                status_md = gr.Markdown("就绪。")

        with gr.Accordion("📁 历史记录（点击任意行可回看视频）", open=False):
            refresh_btn = gr.Button("🔄 刷新列表")
            history_df = gr.Dataframe(
                headers=HISTORY_HEADERS, value=[], interactive=False, wrap=True,
            )
        history_state = gr.State([])

        with gr.Accordion("🔍 任务续查（超时/失败后的任务可在这里取回，不必重新生成）", open=False):
            gr.Markdown(
                "任务超时或查询中断时，task_id 会自动填到这里。"
                "生成中的任务过几分钟点一次「查询状态」，完成后会自动下载到本地。"
            )
            with gr.Row():
                resume_input = gr.Textbox(
                    label="task_id", placeholder="粘贴 task_id", scale=4,
                )
                resume_btn = gr.Button("查询状态", scale=1)
                resume_recent_btn = gr.Button("载入最近未完成任务", scale=1)

        # ---------------- 事件绑定 ----------------

        model_dd.change(
            fn=model_controls, inputs=[model_dd],
            outputs=[model_info, neg_prompt, ref_group, ref_files,
                     duration, resolution, cost_md],
        )
        duration.change(
            fn=lambda mid, d: cost_text(mid, d),
            inputs=[model_dd, duration], outputs=[cost_md],
        )
        gen_btn.click(
            fn=generate,
            inputs=[model_dd, prompt, neg_prompt, ref_files, ref_urls,
                    duration, resolution, seed, prompt_extend, watermark],
            outputs=[status_md, video_out, history_df, history_state, resume_input],
        )
        refresh_btn.click(
            fn=refresh_history, outputs=[history_df, history_state],
        )
        resume_btn.click(
            fn=query_task, inputs=[resume_input],
            outputs=[status_md, video_out, history_df, history_state],
        )
        resume_recent_btn.click(
            fn=load_recent_pending, outputs=[resume_input],
        )
        history_df.select(
            fn=on_history_select, inputs=[history_state],
            outputs=[video_out, status_md],
        )

        def _initial_load():
            rows, metas = scan_history()
            return model_controls(default_model_id()) + (rows, metas)

        demo.load(
            fn=_initial_load,
            outputs=[model_info, neg_prompt, ref_group, ref_files, duration,
                     resolution, cost_md, history_df, history_state],
        )

    return demo
