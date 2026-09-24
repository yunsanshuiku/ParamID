"""Offline experiment workflow and bounded, ordered online sessions."""

from collections import deque
from collections.abc import Mapping
from dataclasses import asdict
import copy
import csv
import io
import threading
import time
import uuid

import numpy as np

from .core import IdentificationError, ModelSpec, ModelStream, RLS, finite_number, fit_linear, integer, resolve_offline_algorithm
from .data import parse_csv, prepare
from .selection import select_arx_order
from .representation import mathematical_model, model_report



def metrics(y, prediction) -> dict:
    """Fit percent may be negative; it is undefined for almost-constant targets."""
    error = np.asarray(y) - np.asarray(prediction)
    norm = float(np.linalg.norm(np.asarray(y) - np.mean(y)))
    return {"rmse": float(np.sqrt(np.mean(error ** 2))), "mae": float(np.mean(np.abs(error))),
            "fit_percent": float(100 * (1 - np.linalg.norm(error) / norm)) if norm > 1e-12 * max(1, np.linalg.norm(y)) else None}


def free_run(rows: list[dict], spec: ModelSpec, theta: np.ndarray, start: int) -> list[float] | None:
    """Initialize using pre-validation history; thereafter feed back predictions only."""
    if spec.kind != "arx":
        return None
    stream = ModelStream(spec)
    for row in rows[max(0, start - spec.warmup):start]:
        stream.step(row)
    predictions = []
    for row in rows[start:]:
        # step builds phi from past measurements/predictions and the known input.
        phi, _, _ = stream.step(row)
        value = float(phi @ theta)
        if not np.isfinite(value) or abs(value) > 1e100:
            return None
        stream.rows[-1][spec.output] = value
        predictions.append(value)
    return predictions


