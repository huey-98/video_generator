"""按模型能力组装百炼 API 请求体（与界面解耦，便于单独验证）。

参考素材的最终结构为实测结果（2026-09-24 通过平台校验报错确认）：
    input.media = [{"type": "reference_image", "url": "https://... 或 data:image/..."}]
其中 type 的合法取值只有 reference_image / reference_video / first_frame。
"""
from __future__ import annotations

from .registry import ModelSpec


def build_payload(
    m: ModelSpec,
    *,
    prompt: str,
    negative_prompt: str = "",
    refs: list | None = None,
    duration: int = 5,
    size: str = "",
    seed: int | None = None,
    prompt_extend: bool = True,
    watermark: bool = False,
) -> tuple[dict, dict]:
    """返回 (input_payload, parameters)。refs 为地址列表（URL 或 base64 data URI）。"""
    input_payload: dict = {"prompt": prompt}

    if m.supports("negative_prompt") and (negative_prompt or "").strip():
        input_payload["negative_prompt"] = negative_prompt.strip()

    if refs:
        input_payload[m.ref_param] = [
            {"type": m.ref_item_type, "url": u} for u in refs
        ]

    parameters: dict = {m.size_param: size, "duration": int(duration)}
    if m.supports("prompt_extend"):
        parameters["prompt_extend"] = bool(prompt_extend)
    if m.supports("watermark"):
        parameters["watermark"] = bool(watermark)
    if m.supports("seed") and seed is not None and int(seed) >= 0:
        parameters["seed"] = int(seed)

    return input_payload, parameters