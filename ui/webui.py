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

MAX_REF_IMAGE_MB = 8   # base64 路径的参考图大小上限（内联会放大 1/3 体积，超限建议用 URL 或上传）
MAX_REF_VIDEO_MB = 30  # base64 路径的参考视频大小上限（本地保护性上限，非平台实测值）
# 上传到百炼临时空间时的上限（平台值，比 base64 路径宽松——文件本身不经 base64 放大）
MAX_UPLOAD_IMAGE_MB = 20    # 平台：参考图 ≤20MB
MAX_UPLOAD_VIDEO_MB = 100   # 平台：参考视频 ≤100MB
MIN_REF_SIDE = 240     # 平台要求参考图至少 240x240（实测报错：resolution must be at least 240x240）
POLL_MAX_ERRORS = 5    # 轮询时允许的连续网络错误次数

HISTORY_HEADERS = ["时间", "模型", "提示词", "时长(秒)", "分辨率", "文件"]

REF_LABELS = {"image": "参考图", "video": "参考视频"}

# 提示词中引用参考素材的编号前缀（实测各类素材分别计数，序号互不占用）
REF_PREFIX = {"image": "图", "video": "视频"}

# ------------------------------------------------------------
# 自定义样式
#   .ref-uploader : 隐藏 Gradio 自带的文件名列表，列表职责交给缩略图
#   .ref-thumbs   : 缩略图网格，强制小尺寸
#   .ref-dock     : 右下角悬浮预览窗，点击缩略图后在这里显示原图/原视频
#
# ※ Gradio 6 起 css 必须传给 launch()，传给 Blocks() 会被静默忽略（只发一条警告），
#   因此这里导出 CUSTOM_CSS 由 main.py 在 launch 时传入。
# ------------------------------------------------------------
CUSTOM_CSS = """
.ref-uploader .file-preview-holder { display: none !important; }

/* 强制格子尺寸：Gradio 默认最小 160px 且会把少量图片拉伸铺满整行，
   用 auto-fill + minmax 固定成小方格子，才是真正的缩略图 */
.ref-thumbs .grid-container {
    grid-template-columns: repeat(auto-fill, minmax(96px, 96px)) !important;
    gap: 6px !important;
    justify-content: start !important;
}
.ref-thumbs .thumbnail-item {
    max-height: 104px !important;
    border-radius: 6px;
}
.ref-thumbs .thumbnail-item img,
.ref-thumbs .thumbnail-item video {
    max-height: 104px !important;
    object-fit: contain !important;
}

/* 右下角悬浮窗：position:fixed 保证滚动时停在原位，不随页面移动 */
.ref-dock {
    position: fixed !important;
    right: 18px !important;
    bottom: 18px !important;
    left: auto !important;
    top: auto !important;
    width: min(46vw, 640px);
    z-index: 1000;
    background: var(--body-background-fill, #ffffff);
    border: 1px solid var(--border-color-primary, #d0d0d0);
    border-radius: 10px;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.24);
    padding: 6px 10px 10px;
}
/* 兜底：万一 Gradio 内部结构变化，也不让媒体撑破窗口 */
.ref-dock img,
.ref-dock video {
    max-width: 100% !important;
    object-fit: contain !important;
}
.ref-dock .ref-dock-head {
    align-items: center !important;
    gap: 8px !important;
}
.ref-dock .ref-dock-close {
    min-width: 34px !important;
    max-width: 34px !important;
    flex: none !important;
}
"""


# 图片魔数 → MIME，平台会按内容判断文件类型，用魔数比扩展名可靠
IMAGE_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
]

# 视频魔数（部分在固定偏移处）
VIDEO_MAGIC = [
    (b"\x1aE\xdf\xa3", "video/webm"),
    (b"\x00\x00\x01\xba", "video/mpeg"),
    (b"OggS", "video/ogg"),
]


def sniff_image_mime(raw: bytes, fallback_name: str) -> str:
    for magic, mime in IMAGE_MAGIC:
        if raw.startswith(magic):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return mimetypes.guess_type(fallback_name)[0] or "image/png"