def offline(payload: dict) -> dict:
    """Select ARX structure on a separate window; never train/select on final validation."""
    reject_removed_filter(payload)
    if payload.get("difference", False):
        raise IdentificationError("差分预处理已移除，请取消 difference 参数并使用原始信号辨识；旧页面请刷新。")
    if payload.get("algorithm") == "frequency":
        from .multichannel import offline_multichannel
        model = payload.get("model", {})
        if model.get("kind") == "regression":
            raise IdentificationError("窗口频域辨识用于线性动态系统，请选择动态模型。")
        if model.get("kind") == "arx":
            model = {**model, "kind": "multichannel", "inputs": [model["input"]], "outputs": [model["output"]]}
        return offline_multichannel({**payload, "model": model})
    if payload.get("model", {}).get("kind") in (None, "multichannel"):
        from .multichannel import offline_multichannel
        return offline_multichannel(payload)
    spec = ModelSpec.from_dict(payload.get("model", {}))
    requested_algorithm = payload.get("algorithm", "ls")
    algorithm, weight_column = resolve_offline_algorithm(requested_algorithm, payload.get("weight_column"))
    algorithm_note = "未选择权重列，按普通最小二乘（LS，等权重）计算。" if requested_algorithm == "wls" and algorithm == "ls" else ""
    _, rows = parse_csv(payload.get("csv", ""), payload.get("header_mode", "auto"))
    independent = bool(payload.get("validation_csv"))
    mode = payload.get("order_mode", "auto")
    if mode not in ("auto", "fixed"):
        raise IdentificationError("order_mode 必须为 auto 或 fixed。")
    automatic = spec.kind == "arx" and mode == "auto"
    selection, fit = None, None
    if automatic:
        selected = select_arx_order(rows, spec, {**payload, "algorithm": algorithm, "weight_column": weight_column}, reserve_validation=not independent)
        spec, fit, selection = selected["spec"], selected["fit"], selected["summary"]
    data = prepare(rows, spec)
    if independent:
        _, validation_rows = parse_csv(payload["validation_csv"], payload.get("validation_header_mode", "auto"))
        validation = prepare(validation_rows, spec)
        if automatic:
            begin, end = selection["train_start"] - spec.warmup, selection["train_end"] - spec.warmup
            x_train, y_train = data["x"][begin:end], data["y"][begin:end]
        else:
            x_train, y_train = data["x"], data["y"]
        x_valid, y_valid, t_valid = validation["x"], validation["y"], validation["time"]
        simulation_rows, simulation_start = validation_rows, spec.warmup
    elif automatic:
        begin, end = selection["train_start"] - spec.warmup, selection["train_end"] - spec.warmup
        split = selection["selection_end"] - spec.warmup
        x_train, y_train = data["x"][begin:end], data["y"][begin:end]
        x_valid, y_valid, t_valid = data["x"][split:], data["y"][split:], data["time"][split:]
        simulation_rows, simulation_start = rows, selection["selection_end"]
    else:
        ratio = finite_number(payload.get("train_ratio", 0.7), "训练比例")
        if not 0.5 <= ratio <= 0.9:
            raise IdentificationError("训练比例须在 [0.5, 0.9]。")
        split = int(len(data["y"]) * ratio)
        x_train, y_train = data["x"][:split], data["y"][:split]
        x_valid, y_valid, t_valid = data["x"][split:], data["y"][split:], data["time"][split:]
        simulation_rows, simulation_start = rows, spec.warmup + split
    if len(y_valid) < 5:
        raise IdentificationError("验证数据至少需要 5 个有效样本。")
    weights = None
    if algorithm == "wls" and fit is None:
        weights = np.asarray([finite_number(row.get(weight_column), "权重列")
                              for row in rows[spec.warmup:spec.warmup + len(y_train)]])
    if fit is None:
        fit = fit_linear(x_train, y_train, algorithm, weights, payload.get("alpha", 1), spec.intercept)
    theta = fit["theta"]
    prediction = x_valid @ theta
    simulation = free_run(simulation_rows, spec, theta, simulation_start)
    warnings = list(fit["warnings"])
    if selection:
        warnings.extend(selection["warnings"])
    poles = []
    if spec.kind == "arx":
        roots = np.roots(np.r_[1.0, -theta[:spec.na]])
        poles = [{"real": float(r.real), "imag": float(r.imag), "magnitude": float(abs(r))} for r in roots]
        if any(abs(r) >= 1 for r in roots):
            warnings.append("ARX 存在单位圆上或外的极点；请结合被辨识对象检查积分、不稳定动态或模型失配。")
        if simulation is None:
            warnings.append("自由运行仿真发散并超出数值范围，无法报告该项指标。")
        warnings.append("ARX 系数是输入输出模型参数；测量噪声或闭环相关噪声可能使 LS/RLS 有偏。")
    warnings.append("数值秩与条件数只描述当前数据，不能替代结构可辨识性分析或持续激励证明。")
    residual = y_valid - prediction
    lag1 = None
    if np.std(residual[:-1]) > 1e-15 and np.std(residual[1:]) > 1e-15:
        lag1 = float(np.corrcoef(residual[:-1], residual[1:])[0, 1])
        if abs(lag1) > 0.2:
            warnings.append("验证残差的一阶相关性较明显，建议检查动态阶次、延迟和噪声模型；该阈值仅用于提示。")
    result = {"version": "0.7.0", "mode": "offline", "model": asdict(spec), "algorithm": algorithm,
              "algorithm_note": algorithm_note,
              "settings": {"alpha": payload.get("alpha", 1) if algorithm == "ridge" else None,
                           "weight_column": weight_column, "requested_algorithm": requested_algorithm,
                           "train_ratio": None if independent or automatic else payload.get("train_ratio", 0.7),
                           "order_mode": "auto" if automatic else "fixed"},
              "parameters": [{"name": name, "value": float(value)} for name, value in zip(spec.names, theta)],
              "scales": fit["scales"].tolist(), "diagnostics": fit["diagnostics"], "poles": poles,
              "train_samples": len(y_train), "validation_samples": len(y_valid),
              "selection_samples": selection["selection_samples"] if selection else 0,
              "order_selection": selection,
              "validation_source": "独立文件" if independent else "按时间连续留出",
              "metrics": metrics(y_valid, prediction),
              "simulation_metrics": metrics(y_valid, simulation) if simulation is not None else None,
              "residual_lag1": lag1, "warnings": warnings}
    result["mathematical_model"] = mathematical_model(spec, theta)
    indices = np.unique(np.linspace(0, len(y_valid) - 1, min(1500, len(y_valid)), dtype=int))
    result["series"] = [{"time": float(t_valid[i]), "measured": float(y_valid[i]),
                         "prediction": float(prediction[i]), "residual": float(residual[i]),
                         "simulation": float(simulation[i]) if simulation is not None else None} for i in indices]
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["time_s", "measured", "one_step_prediction", "free_run", "residual"])
    export_rows = zip(t_valid, y_valid, prediction, simulation if simulation is not None else [""] * len(y_valid), residual)
    for index, item in enumerate(export_rows):
        writer.writerow(item)
    result["csv_export"] = buf.getvalue()
    result["report"] = "\n".join([
        "# 参数辨识报告", "", f"模型：{spec.kind}；算法：{algorithm}",
        *([algorithm_note] if algorithm_note else []),
        f"训练样本：{len(y_train)}；验证样本：{len(y_valid)}；验证方式：{result['validation_source']}",
        *(["", "## 自动定阶", "", f"推荐 na={spec.na}, nb={spec.nb}, nk={spec.nk}（延迟 {spec.nk * spec.sample_time:.8g} 秒）。",
           f"搜索 na=1～8，nb=1～8，nk=0～10，共 {selection['candidate_count']} 个候选，{selection['valid_count']} 个可用。",
           selection["criterion"], f"选模样本：{selection['selection_samples']}；最终验证数据未用于定阶。",
           f"推荐模型选模 RMSE：{selection['selected']['selection_rmse']:.8g}"] if selection else []),
        "", "## 参数", "", "| 参数 | 估计值 |", "| --- | ---: |",
        *[f"| {p['name'].replace('|', '/')} | {p['value']:.10g} |" for p in result["parameters"]],
        "", f"验证一步预测 RMSE：{result['metrics']['rmse']:.8g}",
        f"验证自由运行 RMSE：{result['simulation_metrics']['rmse']:.8g}" if simulation is not None else "",
        *model_report(result["mathematical_model"]),
        "", "## 诊断与适用范围", "", *[f"- {w}" for w in warnings],
        "", "导出的模型参数沿用输入数据单位。在线误差与离线留出验证指标不能直接等同。",
    ])
    return result


