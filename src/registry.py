"""模型注册表：把 config.yaml 中的模型定义解析为 ModelSpec。

每个模型的能力差异（时长范围、分辨率参数名、是否需要参考图、
支持的额外参数）都集中在这里描述，界面和客户端据此动态适配。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModelSpec:
    id: str
    name: str
    description: str = ""
    duration_min: int = 2
    duration_max: int = 15
    duration_default: int = 5
    size_param: str = "resolution"            # 分辨率参数名：size 或 resolution
    size_options: list = field(default_factory=list)  # [(显示名, API值)]
    ref_enabled: bool = False                 # 是否支持参考图
    ref_required: bool = False                # 参考图是否必传
    ref_max: int = 0                          # 参考图最大张数
    ref_param: str = "media"                  # 参考图在 API 中的字段名
    ref_item_type: str = "reference_image"    # media 元素的 type 取值
    ref_url_scheme: str = "any"               # any=支持本地上传(base64)；http=只接受 http(s) 地址
    extra_params: list = field(default_factory=list)
    price_per_second: float | None = None     # 每秒单价（元），None 表示未配置

    def supports(self, param: str) -> bool:
        return param in self.extra_params

    @classmethod
    def from_dict(cls, d: dict) -> "ModelSpec":
        duration = d.get("duration") or {}
        ref = d.get("ref_images") or {}
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            description=d.get("description", ""),
            duration_min=int(duration.get("min", 2)),
            duration_max=int(duration.get("max", 15)),
            duration_default=int(duration.get("default", 5)),
            size_param=d.get("size_param", "resolution"),
            size_options=[(item[0], item[1]) for item in (d.get("size_options") or [])],
            ref_enabled=bool(ref.get("enabled", False)),
            ref_required=bool(ref.get("required", False)),
            ref_max=int(ref.get("max", 0)),
            ref_param=ref.get("param", "media"),
            ref_item_type=ref.get("item_type", "reference_image"),
            ref_url_scheme=ref.get("url_scheme", "any"),
            extra_params=list(d.get("extra_params") or []),
            price_per_second=d.get("price_per_second"),
        )