def sniff_video_mime(raw: bytes, fallback_name: str) -> str:
    for magic, mime in VIDEO_MAGIC:
        if raw.startswith(magic):
            return mime
    # MP4 / MOV 系列：第 4~8 字节为 "ftyp"
    if raw[4:8] == b"ftyp":
        return "video/quicktime" if raw[8:12] == b"qt  " else "video/mp4"
    if raw[:4] == b"RIFF" and raw[8:12] == b"AVI ":
        return "video/x-msvideo"
    return mimetypes.guess_type(fallback_name)[0] or "video/mp4"


def image_size(path: str) -> tuple[int, int]:
    """读取本地图片尺寸（用于提交前的合规检查）。"""
    try:
        with Image.open(path) as im:
            return im.size
    except Exception as e:
        raise BailianError(f"无法读取图片 {Path(path).name}：{e}")


def to_data_uri(path: str, kind: str = "image") -> str:
    """本地文件 → data URI（base64 备用路径：实测 wan2.7-r2v 支持，当前默认不走这里）。"""
    p = Path(path)
    limit = MAX_REF_IMAGE_MB if kind == "image" else MAX_REF_VIDEO_MB
    label = REF_LABELS.get(kind, "参考素材")
    if p.stat().st_size > limit * 1024 * 1024:
        raise BailianError(
            f"{label} {p.name} 超过 {limit}MB，请压缩后重试，或改用 URL 形式"
        )
    raw = p.read_bytes()
    sniff = sniff_image_mime if kind == "image" else sniff_video_mime
    default = "image/png" if kind == "image" else "video/mp4"
    mime = sniff(raw, p.name) or default
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def collect_refs(m, kind_name: str, files, urls_text, upload_fn=None) -> tuple[list, str]:
    """整理某一类参考素材（本地文件 + URL 每行一个），返回 (地址列表, 错误信息)。

    本地文件怎么变成平台可用的地址，取决于模型的 upload_mode：
        oss    → 用 upload_fn 上传到百炼临时空间换 oss:// 临时URL（当前所有模型统一走这里）
        base64 → 直接内联 data URI（备用路径，wan2.7-r2v 实测支持，可在 config.yaml 切回）

    地址顺序 = **本地文件在前、URL 在后**，与界面上 ref_manifest 显示的编号一致。
    """
    kind = m.refs.kind(kind_name)
    label = REF_LABELS[kind_name]
    files = [f for f in (files or []) if f]
    urls = [u.strip() for u in (urls_text or "").splitlines() if u.strip()]

    if not kind.enabled:
        if files or urls:
            return [], f"「{m.name}」不支持{label}，请清空后再试"
        return [], ""
    if len(urls) + len(files) > kind.max:
        return [], f"{label}最多 {kind.max} 个（当前 {len(urls) + len(files)} 个）"
    if files and kind.upload_mode == "oss" and upload_fn is None:
        return [], (f"「{m.name}」的{label}不接受 base64，本地文件必须先上传换取临时 URL，"
                    f"但当前无法连接百炼（请检查 API Key 配置）")
    try:
        local_urls: list = []
        if files:
            # 参考图提交前先本地校验尺寸，避免平台返回 InvalidParameter 白跑一趟
            if kind_name == "image":
                for f in files:
                    w, h = image_size(f)
                    if min(w, h) < MIN_REF_SIDE:
                        raise BailianError(
                            f"参考图 {Path(f).name} 尺寸为 {w}x{h}，平台要求至少 "
                            f"{MIN_REF_SIDE}x{MIN_REF_SIDE}，请换一张更清晰的图片"
                        )
            if kind.upload_mode == "oss":
                local_urls = [upload_fn(f, kind_name) for f in files]
            else:
                local_urls = [to_data_uri(f, kind_name) for f in files]
        return local_urls + urls, ""
    except BailianError as e:
        return [], str(e)


def ref_manifest(m, img_files, img_urls, vid_files, vid_urls) -> str:
    """列出参考素材的编号对照表。

    提示词用「图1」「视频1」指代素材，编号 = 素材在 media 数组中的顺序；
    图片与视频分别从 1 开始计数。界面上直接显示，避免用户靠猜。
    """
    if not m.ref_enabled:
        return ""

    def numbered(kind_name: str, files, urls_text) -> list:
        kind = m.refs.kind(kind_name)
        if not kind.enabled:
            return []
        prefix = REF_PREFIX[kind_name]
        sources = [Path(f).name for f in (files or []) if f]
        sources += [u.strip() for u in (urls_text or "").splitlines() if u.strip()]
        out = []
        for i, src in enumerate(sources):
            shown = src if len(src) <= 48 else src[:45] + "…"
            out.append(f"`{prefix}{i + 1}` = {shown}")
        return out

    items = numbered("image", img_files, img_urls) + numbered(
        "video", vid_files, vid_urls
    )
    if not items:
        return ""
    return (
        "**素材编号**（写提示词时用这些编号指代，按添加顺序）："
        + "　".join(items)
    )


