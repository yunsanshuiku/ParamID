"""Independent Windows shell for the local parameter identification engine.

The server binds atomically to a loopback port chosen by Windows. Writable
experiments and UI profiles live in the current-user data directory.
"""
from __future__ import annotations
import argparse
import ctypes
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
import urllib.request

_DATA_DIR = Path(os.environ.get("PARAMID_DATA_DIR", str(
    Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ParamID"))).expanduser().resolve()
os.environ.setdefault("PARAMID_DATA_DIR", str(_DATA_DIR))
# Windowed executables and pythonw have no console streams; HTTP logging needs one.
if getattr(sys, "frozen", False) or sys.stderr is None or sys.stdout is None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stderr is None:
        sys.stderr = (_DATA_DIR / "desktop.log").open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = sys.stderr

import webview
from http.server import ThreadingHTTPServer
from app import ACQUISITION, Handler


class DesktopAPI:
    """Expose fixed native actions, without arbitrary command/filesystem access."""

    def runtime_info(self):
        """Return the experiment directory for the help dialog."""
        return {"data_dir": str(_DATA_DIR), "version": "0.7.0"}

    def open_data_folder(self):
        """Open only the application's current-user experiment directory."""
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(_DATA_DIR))
        return True


def create_server() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """Start and verify the engine before creating a window; avoid port races."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name="paramid-http", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "health", timeout=10) as response:
            if json.load(response).get("status") != "ok":
                raise RuntimeError("本机计算引擎自检失败")
    except Exception:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        raise
    return server, thread, url


def run(self_test=False, smoke_report: Path | None = None) -> int:
    """Run the native window and always release acquisition resources on exit."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    server, thread, url = create_server()
    smoke = {"status": "pending"}
    try:
        if self_test:
            print(f"ParamID desktop self-test OK: {url}", flush=True)
            return 0
        # These enable native Save dialogs for models, reports and experiment ZIPs.
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["SHOW_DEFAULT_MENUS"] = False
        window = webview.create_window(
            "ParamID · 参数辨识工作台", url, js_api=DesktopAPI(),
            width=1440, height=920, min_size=(1000, 660), resizable=True,
            confirm_close=smoke_report is None, text_select=True,
            background_color="#eff3f8")

        def verify_window():
            """Test the real WebView2 renderer against the packaged engine."""
            try:
                if not window.events.loaded.wait(45):
                    raise RuntimeError("WebView2 窗口加载超时")
                deadline = time.monotonic() + 30
                while not window.evaluate_js("document.body.classList.contains('desktop-app')"):
                    if time.monotonic() > deadline:
                        raise RuntimeError("桌面桥接初始化超时")
                    time.sleep(.1)
                window.evaluate_js("document.getElementById('openGuide').click()")
                if not window.evaluate_js("document.getElementById('quickGuide').open"):
                    raise RuntimeError("使用指南未打开")
                window.evaluate_js("document.getElementById('closeGuide').click()")
                window.evaluate_js("document.getElementById('regressionDemo').click()")
                deadline = time.monotonic() + 60
                while not window.evaluate_js("state.rows.length > 0 && !state.busy"):
                    if time.monotonic() > deadline: raise RuntimeError("示例载入超时")
                    time.sleep(.1)
                window.evaluate_js("document.getElementById('fitButton').click()")
                while not window.evaluate_js("!!state.result && !state.busy"):
                    if time.monotonic() > deadline: raise RuntimeError("计算引擎响应超时")
                    time.sleep(.1)
                metrics = window.evaluate_js("state.result.metrics")
                if metrics["fit_percent"] < 98: raise RuntimeError(f"辨识结果异常: {metrics}")
                smoke.update(status="ok", renderer=webview.renderer, metrics=metrics,
                             downloads=webview.settings["ALLOW_DOWNLOADS"],
                             title=window.title, data_dir=str(_DATA_DIR))
            except Exception as exc:
                smoke.update(status="error", error=str(exc))
                logging.exception("Desktop smoke test failed")
            finally:
                try:
                    smoke_report.parent.mkdir(parents=True, exist_ok=True)
                    smoke_report.write_text(json.dumps(smoke, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
                finally:
                    window.destroy()

        webview.start(
            func=verify_window if smoke_report else None,
            gui="edgechromium", debug=False, private_mode=False,
            storage_path=str(_DATA_DIR / "webview"),
            localization={
                "global.quitConfirmation": "确定退出 ParamID？请先导出需要保留的离线辨识结果。",
                "global.ok": "确定", "global.cancel": "取消", "global.quit": "退出",
                "global.saveFile": "保存文件", "windows.fileFilter.allFiles": "所有文件"})
        return 0 if not smoke_report or smoke["status"] == "ok" else 1
    finally:
        try:
            ACQUISITION.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def main() -> int:
    parser = argparse.ArgumentParser(description="ParamID Windows desktop application")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--smoke-test", type=Path, metavar="REPORT",
                        help="exercise the real desktop window and write a report")
    args = parser.parse_args()
    try:
        return run(args.self_test, args.smoke_test)
    except Exception as exc:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        log = _DATA_DIR / "desktop-crash.log"
        logging.basicConfig(filename=log, level=logging.ERROR, encoding="utf-8")
        logging.exception("ParamID desktop failed")
        if getattr(sys, "frozen", False) and not (args.self_test or args.smoke_test):
            ctypes.windll.user32.MessageBoxW(None,
                f"{exc}\n\n详细日志：{log}\n请确认已安装 Microsoft Edge WebView2 Runtime。",
                "ParamID 启动失败", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
