"""Local acquisition adapters and an ordered, bounded acquisition pipeline.

The producer only receives and validates samples. A separate worker journals
every accepted sample before calling the estimator, so order selection and UI
polling cannot interrupt device reads. Sampling time always comes from device
timestamps or a user-confirmed fixed period, never PC packet arrival times.
"""

from collections import deque
import csv
import json
from pathlib import Path
import queue
import random
import re
import stat
import threading
import time
import uuid
import zipfile

from .core import IdentificationError, finite_number, integer
from .service import create_online_session, reject_removed_filter
from .representation import model_report
from .decoder import BinaryStream, decoder_config
from .frequency import frequency_config


class CSVStream:
    """Decode complete CSV lines, retaining at most 8 KiB of a partial frame.

    Quoted multiline fields are deliberately unsupported by this numeric device
    protocol. Repeated headers are rejected: they often indicate a device reset.
    """

    def __init__(self, encoding="utf-8-sig", header=""):
        if encoding not in ("utf-8-sig", "gb18030"):
            raise IdentificationError("编码请选择 UTF-8 或 GB18030。")
        self.encoding, self.pending, self.columns = encoding, b"", []
        self.delimiter = ","
        if header:
            self._header(header)

    def _header(self, line):
        self.delimiter = max((",", ";", "\t"), key=line.count)
        columns = next(csv.reader([line], delimiter=self.delimiter, strict=True))
        columns = [c.strip().lstrip("\ufeff") for c in columns]
        if not 2 <= len(columns) <= 128 or any(not c or len(c) > 100 for c in columns) or len(set(columns)) != len(columns):
            raise IdentificationError("需要至少两列且不重复的 CSV 表头，例如 time_s,input_v,output_rad_s。")
        # Numeric first lines are data, not field names; never silently lose them.
        try:
            [float(c) for c in columns]
        except ValueError:
            self.columns = columns
            return
        raise IdentificationError("未收到表头；请让设备发送列名，或在连接设置中填写表头。")

    def feed(self, chunk):
        """Yield in order so valid lines preceding a malformed line are retained."""
        self.pending += chunk
        while b"\n" in self.pending:
            raw, self.pending = self.pending.split(b"\n", 1)
            if len(raw) > 8192:
                raise IdentificationError("单行数据超过 8 KiB，请检查设备数据格式。")
            try:
                line = raw.rstrip(b"\r").decode(self.encoding).strip()
                if not line:
                    continue
                if not self.columns:
                    self._header(line)
                    continue
                cells = next(csv.reader([line], delimiter=self.delimiter, strict=True))
                if [c.strip() for c in cells] == self.columns:
                    raise IdentificationError("再次收到表头，设备可能已重启；请重新连接开始新实验。")
                if len(cells) != len(self.columns):
                    raise IdentificationError("数据列数与表头不一致；请检查设备模板。")
                yield {name: finite_number(value, name) for name, value in zip(self.columns, cells)}
            except (UnicodeError, csv.Error) as exc:
                raise IdentificationError("无法解读采样行，请检查编码、分隔符和设备协议。") from exc
        if len(self.pending) > 8192:
            raise IdentificationError("数据长时间没有换行，或单行超过 8 KiB。")


