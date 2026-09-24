"""Local HTTP API and command-line entry point for ParamID."""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from paramid.acquisition import AcquisitionManager, serial_ports
from paramid.core import IdentificationError
from paramid.data import detect_channels, parse_table
from paramid.decoder import preview_decode
from paramid.service import SessionStore, offline

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("PARAMID_DATA_DIR", str(ROOT / "outputs"))).expanduser().resolve()
STORE = SessionStore()
FIT_LOCK = threading.Lock()
ACQUISITION = AcquisitionManager(DATA_ROOT / "acquisitions")
PICKER_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    """Serve the local UI and JSON API; only loopback origins are accepted."""

    def log_message(self, format, *args):
        # Keep the normal HTTP log useful in desktop crash reports.
        super().log_message(format, *args)

    def send(self, status, content, mime="application/json; charset=utf-8"):
        body = content if isinstance(content, bytes) else json.dumps(
            content, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        hosts = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in hosts and (
            not origin or origin in {"http://" + host for host in hosts}
        )

    def do_GET(self):
        if not self.allowed():
            return self.send(403, {"error": "\u4ec5\u5141\u8bb8\u672c\u673a\u5e94\u7528\u8bf7\u6c42\u3002"})
        url = urlsplit(self.path)
        try:
            if url.path == "/health":
                return self.send(200, {"status": "ok", "version": "0.7.0"})
            if url.path == "/api/acquisition/ports":
                return self.send(200, serial_ports())
            if url.path == "/api/acquisition/templates":
                return self.send(200, ACQUISITION.templates())
            if url.path == "/api/acquisition":
                return self.send(200, ACQUISITION.active())
            if url.path == "/api/acquisition/download":
                key = parse_qs(url.query).get("id", [""])[0]
                path = ACQUISITION.download(key)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="experiment-{key[:8]}.zip"',
                )
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with path.open("rb") as file:
                    while chunk := file.read(65536):
                        self.wfile.write(chunk)
                return
            if url.path == "/api/demo":
                kind = parse_qs(url.query).get("kind", ["regression"])[0]
                if kind not in ("regression", "arx", "multichannel", "frequency"):
                    raise IdentificationError("\u672a\u77e5\u793a\u4f8b\u3002")
                return self.send(
                    200,
                    {
                        "csv": (ROOT / "examples" / f"{kind}.csv").read_text(encoding="utf-8"),
                        "config": json.loads(
                            (ROOT / "examples" / f"{kind}.json").read_text(encoding="utf-8")
                        ),
                    },
                )
            if url.path == "/api/session":
                key = parse_qs(url.query).get("id", [""])[0]
                return self.send(200, {"session_id": key, **STORE.get(key).snapshot()})
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/acquisition.js": ("acquisition.js", "application/javascript; charset=utf-8"),
                "/acquisition.css": ("acquisition.css", "text/css; charset=utf-8"),
                "/model.js": ("model.js", "application/javascript; charset=utf-8"),
                "/model.css": ("model.css", "text/css; charset=utf-8"),
                "/order.css": ("order.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                "/multichannel.js": ("multichannel.js", "application/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
                "/workbench.css": ("workbench.css", "text/css; charset=utf-8"),
                "/workbench.js": ("workbench.js", "application/javascript; charset=utf-8"),
            }
            if url.path not in assets:
                return self.send(404, {"error": "\u8d44\u6e90\u4e0d\u5b58\u5728\u3002"})
            name, mime = assets[url.path]
            self.send(200, (ROOT / "web" / name).read_bytes(), mime)
        except IdentificationError as exc:
            self.send(400, {"error": str(exc)})

    def do_POST(self):
        if not self.allowed():
            return self.send(403, {"error": "\u8bf7\u6c42\u6765\u6e90\u4e0d\u5c5e\u4e8e\u672c\u673a\u5e94\u7528\u3002"})
        acquired = False
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 26 * 1024 * 1024:
                raise IdentificationError("\u8bf7\u6c42\u987b\u5728 26MB \u4ee5\u5185\u3002")
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise IdentificationError("\u8bf7\u6c42\u987b\u4f7f\u7528 application/json\u3002")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise IdentificationError("\u8bf7\u6c42\u5fc5\u987b\u4e3a JSON \u5bf9\u8c61\u3002")
            path = urlsplit(self.path).path
            if path == "/api/acquisition/decode-preview":
                return self.send(200, preview_decode(payload))
            if path == "/api/acquisition/connect":
                return self.send(200, ACQUISITION.connect(payload))
            if path == "/api/acquisition/start":
                return self.send(200, ACQUISITION.get(payload.get("id")).start(payload.get("channels", {})))
            if path == "/api/acquisition/stop":
                return self.send(200, ACQUISITION.get(payload.get("id")).stop())
            if path == "/api/acquisition/template":
                return self.send(200, ACQUISITION.save_template(payload))
            if path == "/api/acquisition/pick-file":
                if not PICKER_LOCK.acquire(blocking=False):
                    raise IdentificationError("\u6587\u4ef6\u9009\u62e9\u7a97\u53e3\u5df2\u6253\u5f00\uff0c\u8bf7\u5728\u684c\u9762\u5b8c\u6210\u9009\u62e9\u3002")
                try:
                    import tkinter as tk
                    from tkinter import filedialog
                    window = tk.Tk()
                    window.withdraw()
                    window.attributes("-topmost", True)
                    try:
                        selected = filedialog.askopenfilename(
                            parent=window,
                            title="\u9009\u62e9\u6b63\u5728\u8ffd\u52a0\u91c7\u6837\u7684 CSV \u6587\u4ef6",
                            filetypes=[("\u91c7\u6837\u6570\u636e", "*.csv *.tsv *.txt")],
                        )
                    finally:
                        window.destroy()
                    return self.send(200, {"path": selected})
                finally:
                    PICKER_LOCK.release()
            if path == "/api/inspect":
                table = parse_table(payload.get("csv", ""), payload.get("header_mode", "auto"))
                return self.send(200, {**table, "channels": detect_channels(table["columns"], table["rows"])})
            if path == "/api/offline":
                acquired = FIT_LOCK.acquire(blocking=False)
                if not acquired:
                    return self.send(409, {"error": "\u5df2\u6709\u79bb\u7ebf\u4efb\u52a1\u6b63\u5728\u8ba1\u7b97\u3002"})
                return self.send(200, offline(payload))
            if path == "/api/session/create":
                return self.send(200, STORE.create(payload))
            if path == "/api/session/push":
                key = payload.get("session_id", "")
                result = STORE.get(key).push(payload.get("samples"), payload.get("sequence"))
                return self.send(200, {"session_id": key, **result})
            if path == "/api/session/delete":
                STORE.delete(payload.get("session_id", ""))
                return self.send(200, {"deleted": True})
            return self.send(404, {"error": "\u63a5\u53e3\u4e0d\u5b58\u5728\u3002"})
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
            self.send(400, {"error": str(exc)})
        except IdentificationError as exc:
            self.send(400, {"error": str(exc)})
        except Exception:
            import traceback
            traceback.print_exc()
            self.send(500, {"error": "\u8ba1\u7b97\u5931\u8d25\uff0c\u8bf7\u67e5\u770b\u670d\u52a1\u65e5\u5fd7\u3002"})
        finally:
            if acquired:
                FIT_LOCK.release()


def main():
    parser = argparse.ArgumentParser(description="ParamID 0.7.0")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--capture-dir", type=Path, default=None)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--validation", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "offline")
    args = parser.parse_args()
    if args.capture_dir is not None:
        ACQUISITION.root = args.capture_dir.resolve()
    if args.csv:
        if not args.config:
            parser.error("\u79bb\u7ebf\u547d\u4ee4\u884c\u6a21\u5f0f\u9700\u8981 --config\u3002")
        payload = json.loads(args.config.read_text(encoding="utf-8-sig"))
        payload["csv"] = args.csv.read_text(encoding="utf-8-sig")
        if args.validation:
            payload["validation_csv"] = args.validation.read_text(encoding="utf-8-sig")
        result = offline(payload)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "validation.csv").write_text(result.pop("csv_export"), encoding="utf-8-sig")
        (args.output / "report.md").write_text(result.pop("report"), encoding="utf-8")
        (args.output / "model.json").write_text(
            json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result.get("metrics") or {}, indent=2))
        return
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(f"ParamID: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ACQUISITION.close()
        server.server_close()


if __name__ == "__main__":
    main()