class OnlineSession:
    """Atomic online stream; ARX defaults to causal initial order calibration.

    The first 400 observed samples trigger selection. With insufficient excitation,
    retry every 200 samples using a bounded 1200-sample window. Calibration is
    internal initialization; historical errors are not presented as live forecasts.
    Once selected, the structure stays fixed and RLS tracks its coefficients.
    """

    def __init__(self, config: dict):
        self.spec = ModelSpec.from_dict(config.get("model", {}))
        reject_removed_filter(config)
        if config.get("algorithm", "rls") not in ("rls", "ls"):
            raise IdentificationError("RLS 会话算法须为 rls；频域会话请通过会话工厂建立。")
        mode = config.get("order_mode", "auto")
        if mode not in ("auto", "fixed"):
            raise IdentificationError("order_mode 必须为 auto 或 fixed。")
        self.automatic = self.spec.kind == "arx" and mode == "auto"
        if self.automatic and (config.get("scales") is not None or config.get("theta0") is not None):
            raise IdentificationError("自动定阶时参数维度尚未确定；尺度与初值由初始数据自动建立。")
        # Validate scalar RLS settings before collecting any samples.
        initial = RLS(len(self.spec.names), config.get("forgetting", 0.995), config.get("p0", 1000),
                      config.get("scales"), config.get("theta0"))
        self.forgetting, self.p0 = initial.forgetting, float(config.get("p0", 1000))
        self.stream = ModelStream(self.spec)
        self.estimator = None if self.automatic else initial
        self.calibration = deque(maxlen=1200)
        self.calibration_samples = 0
        self.last_selection_attempt = 0
        self.selection_error = ""
        self.order_selection = None
        self.history = deque(maxlen=2000)
        self.sequence = 0
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def push(self, samples: list, sequence: int) -> dict:
        """Either all samples commit or none do; duplicate/reordered requests are rejected."""
        if not isinstance(samples, list) or not 1 <= len(samples) <= 256:
            raise IdentificationError("每次提交需要 1～256 个样本。")
        sequence = integer(sequence, "sequence", 0, 2**53 - 1)
        with self.lock:
            if sequence != self.sequence:
                raise IdentificationError(f"样本序号不匹配，期望 sequence={self.sequence}；请先读取会话快照。")
            trial = copy.copy(self)
            trial.stream = copy.deepcopy(self.stream)
            trial.estimator = copy.deepcopy(self.estimator)
            trial.calibration = copy.deepcopy(self.calibration)
            trial.history = deque(self.history, maxlen=2000)
            additions = []
            for sample in samples:
                update = trial._consume(sample)
                if update is not None:
                    additions.append(update)
            trial.history.extend(additions)
            trial.updated = time.monotonic()
            self.__dict__.update(trial.__dict__)
            result = self._snapshot(additions, "append")
            return result

    def _consume(self, sample: dict) -> dict | None:
        """Consume only observed data; failed selection retains a bounded calibration window."""
        item = self.stream.step(sample)
        self.sequence += 1
        if self.estimator is None:
            self.calibration.append(dict(self.stream.rows[-1]))
            if self.sequence >= 400 and self.sequence - self.last_selection_attempt >= 200:
                self.last_selection_attempt = self.sequence
                try:
                    selected = select_arx_order(list(self.calibration), self.spec, {"algorithm": "ls"}, reserve_validation=False)
                except IdentificationError as exc:
                    self.selection_error = str(exc)
                    return None
                self.spec = selected["spec"]
                stream = ModelStream(self.spec)
                estimator = RLS(len(self.spec.names), self.forgetting, self.p0, selected["fit"]["scales"])
                # All buffered observations precede the first displayed live forecast.
                for row in self.calibration:
                    buffered = stream.step(row)
                    if buffered is not None:
                        phi, y, _ = buffered
                        estimator.step(phi, y)
                stream.count = self.sequence
                if not self.spec.time:
                    stream.last_time = (self.sequence - 1) * self.spec.sample_time
                self.stream, self.estimator = stream, estimator
                self.calibration_samples = self.sequence
                self.order_selection = {**selected["summary"], "purpose": "online_initialization",
                                        "calibration_samples": self.sequence, "window_samples": len(self.calibration)}
                self.selection_error = ""
                self.calibration.clear()
            return None
        if item is None:
            return None
        phi, y, timestamp = item
        result = {"time": timestamp, "measured": y, **self.estimator.step(phi, y)}
        return result

    def _snapshot(self, series=None, history_mode="replace") -> dict:
        pending = self.estimator is None
        diagnostic = ({"rank": 0, "parameters": 0, "condition": None, "updates": 0,
                       "forgetting": self.forgetting, "p_diagonal": []} if pending else self.estimator.diagnostics())
        warnings = ["在线显示更新前的一步预测误差；它不是独立验证误差，也不是参数置信区间。"]
        if pending:
            warnings.append(self.selection_error or "正在收集初始数据；达到 400 点后自动选择阶次和输入延迟。")
        elif diagnostic["rank"] < len(self.spec.names):
            warnings.append("已观测数据的有效秩不足，部分参数尚不能被独立区分。")
        if diagnostic["condition"] is not None and diagnostic["condition"] > 1e8:
            warnings.append("在线回归数据病态，请检查激励与特征尺度。")
        selection = self.order_selection
        if self.automatic and pending:
            selection = {"mode": "auto", "status": "collecting", "received": self.sequence,
                         "target_samples": 400 if self.sequence < 400 else self.last_selection_attempt + 200,
                         "message": self.selection_error}
        if self.order_selection:
            warnings.extend(self.order_selection["warnings"])
            warnings.append("初始数据用于自动定阶和初始化；仅展示定阶完成后的在线预测误差。")
            if history_mode == "append":
                # Incremental high-rate replies need only the visible shortlist.
                selection = {**selection, "candidates": selection["candidates"][:12]}
        return {"version": "0.7.0", "mode": "online", "model": asdict(self.spec), "algorithm": "rls",
                "next_sequence": self.sequence, "updates": 0 if pending else self.estimator.count,
                "warmup_remaining": max(0, selection["target_samples"] - self.sequence) if pending else max(0, self.spec.warmup - self.stream.count),
                "order_selection": selection, "calibration_samples": self.calibration_samples,
                "parameters": [] if pending else [{"name": name, "value": float(v)} for name, v in zip(self.spec.names, self.estimator.theta)],
                "mathematical_model": mathematical_model(self.spec, self.estimator.theta)
                if not pending and self.estimator.count > 0 else None,
                "scales": [] if pending else self.estimator.scales.tolist(), "diagnostics": diagnostic,
                "series": list(self.history) if series is None else series,
                "history_mode": history_mode, "warnings": warnings}

    def snapshot(self) -> dict:
        with self.lock:
            self.updated = time.monotonic()
            return self._snapshot()


