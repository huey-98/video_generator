"""阿里百炼视频生成 API 客户端（原生 HTTP，异步任务模式）。

调用流程：
    1. create_task()  POST /services/aigc/video-generation/video-synthesis
       （必须带 X-DashScope-Async: enable 请求头）→ 得到 task_id
    2. get_task()     GET /tasks/{task_id} 轮询，直到 task_status == SUCCEEDED
    3. download_video()  下载 output.video_url 到本地（URL 24 小时内有效）

不依赖 dashscope SDK，避免 SDK 版本对新模型的兼容问题。
"""
from __future__ import annotations

import time
from pathlib import Path

import requests


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
    ):
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout
        self.download_timeout = download_timeout
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
