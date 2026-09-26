"""模型注册表：把 config.yaml 中的模型定义解析为 ModelSpec。

每个模型的能力差异（时长范围、分辨率参数名、支持哪些参考素材、
支持的额外参数）都集中在这里描述，界面和客户端据此动态适配。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# media 元素 type 的合法取值（2026-09-24 实测：只有这三个）
ITEM_TYPES = {
    "image": "reference_image",
    "video": "reference_video",
    "first_frame": "first_frame",
}


@dataclass
class RefKind:
    """一类参考素材（参考图 或 参考视频）的配置。"""

    enabled: bool = False
    max: int = 0
    item_type: str = ""           # media 元素的 type 取值
    # 本地文件怎么变成平台能用的地址：
    #   base64 = 直接内联（data:...;base64,...），体积会放大 1/3
    #   oss    = 先上传到百炼临时空间换成 oss:// 临时URL（48 小时有效），平台不收 base64 时用
    upload_mode: str = "base64"

    @classmethod
    def from_dict(cls, d: dict, kind: str) -> "RefKind":
        # 兼容旧字段名 url_scheme（any → base64；http → oss）
        legacy = {"any": "base64", "http": "oss"}.get(d.get("url_scheme", ""), "")
        return cls(
            enabled=bool(d.get("enabled", False)),
            max=int(d.get("max", 0)),
            item_type=d.get("item_type") or ITEM_TYPES.get(kind, ""),
            upload_mode=d.get("upload_mode") or legacy or "base64",
        )


@dataclass
class RefSpec:
    """模型的全部参考素材配置（对应 config.yaml 里的 refs 段）。"""

    enabled: bool = False
    param: str = "media"          # API 字段名（实测为 media）
    min_total: int = 0            # 至少要几个素材
    max_total: int = 0            # 数组长度上限；0 = 不限
    image: RefKind = field(default_factory=RefKind)
    video: RefKind = field(default_factory=RefKind)

    def kind(self, name: str) -> RefKind:
        return self.image if name == "image" else self.video

    def kinds(self) -> list:
        """返回 [(类别名, RefKind), ...]，顺序即 media 数组中的排列顺序。"""
        return [("image", self.image), ("video", self.video)]

    @property
    def local_upload_kinds(self) -> list:
        """支持本地上传的素材类别（两种模式都算：base64 内联或上传换临时URL）。"""
        return [n for n, k in self.kinds() if k.enabled]

    @classmethod
    def from_dict(cls, d: dict) -> "RefSpec":
        return cls(
            enabled=bool(d.get("enabled", False)),
            param=d.get("param", "media"),
            min_total=int(d.get("min_total", 0)),
            max_total=int(d.get("max_total", 0)),
            image=RefKind.from_dict(d.get("image") or {}, "image"),
            video=RefKind.from_dict(d.get("video") or {}, "video"),
        )


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
    refs: RefSpec = field(default_factory=RefSpec)
    extra_params: list = field(default_factory=list)
    price_per_second: float | None = None     # 每秒单价（元），None 表示未配置

    # ---- 便捷访问（界面/请求体组装常用） ----

    @property
    def ref_enabled(self) -> bool:
        return self.refs.enabled

    @property
    def ref_param(self) -> str:
        return self.refs.param

    @property
    def image_max(self) -> int:
        return self.refs.image.max if self.refs.image.enabled else 0

    @property
    def video_max(self) -> int:
        return self.refs.video.max if self.refs.video.enabled else 0

    @property
    def ref_total_max(self) -> int:
        """素材总数上限：取 max_total，未配置时退化为两类上限之和。"""
        if self.refs.max_total:
            return self.refs.max_total
        return self.image_max + self.video_max

    def supports(self, param: str) -> bool:
        return param in self.extra_params

    @classmethod
    def from_dict(cls, d: dict) -> "ModelSpec":
        duration = d.get("duration") or {}
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            description=d.get("description", ""),
            duration_min=int(duration.get("min", 2)),
            duration_max=int(duration.get("max", 15)),
            duration_default=int(duration.get("default", 5)),
            size_param=d.get("size_param", "resolution"),
            size_options=[(item[0], item[1]) for item in (d.get("size_options") or [])],
            refs=RefSpec.from_dict(d.get("refs") or {}),
            extra_params=list(d.get("extra_params") or []),
            price_per_second=d.get("price_per_second"),
        )