def reject_removed_filter(config):
    """Reject stale enabled settings instead of silently changing the experiment."""
    old = config.get("output_filter")
    if old is not None and (not isinstance(old, dict) or old.get("enabled", False)):
        raise IdentificationError("低通滤波模块已移除，请刷新页面并移除旧模板中的 output_filter 设置。")


def create_online_session(config):
    """Select a stream estimator while preserving the shared ordered push API."""
    reject_removed_filter(config)
    if config.get("algorithm", "rls") == "frequency":
        from .frequency_session import FrequencySession
        return FrequencySession(config)
    return OnlineSession(config)


class SessionStore:
    """At most 8 local sessions; inactive sessions expire after one hour."""

    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()

    def create(self, config: dict) -> dict:
        session = create_online_session(config)
        with self.lock:
            now = time.monotonic()
            self.sessions = {key: value for key, value in self.sessions.items() if now - value.updated < 3600}
            if len(self.sessions) >= 8:
                raise IdentificationError("同时最多 8 个会话，请先结束不使用的会话。")
            key = uuid.uuid4().hex
            self.sessions[key] = session
        return {"session_id": key, **session.snapshot()}

    def get(self, key: str) -> OnlineSession:
        if not isinstance(key, str):
            raise IdentificationError("session_id 必须是字符串。")
        with self.lock:
            if key not in self.sessions:
                raise IdentificationError("会话不存在或已过期，请重新建立。")
            if time.monotonic() - self.sessions[key].updated >= 3600:
                del self.sessions[key]
                raise IdentificationError("会话已过期，请重新建立。")
            return self.sessions[key]

    def delete(self, key: str):
        with self.lock:
            self.sessions.pop(key, None)