def ref_file_label(m, kind_name: str) -> str:
    """本地文件上传框的标签：oss 模式下说明文件会先换成临时 URL。"""
    kind = m.refs.kind(kind_name)
    prefix = REF_PREFIX[kind_name]
    unit = "张" if kind_name == "image" else "个"
    label = f"上传{REF_LABELS[kind_name]}（可多{unit}，按顺序编号为 {prefix}1、{prefix}2…）"
    if kind.upload_mode == "oss":
        label += "——生成时自动上传换取临时URL，也可点下方「📤」预上传"
    return label


def make_dock_handler(kind_name: str):
    """点击缩略图 → 在右下角悬浮窗显示原图/原视频。

    窗口尺寸由 CSS 限死（position:fixed + 原生 height 参数），
    图片或视频超出窗口时自动等比缩小，保证完整显示、不裁切。

    返回顺序与 outputs 一致：[悬浮窗, 图片, 视频, 标题]
    """
    is_image = kind_name == "image"

    def handler(files, evt: gr.SelectData):
        paths = [x for x in (files or []) if x]
        idx = evt.index
        if isinstance(idx, (list, tuple)):   # 不同 Gradio 版本可能是元组
            idx = idx[0] if idx else None
        if idx is None or not (0 <= idx < len(paths)):
            return gr.update(), gr.update(), gr.update(), gr.update()
        path = paths[idx]
        title = (f"**原图** · {Path(path).name}" if is_image
                 else f"**参考视频** · {Path(path).name}")
        return (
            gr.update(visible=True),                                       # 悬浮窗
            gr.update(value=path if is_image else None, visible=is_image),
            gr.update(value=None if is_image else path, visible=not is_image),
            title,
        )

    return handler


def ref_gallery(kind_name: str, files) -> list:
    """把选中的本地文件整理成带编号的预览项 [(文件路径, 说明), ...]。

    编号用「图1」「视频1」这种前缀，与 ref_manifest 的编号表一致，
    这样预览里看到的就是提示词里该写的那个编号。
    """
    prefix = REF_PREFIX[kind_name]
    return [
        (f, f"{prefix}{i} · {Path(f).name}")
        for i, f in enumerate([x for x in (files or []) if x], 1)
    ]