class FileFollower:
    """Read only appended complete lines and detect truncation/replacement.

    A small trailing preview is read on connect; historical rows are not fitted.
    A byte anchor detects in-place rewrites even if the file regrew past our cursor.
    """

    def __init__(self, path, encoding):
        path = Path(path).expanduser().resolve(strict=True)
        if not stat.S_ISREG(path.stat().st_mode) or path.suffix.lower() not in (".csv", ".tsv", ".txt"):
            raise IdentificationError("请选择本地 CSV、TSV 或 TXT 普通文件。")
        self.path, self.parser = path, CSVStream(encoding)
        self.file = path.open("rb", buffering=0)
        try:
            first = self.file.readline(8194)
            if not first.endswith(b"\n"):
                raise IdentificationError("文件需要一行完整表头，再开始跟随。")
            list(self.parser.feed(first))
            header_end = self.file.tell()
            size = self.file.seek(0, 2)
            begin = max(header_end, size - 65536)
            self.file.seek(begin)
            if begin > header_end:
                self.file.readline()  # discard the fragment at the preview boundary
            self.initial = self.file.read()
            self.position = self.file.tell()
            self.identity = (path.stat().st_dev, path.stat().st_ino)
            self.anchor = self._anchor()
        except Exception:
            self.file.close()
            raise

    def _anchor(self):
        self.file.seek(max(0, self.position - 64))
        value = self.file.read(self.position - self.file.tell())
        self.file.seek(self.position)
        return value

    def read(self):
        info = self.path.stat()
        if (info.st_dev, info.st_ino) != self.identity or info.st_size < self.position or self._anchor() != self.anchor:
            raise IdentificationError("采样文件被截断、覆盖或重新创建；已结束本次实验，请重新连接。")
        chunk = self.file.read(4096)
        self.position = self.file.tell()
        self.anchor = self._anchor()
        return chunk

    def close(self):
        self.file.close()


def serial_ports():
    try:
        from serial.tools import list_ports
        return [{"port": p.device, "label": f"{p.device} · {p.description}"} for p in list_ports.comports()]
    except ImportError as exc:
        raise IdentificationError("串口组件未安装，请安装 requirements.txt 中的 pyserial。") from exc


def source_config(config):
    """Whitelist local read-only adapters; no arbitrary serial URLs or commands."""
    kind = config.get("kind", "demo")
    if kind not in ("demo", "serial", "file"):
        raise IdentificationError("请选择仿真、USB 串口或跟随 CSV。")
    result = {"kind": kind, "encoding": config.get("encoding", "utf-8-sig")}
    if result["encoding"] not in ("utf-8-sig", "gb18030"):
        raise IdentificationError("不支持的字符编码。")
    if kind == "serial":
        port = config.get("port", "")
        if not isinstance(port, str) or not re.fullmatch(r"COM[1-9][0-9]{0,3}", port, re.I):
            raise IdentificationError("请选择有效的 COM 串口。")
        result.update(port=port, baudrate=integer(config.get("baudrate", 115200), "波特率", 300, 3000000),
                      header=str(config.get("header", ""))[:8192], parity=config.get("parity", "N"),
                      stopbits=finite_number(config.get("stopbits", 1), "停止位"),
                      bytesize=integer(config.get("bytesize", 8), "数据位", 5, 8))
        result["format"] = config.get("format", "csv")
        if result["format"] not in ("csv", "binary"):
            raise IdentificationError("请选择文本 CSV 或二进制接收格式。")
        if result["format"] == "binary":
            if result["bytesize"] != 8:
                raise IdentificationError("二进制接收需要 8 个串口数据位；每个 double 由 8 个字节组成。")
            result["decoder"] = decoder_config(config.get("decoder", {}))
        if result["parity"] not in ("N", "E", "O") or result["stopbits"] not in (1, 1.5, 2):
            raise IdentificationError("不支持的串口校验或停止位设置。")
    if kind == "file":
        if not isinstance(config.get("path"), str) or not config["path"].strip():
            raise IdentificationError("请先选择正在写入的采样文件。")
        result["path"] = config["path"]
    return result


