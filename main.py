"""AI 视频生成器 - Web UI 启动入口

用法：
    python main.py              启动并自动打开浏览器
    python main.py --no-browser 启动但不打开浏览器
    python main.py --port 7861  指定端口
"""
import sys
from pathlib import Path

# 保证可以 import src / ui（无论从哪个目录启动）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui.webui import CUSTOM_CSS, build_app


def silence_windows_proactor_noise() -> None:
    """屏蔽 Windows 上 asyncio Proactor 的无害报错（不影响功能）。

    浏览器关闭/刷新连接时，事件循环在 _call_connection_lost 里 shutdown socket
    会抛 ConnectionResetError，被 asyncio 打印成一大段 "Exception in callback" 堆栈。
    这是 Python 已知问题（cpython#109383），只是控制台噪音，这里吞掉它。
    """
    if sys.platform != "win32":
        return
    try:
        from asyncio import proactor_events

        original = proactor_events._ProactorBasePipeTransport._call_connection_lost

        def _safe_call_connection_lost(self, exc):
            try:
                return original(self, exc)
            except ConnectionResetError:
                return None

        proactor_events._ProactorBasePipeTransport._call_connection_lost = (
            _safe_call_connection_lost
        )
    except Exception:
        pass  # 内部结构变化时静默跳过，不影响启动


def main():
    silence_windows_proactor_noise()
    no_browser = "--no-browser" in sys.argv
    port = 7860
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    app = build_app()
    app.queue()  # 排队机制：生成任务耗时长，避免并发冲突
    app.launch(
        server_name="127.0.0.1",
        server_port=port,
        inbrowser=not no_browser,
        # Gradio 6 起 css 必须传给 launch()，传给 Blocks() 会被静默忽略
        css=CUSTOM_CSS,
    )


if __name__ == "__main__":
    main()