def check_totals(m, images: list, videos: list) -> str:
    """检查素材总数（实测 r2v 的 media 数组上限为 5，图 + 视频合计）。"""
    total = len(images) + len(videos)
    if total < m.refs.min_total:
        return (f"「{m.name}」至少需要 {m.refs.min_total} 个参考素材"
                "（参考图或参考视频，上传本地文件或粘贴 URL）")
    if m.refs.max_total and total > m.refs.max_total:
        return (f"参考素材合计最多 {m.refs.max_total} 个"
                f"（当前 {len(images)} 张参考图 + {len(videos)} 个参考视频 = {total} 个）")
    return ""


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

    def ref_manifest_for(model_id, img_files, img_urls, vid_files, vid_urls) -> str:
        """事件回调版：下拉框传来的是模型 id，先解析成 ModelSpec。"""
        try:
            m = cfg.get_model(model_id)
        except KeyError:
            return ""
        return ref_manifest(m, img_files, img_urls, vid_files, vid_urls)

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
            parts = []
            if m.image_max:
                parts.append(f"参考图最多 {m.image_max} 张")
            if m.video_max:
                parts.append(f"参考视频最多 {m.video_max} 个")
            if parts:
                req = "**至少需要 1 个**" if m.refs.min_total else "可选"
                total = f"，合计最多 {m.ref_total_max} 个" if m.refs.max_total else ""
                lines.append(f"- 参考素材：{req}（{'；'.join(parts)}{total}）")
            for name, kind in m.refs.kinds():
                if not kind.enabled:
                    continue
                label = REF_LABELS[name]
                if kind.upload_mode == "oss":
                    lines.append(f"- 📤 {label}：本地文件生成时自动上传换取临时URL"
                                 f"（48 小时有效），也可点「📤」预上传或直接粘贴公网 URL")
                else:
                    lines.append(f"- {label}支持本地上传或 URL")
        return "\n".join(lines)

    # ---------------- 本地文件 → 临时URL ----------------

    def make_uploader(m):
        """返回「本地文件 → oss:// 临时URL」的函数。

        上传时指定的模型必须与后续调用模型一致（平台规则），所以固定用 m.id。
        每次上传都重新取凭证，upload_dir 带新 UUID，因此同名文件不会互相覆盖。
        """
        def _upload(path, kind_name: str) -> str:
            client = get_client()
            p = Path(path)
            limit = MAX_UPLOAD_IMAGE_MB if kind_name == "image" else MAX_UPLOAD_VIDEO_MB
            size_mb = p.stat().st_size / 1048576
            if size_mb > limit:
                raise BailianError(
                    f"{REF_LABELS[kind_name]} {p.name} 为 {size_mb:.1f}MB，"
                    f"超过平台上限 {limit}MB，请压缩后重试"
                )
            with open(p, "rb") as f:
                head = f.read(32)   # 只需文件头即可嗅探类型，不必把整份文件读进内存
            sniff = sniff_image_mime if kind_name == "image" else sniff_video_mime
            return client.upload_file(p, m.id, sniff(head, p.name))

        return _upload

    def upload_refs_for(model_id, kind_name, files, urls_text):
        """「📤 上传换取临时URL」按钮：上传选中的本地文件，地址追加进 URL 输入框。"""
        label = REF_LABELS[kind_name]
        no_change = gr.update()
        try:
            m = cfg.get_model(model_id)
        except KeyError as e:
            yield no_change, no_change, f"❌ {e}"
            return

        files = [f for f in (files or []) if f]
        if not files:
            yield no_change, no_change, f"⚠️ 请先选择要上传的{label}文件"
            return
        kind = m.refs.kind(kind_name)
        if len(files) > kind.max:
            yield no_change, no_change, f"❌ {label}最多 {kind.max} 个（当前 {len(files)} 个）"
            return

        uploader = make_uploader(m)
        existing = [u.strip() for u in (urls_text or "").splitlines() if u.strip()]
        uploaded: list = []
        for i, f in enumerate(files, 1):
            yield (no_change, no_change,
                   f"📤 正在上传{label} {i}/{len(files)}：`{Path(f).name}`…")
            try:
                uploaded.append(uploader(f, kind_name))
            except BailianError as e:
                yield no_change, no_change, f"❌ {e}"
                return

        # 上传完就把本地文件从上传区移除、只留 URL，编号才不会错乱
        yield ("\n".join(existing + uploaded), None,
               f"✅ 已上传 {len(uploaded)} 个{label}，临时 URL 已填入下方输入框"
               f"（**48 小时内有效**，请尽快生成）。\n\n"
               f"文件已从上传区移除，直接点「🚀 开始生成」即可。")

    def make_upload_handler(kind_name: str):
        """给上传按钮绑定固定的素材类别（回调不好传常量，用闭包包一层）。"""
        def handler(model_id, files, urls_text):
            yield from upload_refs_for(model_id, kind_name, files, urls_text)

        return handler

    # ---------------- 本地文件预览 ----------------

    def ref_preview(kind_name: str, files):
        """刷新预览画廊；没有文件时整体隐藏，不占版面。"""
        items = ref_gallery(kind_name, files)
        return gr.update(value=items, visible=bool(items))

    def make_preview_handler(kind_name: str):
        def handler(files):
            return ref_preview(kind_name, files)

        return handler

    def model_controls(model_id: str) -> tuple:
        """模型切换时，返回各控件的新状态（与 outputs 顺序一致）。"""
        m = cfg.get_model(model_id)
        size_choices = m.size_options
        img, vid = m.refs.image, m.refs.video
        img_on, vid_on = m.ref_enabled and img.enabled, m.ref_enabled and vid.enabled
        return (
            model_info_text(model_id),                                     # model_info
            gr.update(visible=m.supports("negative_prompt")),              # neg_prompt
            gr.update(visible=img_on),                                     # ref_image_group
            gr.update(                                                     # ref_image_files
                visible=img_on, label=ref_file_label(m, "image"),
            ),
            # 「上传换临时URL」按钮只在 oss 模式的模型（wan3.0 系列）下出现
            gr.update(visible=img_on and img.upload_mode == "oss"),        # ref_image_upload_btn
            gr.update(visible=vid_on),                                     # ref_video_group
            gr.update(                                                     # ref_video_files
                visible=vid_on, label=ref_file_label(m, "video"),
            ),
            gr.update(visible=vid_on and vid.upload_mode == "oss"),        # ref_video_upload_btn
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
            "",                                                            # ref_msg_md
        )

    # ---------------- 生成主流程 ----------------

    def generate(model_id, prompt, neg,
                 ref_image_files, ref_image_urls_text,
                 ref_video_files, ref_video_urls_text,
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

        # ---- 收集参考素材：参考图 + 参考视频，两类可混用 ----
        # 本地文件若走 oss 模式（wan3.0 系列），提交前要自动上传换临时 URL，
        # 先提示一句——上传一个几十 MB 的视频可能要数秒到数十秒
        uploading = [
            REF_LABELS[name]
            for name, fs in (("image", ref_image_files), ("video", ref_video_files))
            if m.refs.kind(name).enabled
            and m.refs.kind(name).upload_mode == "oss"
            and any(fs or [])
        ]
        if uploading:
            yield (f"📤 正在上传{'、'.join(uploading)}到百炼临时空间"
                   f"（换取 48 小时有效的临时 URL）…",
                   None, no_change, no_change, no_change)

        upload_fn = make_uploader(m)
        images, err = collect_refs(m, "image", ref_image_files, ref_image_urls_text, upload_fn)
        if not err:
            videos, err = collect_refs(m, "video", ref_video_files, ref_video_urls_text, upload_fn)
        if not err:
            err = check_totals(m, images, videos)
        if err:
            yield (f"❌ {err}",) + empty_ret[1:]
            return
        total = len(images) + len(videos)

        # ---- 组装请求体 ----
        input_payload, parameters = build_payload(
            m,
            prompt=prompt,
            negative_prompt=neg,
            images=images,
            videos=videos,
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
            "ref_count": total,
            "ref_images": len(images),
            "ref_videos": len(videos),
            "ref_map": ref_manifest(m, ref_image_files, ref_image_urls_text,
                                    ref_video_files, ref_video_urls_text),
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
                    placeholder=(
                        "例如：图1中的女孩穿上图2里的红色外套，在城市街头向前走，"
                        "镜头缓慢推近，电影级画质\n"
                        "（用了参考素材时，直接用「图1」「图2」「视频1」指代它们即可）"
                    ),
                )
                neg_prompt = gr.Textbox(
                    label="负面提示词（不希望出现的内容）", lines=2, visible=False,
                    placeholder="例如：模糊、变形、低画质",
                )
                with gr.Group(visible=False) as ref_image_group:
                    gr.Markdown("**参考图**（锁定主体外观：人物 / 角色 / 产品）")
                    ref_image_files = gr.File(
                        label="上传参考图（可多张，按顺序编号为 图1、图2…）",
                        file_count="multiple", file_types=["image"],
                        elem_classes=["ref-uploader"],
                    )
                    ref_image_upload_btn = gr.Button(
                        "📤 上传换取临时URL", visible=False, size="sm",
                    )
                    ref_image_preview = gr.Gallery(
                        label="参考图（点缩略图看原图）",
                        visible=False, columns=8, min_width=72,
                        object_fit="contain", allow_preview=False,
                        elem_classes=["ref-thumbs"],
                    )
                    ref_image_urls = gr.Textbox(
                        label="或粘贴参考图 URL（每行一个，可与本地图片混用；接在本地图之后编号）",
                        lines=2,
                    )
                with gr.Group(visible=False) as ref_video_group:
                    gr.Markdown("**参考视频**（参考动作 / 镜头运动 / 风格）")
                    ref_video_files = gr.File(
                        label="上传参考视频（可多个，按顺序编号为 视频1、视频2…）",
                        file_count="multiple", file_types=["video"],
                        elem_classes=["ref-uploader"],
                    )
                    ref_video_upload_btn = gr.Button(
                        "📤 上传换取临时URL", visible=False, size="sm",
                    )
                    ref_video_preview = gr.Gallery(
                        label="参考视频（点缩略图在右下角播放）",
                        visible=False, columns=6, min_width=88,
                        object_fit="contain", allow_preview=False,
                        elem_classes=["ref-thumbs"],
                    )
                    ref_video_urls = gr.Textbox(
                        label="或粘贴参考视频 URL（每行一个，可与本地视频混用；接在本地视频之后编号）",
                        lines=2,
                    )
                ref_manifest_md = gr.Markdown()
                ref_msg_md = gr.Markdown()
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

        # ---------------- 右下角悬浮预览窗 ----------------
        # 放在最外层（不嵌进任何 Group），靠 position:fixed 浮在浏览器右下角
        with gr.Column(elem_classes=["ref-dock"], visible=False) as ref_dock:
            with gr.Row(elem_classes=["ref-dock-head"]):
                ref_dock_title = gr.Markdown("")
                ref_dock_close = gr.Button(
                    "✕", size="sm", elem_classes=["ref-dock-close"],
                )
            # 高度交给 Gradio 原生参数控制（比 CSS 猜内部 DOM 可靠），
            # 超出窗口时 Gradio 会自动等比缩放到窗口内，保证完整显示
            ref_dock_image = gr.Image(
                show_label=False, interactive=False, visible=False,
                height="44vh",
                elem_classes=["ref-dock-media"],
            )
            ref_dock_video = gr.Video(
                show_label=False, interactive=False, visible=False,
                height="44vh",
                elem_classes=["ref-dock-media"],
            )

        # ---------------- 事件绑定 ----------------

        model_dd.change(
            fn=model_controls, inputs=[model_dd],
            outputs=[model_info, neg_prompt,
                     ref_image_group, ref_image_files, ref_image_upload_btn,
                     ref_video_group, ref_video_files, ref_video_upload_btn,
                     duration, resolution, cost_md, ref_msg_md],
        )
        duration.change(
            fn=lambda mid, d: cost_text(mid, d),
            inputs=[model_dd, duration], outputs=[cost_md],
        )
        # 素材编号对照：换模型或增删素材时实时刷新，用户照抄提示词里的「图1」「视频1」
        ref_inputs = [model_dd, ref_image_files, ref_image_urls,
                      ref_video_files, ref_video_urls]
        for comp in ref_inputs:
            comp.change(fn=ref_manifest_for, inputs=ref_inputs,
                        outputs=[ref_manifest_md])
        # 本地文件 → 临时URL（仅 oss 模式的模型显示这两个按钮）
        ref_image_upload_btn.click(
            fn=make_upload_handler("image"),
            inputs=[model_dd, ref_image_files, ref_image_urls],
            outputs=[ref_image_urls, ref_image_files, ref_msg_md],
        )
        ref_video_upload_btn.click(
            fn=make_upload_handler("video"),
            inputs=[model_dd, ref_video_files, ref_video_urls],
            outputs=[ref_video_urls, ref_video_files, ref_msg_md],
        )
        # 本地文件预览：选好文件就能看到画面和编号，不用猜自己传了哪几张
        ref_image_files.change(
            fn=make_preview_handler("image"), inputs=[ref_image_files],
            outputs=[ref_image_preview],
        )
        ref_video_files.change(
            fn=make_preview_handler("video"), inputs=[ref_video_files],
            outputs=[ref_video_preview],
        )
        # 点缩略图 → 右下角悬浮窗看原图 / 播放原视频
        for gallery, files_comp, kind_name in (
            (ref_image_preview, ref_image_files, "image"),
            (ref_video_preview, ref_video_files, "video"),
        ):
            gallery.select(
                fn=make_dock_handler(kind_name), inputs=[files_comp],
                outputs=[ref_dock, ref_dock_image, ref_dock_video, ref_dock_title],
            )
        ref_dock_close.click(
            fn=lambda: gr.update(visible=False), outputs=[ref_dock],
        )
        gen_btn.click(
            fn=generate,
            inputs=[model_dd, prompt, neg_prompt,
                    ref_image_files, ref_image_urls,
                    ref_video_files, ref_video_urls,
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
            outputs=[model_info, neg_prompt,
                     ref_image_group, ref_image_files, ref_image_upload_btn,
                     ref_video_group, ref_video_files, ref_video_upload_btn,
                     duration, resolution, cost_md, ref_msg_md,
                     history_df, history_state],
        )

    return demo
