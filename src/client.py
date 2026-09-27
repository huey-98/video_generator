"""阿里百炼视频生成 API 客户端（原生 HTTP，异步任务模式）。

调用流程：
    1. create_task()  POST /services/aigc/video-generation/video-synthesis
       （必须带 X-DashScope-Async: enable 请求头）→ 得到 task_id
    2. get_task()     GET /tasks/{task_id} 轮询，直到 task_status == SUCCEEDED
    3. download_video()  下载 output.video_url 到本地（URL 24 小时内有效）

不依赖 dashscope SDK，避免 SDK 版本对新模型的兼容问题。
"""
from __future__ import annotations

import mimetypes
import time
from pathlib import Path

import requests

# 文件上传接口硬限 1GB（模型自身还有更严的素材限制，见 config.yaml 的 refs 段）
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024


def has_oss_url(obj) -> bool:
    """递归检查请求体里有没有 oss:// 形式的临时 URL。

    含 oss:// 时必须给请求加 X-DashScope-OssResourceResolve: enable，
    否则平台不会去解析该地址（官方文档《上传文件获取临时URL》明确要求）。
    """
    if isinstance(obj, str):
        return obj.startswith("oss://")
    if isinstance(obj, dict):
        return any(has_oss_url(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(has_oss_url(v) for v in obj)
    return False


class BailianError(Exception):
    """百炼 API 调用失败"""


class BailianClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        poll_interval: int = 5,
        task_timeout: int = 900,
        download_timeout: int = 300,
        upload_timeout: int = 300,
    ):
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout
        self.download_timeout = download_timeout
        self.upload_timeout = upload_timeout
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {api_key}"})

    # ---------------- 任务接口 ----------------

    def create_task(self, model: str, input_payload: dict, parameters: dict) -> tuple[str, dict]:
        """创建视频生成任务，返回 (task_id, 原始响应)。"""
        url = f"{self.base_url}/services/aigc/video-generation/video-synthesis"
        headers = {
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",  # 必须，异步模式
        }
        # 用了上传得到的 oss:// 临时URL 时，必须显式开启资源解析
        if has_oss_url(input_payload):
            headers["X-DashScope-OssResourceResolve"] = "enable"
        body = {"model": model, "input": input_payload, "parameters": parameters}
        try:
            resp = self._session.post(url, json=body, headers=headers, timeout=60)
        except requests.RequestException as e:
            raise BailianError(f"网络错误：{e}") from e
        data = self._parse(resp)
        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise BailianError(f"响应中没有 task_id：{str(data)[:300]}")
        return task_id, data

    def get_task(self, task_id: str) -> dict:
        """查询任务状态，返回原始响应。"""
        try:
            resp = self._session.get(f"{self.base_url}/tasks/{task_id}", timeout=60)
        except requests.RequestException as e:
            raise BailianError(f"网络错误：{e}") from e
        return self._parse(resp)

    @staticmethod
    def task_status(task_data: dict) -> str:
        return (task_data.get("output") or {}).get("task_status", "UNKNOWN")

    @staticmethod
    def extract_video_url(task_data: dict) -> str:
        """从成功的任务响应中提取视频地址（兼容不同的返回结构）。"""
        output = task_data.get("output") or {}
        url = output.get("video_url")
        if not url:
            results = output.get("results")
            if isinstance(results, list) and results:
                url = (results[0] or {}).get("video_url")
        if not url:
            raise BailianError("任务成功但响应中没有 video_url")
        return url

    def download_video(self, url: str, dest: Path) -> Path:
        """下载视频到本地。下载地址是 CDN 链接，不带鉴权头。"""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with requests.get(url, stream=True, timeout=self.download_timeout) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
        except requests.RequestException as e:
            raise BailianError(f"视频下载失败：{e}") from e
        return dest

    # ---------------- 文件上传（本地文件 → 临时URL） ----------------
    #
    # 百炼提供免费临时存储空间：上传本地文件后拿到 oss:// 形式的临时 URL。
    # 当前所有参考素材模型统一走此路径（wan3.0 系列不收 base64 必须用；
    # wan2.7-r2v 官方文档确认 media.url 同时支持 公网URL/oss临时URL/base64）。
    # 文档：https://help.aliyun.com/zh/model-studio/get-temporary-file-url

    def get_upload_policy(self, model: str) -> dict:
        """获取文件上传凭证（免费接口，凭证本身 300 秒内有效）。"""
        try:
            resp = self._session.get(
                f"{self.base_url}/uploads",
                params={"action": "getPolicy", "model": model},
                timeout=30,
            )
        except requests.RequestException as e:
            raise BailianError(f"获取上传凭证失败（网络错误）：{e}") from e
        policy = self._parse(resp).get("data") or {}
        if not policy.get("upload_host") or not policy.get("upload_dir"):
            raise BailianError(f"上传凭证响应缺少必要字段：{str(policy)[:300]}")
        return policy

    def upload_file(self, path, model: str, mime: str = "") -> str:
        """上传本地文件到百炼临时空间，返回 oss:// 形式的临时 URL。

        注意三条平台规则：
          - URL **48 小时**后失效，需在有效期内完成生成
          - 上传时指定的模型必须与后续调用模型**一致**，不能跨模型共用
          - 必须与调用方是同一主账号的 API Key
        每次调用都重新取凭证（upload_dir 带新 UUID），因此同名文件不会互相覆盖。
        """
        p = Path(path)
        size = p.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise BailianError(
                f"文件 {p.name} 为 {size / 1048576:.1f}MB，"
                f"超过上传接口上限 {MAX_UPLOAD_BYTES // 1048576}MB"
            )
        policy = self.get_upload_policy(model)
        object_key = f"{policy['upload_dir']}/{p.name}"
        content_type = mime or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        try:
            with open(p, "rb") as f:
                resp = requests.post(
                    policy["upload_host"],
                    files={
                        "key": (None, object_key),
                        "policy": (None, policy["policy"]),
                        "OSSAccessKeyId": (None, policy["oss_access_key_id"]),
                        "signature": (None, policy["signature"]),
                        "success_action_status": (None, "200"),
                        "x-oss-object-acl": (None, policy.get("x_oss_object_acl", "private")),
                        "x-oss-forbid-overwrite": (
                            None, str(policy.get("x_oss_forbid_overwrite", True)).lower()
                        ),
                        "file": (p.name, f, content_type),
                    },
                    timeout=self.upload_timeout,
                )
        except requests.RequestException as e:
            raise BailianError(f"上传 {p.name} 失败（网络错误）：{e}") from e
        # OSS PostObject 成功时返回 200 + 空响应体
        if resp.status_code != 200:
            raise BailianError(f"上传 {p.name} 失败 HTTP {resp.status_code}：{resp.text[:200]}")
        return f"oss://{object_key}"

    # ---------------- 内部 ----------------

    @staticmethod
    def _parse(resp: requests.Response) -> dict:
        try:
            data = resp.json()
        except ValueError:
            raise BailianError(f"API 返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")
        if resp.status_code != 200:
            code = data.get("code", "")
            msg = data.get("message", "")
            raise BailianError(f"HTTP {resp.status_code} {code}: {msg}")
        return data