def channel_config(config, columns, preview):
    """Resolve physical scaling and check uniform device timestamps before fitting."""
    result = {key: str(config.get(key, ""))[:100] for key in
              ("input", "output", "time", "input_name", "output_name", "input_unit", "output_unit")}
    if result["input"] not in columns or result["output"] not in columns or result["input"] == result["output"]:
        raise IdentificationError("请分别选择输入信号和输出信号，且不能是同一列。")
    if result["time"] and (result["time"] not in columns or result["time"] in (result["input"], result["output"])):
        raise IdentificationError("时间列必须与输入、输出不同。")
    for key in ("input_scale", "output_scale", "time_scale"):
        result[key] = finite_number(config.get(key, 1), key)
        if result[key] == 0 or (key == "time_scale" and result[key] < 0):
            raise IdentificationError("通道倍率不能为零，时间倍率必须为正。")
    dt = config.get("sample_time")
    if result["time"] and config.get("auto_time", True):
        times = [row[result["time"]] * result["time_scale"] for row in preview]
        if len(times) < 3:
            raise IdentificationError("还需要至少 3 个时间戳才能自动识别采样周期，请稍候。")
        deltas = [b - a for a, b in zip(times, times[1:])]
        dt = sorted(deltas)[len(deltas) // 2]
        if dt <= 0 or any(abs(d - dt) > .01 * dt + 1e-12 for d in deltas):
            raise IdentificationError("时间戳不连续或采样间隔变化超过 1%，请检查采样设备、时间单位和丢包。")
    result["sample_time"] = finite_number(dt, "采样周期")
    if result["sample_time"] <= 0:
        raise IdentificationError("没有时间列时，请填写设备实际的固定采样周期（秒）。")
    result["auto_time"] = bool(config.get("auto_time", True))
    reject_removed_filter(config)
    result["algorithm"] = config.get("algorithm", "rls")
    if result["algorithm"] not in ("rls", "frequency"):
        raise IdentificationError("在线算法须为 rls 或 frequency。")
    if result["algorithm"] == "frequency":
        result["frequency"] = frequency_config(config.get("frequency"))
    return result


class Acquisition:
    """One connection and one experiment with a loss-intolerant bounded queue.

    On a source fault, stop receiving and drain all previously accepted rows.
    On a fitting fault, continue journaling queued rows but stop fitting. Counts
    distinguish received preview rows, recorded experiment rows and fitted rows.
    Stopping never splices discontinuous experiments into a single ARX series.
    """

    def __init__(self, config, root, queue_limit=8192, sample_limit=1000000):
        self.id, self.source = uuid.uuid4().hex, source_config(config)
        self.root, self.directory = Path(root), Path(root) / self.id
        self.lock = threading.RLock()
        self.stop_event, self.producer_done, self.done = threading.Event(), threading.Event(), threading.Event()
        self.queue = queue.Queue(maxsize=queue_limit)
        self.preview = deque(maxlen=120)
        self.columns, self.error, self.rejected = [], "", None
        self.received = self.accepted = self.recorded = self.fitted = 0
        self.session = self.result = self.channels = None
        self.last_sample_at = self.last_timestamp = None
        self.sample_limit, self.phase = sample_limit, "preview"
        self.files, self.archive = [], None
        self.adapter = None
        try:
            if self.source["kind"] == "file":
                self.adapter = FileFollower(self.source["path"], self.source["encoding"])
                self.parser = self.adapter.parser
                self.columns = list(self.parser.columns)
                for row in self.parser.feed(self.adapter.initial):
                    self._receive(row)
            elif self.source["kind"] == "serial":
                import serial
                self.parser = (BinaryStream(self.source["decoder"]) if self.source["format"] == "binary"
                               else CSVStream(self.source["encoding"], self.source["header"]))
                self.columns = list(self.parser.columns)
                self.adapter = serial.Serial(port=self.source["port"], baudrate=self.source["baudrate"],
                                             bytesize=self.source["bytesize"], parity=self.source["parity"],
                                             stopbits=self.source["stopbits"], timeout=.1)
            else:
                self.columns = ["time_s", "input_v", "output_rad_s"]
        except Exception as exc:
            if self.adapter:
                self.adapter.close()
            if isinstance(exc, IdentificationError):
                raise
            if self.source["kind"] == "serial":
                raise IdentificationError(f"串口打开失败：{exc}。请核对端口、驱动及占用情况。") from exc
            raise IdentificationError(f"采样文件打开失败：{exc}") from exc
        self.worker = threading.Thread(target=self._work, daemon=True, name="paramid-estimate")
        self.producer = threading.Thread(target=self._produce, daemon=True, name="paramid-receive")

    def connect(self):
        self.worker.start()
        self.producer.start()

    def fail(self, message):
        with self.lock:
            if not self.error:
                self.error = str(message)
            self.phase = "stopping"
        self.stop_event.set()

    def _receive(self, row):
        with self.lock:
            self.received += 1
            self.preview.append(row)
            self.last_sample_at = time.monotonic()
            if self.session is None or self.stop_event.is_set():
                return
            try:
                if self.accepted >= self.sample_limit:
                    raise IdentificationError("本次实验已达到 1,000,000 点上限，请保存后开始新实验。")
                c = self.channels
                timestamp = row[c["time"]] * c["time_scale"] if c["time"] else self.accepted * c["sample_time"]
                sample = {"time_s": finite_number(timestamp, "时间"),
                          "u": finite_number(row[c["input"]] * c["input_scale"], "输入"),
                          "y": finite_number(row[c["output"]] * c["output_scale"], "输出")}
                if self.last_timestamp is not None and abs(timestamp - self.last_timestamp - c["sample_time"]) > .01 * c["sample_time"] + 1e-12:
                    raise IdentificationError("采样时间中断、回退或间隔偏差超过 1%；已结束本次实验，请检查设备。")
                self.queue.put_nowait((row, sample))
                self.accepted += 1
                self.last_timestamp = timestamp
            except queue.Full:
                self.rejected = row
                self.fail("处理队列已满，采样速度超过当前处理能力；已停止接收并保存已接受的数据。")
            except (ValueError, KeyError) as exc:
                self.rejected = row
                self.fail(str(exc))

    def _produce(self):
        try:
            if self.source["kind"] == "demo":
                rng, y, old_u, k = random.Random(24), 0., 0., 0
                while not self.stop_event.is_set():
                    for _ in range(10):
                        if self.stop_event.is_set():
                            break
                        u = rng.uniform(-2, 2)
                        y = .82 * y + .35 * old_u + .04 + rng.gauss(0, .005)
                        self._receive({"time_s": k * .01, "input_v": u, "output_rad_s": y})
                        old_u, k = u, k + 1
                    self.stop_event.wait(.1)
            else:
                while not self.stop_event.is_set():
                    chunk = self.adapter.read() if self.source["kind"] == "file" else self.adapter.read(min(4096, max(1, self.adapter.in_waiting)))
                    for row in self.parser.feed(chunk):
                        self._receive(row)
                        if self.stop_event.is_set():
                            break
                    with self.lock:
                        self.columns = list(self.parser.columns)
                    if not chunk:
                        self.stop_event.wait(.05)
        except Exception as exc:
            self.fail(f"接收已停止：{exc}")
        finally:
            try:
                if self.adapter:
                    self.adapter.close()
            finally:
                self.producer_done.set()

    def start(self, config):
        with self.lock:
            if self.session is not None or self.stop_event.is_set():
                raise IdentificationError("本次连接已建模或已结束，请重新连接以开始新实验。")
            if not self.preview:
                raise IdentificationError("尚未收到采样，请先检查设备是否开始发送。")
            channels = channel_config(config, self.columns, list(self.preview))
            session = create_online_session({"model": {"kind": "arx", "input": "u", "output": "y", "time": "time_s",
                                               "sample_time": channels["sample_time"]}, "order_mode": "auto",
                                     "algorithm": channels["algorithm"], "frequency": channels.get("frequency")})
            self.directory.mkdir(parents=True)
            try:
                for name in ("raw_samples.csv", "samples.csv", "predictions.csv"):
                    self.files.append((self.directory / name).open("w", encoding="utf-8-sig", newline=""))
                self.raw_writer, self.sample_writer, self.prediction_writer = [csv.writer(f) for f in self.files]
                self.raw_writer.writerow(self.columns)
                self.sample_writer.writerow(["time_s", "u", "y"])
                self.prediction_writer.writerow(["time_s", "measured", "prediction_before_update", "residual"])
                self.channels = channels
                self._json("metadata.json", {"source": self.source, "channels": channels, "status": "recording"})
            except Exception:
                for f in self.files:
                    f.close()
                raise
            self.session, self.result, self.phase = session, session.snapshot(), "recording"
        return self.snapshot()

    def _json(self, name, value):
        (self.directory / name).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")

    def _work(self):
        estimator_failed = False
        try:
            while not self.producer_done.is_set() or not self.queue.empty():
                try:
                    batch = [self.queue.get(timeout=.1)]
                except queue.Empty:
                    continue
                # Give low-rate reads a short batching opportunity; bounds stay fixed.
                deadline = time.monotonic() + .05
                while len(batch) < 128 and time.monotonic() < deadline:
                    try:
                        batch.append(self.queue.get(timeout=.005))
                    except queue.Empty:
                        if self.producer_done.is_set():
                            break
                for raw, sample in batch:
                    self.raw_writer.writerow([raw[c] for c in self.columns])
                    self.sample_writer.writerow([sample[c] for c in ("time_s", "u", "y")])
                    with self.lock:
                        self.recorded += 1
                for f in self.files:
                    f.flush()
                if not estimator_failed:
                    try:
                        update = self.session.push([sample for _, sample in batch], self.fitted)
                        for row in update["series"]:
                            self.prediction_writer.writerow([row[c] for c in ("time", "measured", "prediction", "residual")])
                        snapshot = self.session.snapshot()
                        snapshot["acquisition_channels"] = self.channels
                        with self.lock:
                            self.fitted = snapshot["next_sequence"]
                            self.result = snapshot
                    except Exception as exc:
                        estimator_failed = True
                        self.fail(f"模型计算停止，采样记录仍保留：{exc}")
                if sum(f.tell() for f in self.files) > 512 * 1024 * 1024:
                    self.fail("本次实验记录已达 512 MiB，请保存后开始新实验。")
        except Exception as exc:
            self.fail(f"采样记录写入失败：{exc}。请检查磁盘空间；部分已接受样本可能未保存。")
        finally:
            try:
                if self.session:
                    for f in self.files:
                        f.close()
                    self._finalize()
            except Exception as exc:
                self.fail(f"保存实验失败：{exc}")
                # Even after another error the saving failure must be visible.
                with self.lock:
                    self.error += f"；保存实验失败：{exc}"
            with self.lock:
                self.phase = "error" if self.error else "stopped"
            self.done.set()

    def _finalize(self):
        result = self.session.snapshot()
        result["acquisition_channels"] = self.channels
        self._json("model.json", {k: v for k, v in result.items() if k != "series"})
        self._json("metadata.json", {"source": self.source, "channels": self.channels, "received_including_preview": self.received,
                                    "accepted": self.accepted, "recorded": self.recorded, "fitted": self.fitted,
                                    "error": self.error, "rejected_sample": self.rejected, "status": "error" if self.error else "stopped",
                                    "decoder_diagnostics": self.parser.diagnostics() if isinstance(getattr(self, "parser", None), BinaryStream) else None,
                                    "partial_line_bytes_not_accepted": len(self.parser.pending) if isinstance(getattr(self, "parser", None), CSVStream) else 0,
                                    "partial_frame_bytes_not_accepted": len(self.parser.pending) if isinstance(getattr(self, "parser", None), BinaryStream) else 0})
        report = ["# 在线采集与辨识记录", "", "数据来源：" + {"demo": "仿真演示（非真实设备）", "serial": "USB 串口", "file": "CSV 文件跟随"}[self.source["kind"]],
                  f"已接受 {self.accepted} 点；已保存 {self.recorded} 点；已用于建模 {self.fitted} 点。",
                  "仅记录开始自动建模之后的新采样；预览历史未参与辨识。",
                  f"停止原因：{self.error or '用户结束采集'}", "",
                  "原始数值：raw_samples.csv；单位换算后：samples.csv；完整在线预测：predictions.csv。",
                  "samples.csv 中 u、y 的含义、单位及倍率见 metadata.json；时间单位为秒。",
                  "模型关系：y[k] = Σ aᵢ y[k−i] + Σ bⱼ u[k−nk−j] + intercept。",
                  "", "## 当前模型", ""]
        report += [f"- {p['name']} = {p['value']:.10g}" for p in result["parameters"]] or ["尚未收集到足够的有效数据以建立模型。"]
        report += model_report(result["mathematical_model"])
        report += ["", "辨识算法：" + result["algorithm"]]
        if result.get("frequency"):
            report += ["频域窗口设置与最终频谱、相干度见 model.json 的 frequency。"]
        report += ["", "## 说明", "", *["- " + w for w in result["warnings"]],
                   "- ARX 系数描述输入输出动态关系，不能直接解释为惯量、电阻等物理参数。",
                   "- 图表保留最近 2,000 个预测；CSV 保存本次实验完整的已记录采样和在线预测。"]
        (self.directory / "report.md").write_text("\n".join(report), encoding="utf-8")
        archive = self.directory / "experiment.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            names = ["raw_samples.csv", "samples.csv", "predictions.csv", "model.json", "metadata.json", "report.md"]
            for name in names:
                zipped.write(self.directory / name, name)
        with self.lock:
            self.archive = archive
            self.result = result

    def stop(self):
        with self.lock:
            if not self.done.is_set():
                self.phase = "stopping"
            self.stop_event.set()
        return self.snapshot()

    def snapshot(self):
        """Use a cached fit result; polling must not wait for order selection."""
        with self.lock:
            age = time.monotonic() - self.last_sample_at if self.last_sample_at else None
            return {"id": self.id, "source": self.source, "phase": self.phase, "error": self.error,
                    "columns": list(self.columns), "preview": list(self.preview), "received": self.received,
                    "accepted": self.accepted, "recorded": self.recorded, "fitted": self.fitted,
                    "queued": self.queue.qsize(), "sample_age": age, "channels": self.channels,
                    "result": self.result, "finished": self.done.is_set(),
                    "decoder_diagnostics": self.parser.diagnostics() if isinstance(getattr(self, "parser", None), BinaryStream) else None,
                    "download": f"/api/acquisition/download?id={self.id}" if self.archive else None}


class AcquisitionManager:
    """One active hardware connection; persisted templates and UUID-only exports."""

    def __init__(self, root):
        self.root, self.current = Path(root), None
        self.lock = threading.Lock()

    def connect(self, config):
        with self.lock:
            if self.current and not self.current.done.is_set():
                raise IdentificationError("请先停止当前采集，等数据保存完成后再连接。")
            acquisition = Acquisition(config, self.root)
            self.current = acquisition
            acquisition.connect()
            return acquisition.snapshot()

    def get(self, key):
        with self.lock:
            if not self.current or key != self.current.id:
                raise IdentificationError("采集连接已改变，请刷新页面。")
            return self.current

    def active(self):
        with self.lock:
            return self.current.snapshot() if self.current else None

    def templates(self):
        path = self.root / "templates.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def save_template(self, payload):
        name = str(payload.get("name", "")).strip()[:80]
        if not name:
            raise IdentificationError("请填写设备模板名称。")
        acquisition = self.get(payload.get("id"))
        with acquisition.lock:
            channels = channel_config(payload.get("channels", {}), acquisition.columns, list(acquisition.preview))
        with self.lock:
            items = [t for t in self.templates() if t["name"] != name]
            if len(items) >= 50:
                raise IdentificationError("最多保存 50 个模板，请复用已有模板名称。")
            items.append({"name": name, "source": acquisition.source, "channels": channels})
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.root / "templates.tmp"
            temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.root / "templates.json")
            return items

    def download(self, key):
        if not isinstance(key, str) or not re.fullmatch("[a-f0-9]{32}", key):
            raise IdentificationError("实验编号无效。")
        with self.lock:
            if self.current and key == self.current.id and not self.current.done.is_set():
                raise IdentificationError("正在保存数据，请稍候。")
        path = self.root / key / "experiment.zip"
        if not path.is_file():
            raise IdentificationError("实验尚未保存，或文件不存在。")
        return path

    def close(self):
        if self.current:
            self.current.stop()
            self.current.done.wait(30)
