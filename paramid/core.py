"""Model contracts and numerically scaled linear parameter estimation.

No transport or UI dependencies belong in this module. Models produce phi so
that y[k] = phi[k] @ theta + noise. ARX uses POSITIVE output coefficients:
y[k] = sum(a_i*y[k-i]) + sum(b_j*u[k-nk-j]) + c.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Mapping

import numpy as np


class IdentificationError(ValueError):
    """A user-correctable model, data, or numerical error."""


def finite_number(value, name: str) -> float:
    """Reject booleans, missing values, NaN and infinity at system boundaries."""
    if isinstance(value, bool):
        raise IdentificationError(f"{name} 必须是有限数值。")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise IdentificationError(f"{name} 必须是有限数值。") from exc
    if not math.isfinite(result) or abs(result) > 1e100:
        raise IdentificationError(f"{name} 超出允许的有限数值范围。")
    return result


def integer(value, name: str, low: int, high: int) -> int:
    result = finite_number(value, name)
    if result != int(result) or not low <= result <= high:
        raise IdentificationError(f"{name} 必须是 {low}～{high} 的整数。")
    return int(result)


@dataclass(frozen=True)
class ModelSpec:
    """Immutable model metadata, shared by batch fitting and streaming.

    A regression feature is a measured/precomputed CSV column (e.g. acceleration,
    sin(position), current); names are never executed as Python expressions.
    Sampling time is in seconds. ARX assumes uniform sampling within 1 percent.
    """

    kind: str
    output: str
    features: tuple[str, ...] = ()
    input: str = ""
    time: str = ""
    sample_time: float = 0.01
    intercept: bool = True
    na: int = 1
    nb: int = 1
    nk: int = 1

    @classmethod
    def from_dict(cls, config: dict) -> "ModelSpec":
        if not isinstance(config, dict):
            raise IdentificationError("model 必须是对象。")
        kind = config.get("kind", "regression")
        if kind not in ("regression", "arx"):
            raise IdentificationError("首版支持 regression 和 arx 模型。")
        output, time_col = config.get("output", ""), config.get("time", "")
        if not isinstance(output, str) or not output.strip():
            raise IdentificationError("请选择输出列。")
        if not isinstance(time_col, str) or time_col == output:
            raise IdentificationError("时间列必须与输出列不同。")
        intercept = config.get("intercept", True)
        if not isinstance(intercept, bool):
            raise IdentificationError("intercept 必须是布尔值。")
        dt = finite_number(config.get("sample_time", 0.01), "采样周期")
        if dt <= 0:
            raise IdentificationError("采样周期必须为正。")
        features = config.get("features", []) if kind == "regression" else []
        if not isinstance(features, list) or any(not isinstance(x, str) or not x for x in features):
            raise IdentificationError("回归特征必须是列名列表。")
        if kind == "regression" and (not features or len(features) != len(set(features))):
            raise IdentificationError("至少选择一个特征，且特征不能重复。")
        if output in features or (time_col and time_col in features):
            raise IdentificationError("输出列和时间列不能同时作为回归特征。")
        if intercept and "intercept" in features:
            raise IdentificationError("特征名 intercept 与截距参数重名，请改名或关闭截距。")
        input_col = config.get("input", "") if kind == "arx" else ""
        if kind == "arx" and (not isinstance(input_col, str) or not input_col or input_col in (output, time_col)):
            raise IdentificationError("ARX 输入列、输出列和时间列必须不同。")
        spec = cls(kind=kind, output=output, features=tuple(features), input=input_col,
                   time=time_col, sample_time=dt, intercept=intercept,
                   na=integer(config.get("na", 1), "na", 1, 8),
                   nb=integer(config.get("nb", 1), "nb", 1, 8),
                   nk=integer(config.get("nk", 1), "nk", 0, 50))
        if len(spec.names) > 32:
            raise IdentificationError("首版最多支持 32 个参数（含截距）。")
        return spec

    @property
    def names(self) -> list[str]:
        base = list(self.features) if self.kind == "regression" else (
            [f"a{i}" for i in range(1, self.na + 1)] + [f"b{i}" for i in range(self.nb)])
        return base + (["intercept"] if self.intercept else [])

    @property
    def warmup(self) -> int:
        return max(self.na, self.nk + self.nb - 1) if self.kind == "arx" else 0

    @property
    def columns(self) -> list[str]:
        return list(dict.fromkeys(([self.time] if self.time else []) +
                                 (list(self.features) if self.kind == "regression" else [self.input]) + [self.output]))


class ModelStream:
    """Build a causal regressor using bounded histories and validated timestamps.

    ARX warm-up consumes measurements without estimating. Once warm, the current
    measured output is excluded from phi, preventing target leakage. History is
    updated only after validation. Regression needs no warm-up.
    """

    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.rows = deque(maxlen=spec.warmup + 1)
        self.count = 0
        self.last_time = None

    def step(self, sample: Mapping) -> tuple[np.ndarray, float, float] | None:
        if not isinstance(sample, Mapping):
            raise IdentificationError("每个样本必须是列名到数值的对象。")
        spec = self.spec
        row = {key: finite_number(sample.get(key), key) for key in spec.columns}
        timestamp = row[spec.time] if spec.time else self.count * spec.sample_time
        if self.last_time is not None:
            delta = timestamp - self.last_time
            if delta <= 0:
                raise IdentificationError("时间戳必须严格递增，不能重复或乱序。")
            if spec.kind == "arx" and abs(delta - spec.sample_time) > 0.01 * spec.sample_time + 1e-12:
                raise IdentificationError("ARX 时间间隔偏差超过 1%；请检查采样周期、丢包和时间单位。")
        phi = None
        if self.count >= spec.warmup:
            if spec.kind == "regression":
                values = [row[key] for key in spec.features]
            else:
                values = [self.rows[-i][spec.output] for i in range(1, spec.na + 1)]
                for j in range(spec.nb):
                    lag = spec.nk + j
                    values.append(row[spec.input] if lag == 0 else self.rows[-lag][spec.input])
            if spec.intercept:
                values.append(1.0)
            phi = np.asarray(values, dtype=float)
        self.rows.append(row)
        self.last_time, self.count = timestamp, self.count + 1
        return None if phi is None else (phi, row[spec.output], timestamp)


def information_diagnostics(matrix: np.ndarray) -> dict:
    """Numerical rank of the supplied design; this is NOT a proof of PE."""
    singular = np.linalg.svd(matrix, compute_uv=False)
    tolerance = max(matrix.shape) * np.finfo(float).eps * singular[0] if len(singular) else 0
    rank = int(np.count_nonzero(singular > tolerance))
    condition = float(singular[0] / singular[-1]) if rank == matrix.shape[1] and singular[-1] > 0 else None
    return {"rank": rank, "parameters": matrix.shape[1], "condition": condition}


def resolve_offline_algorithm(algorithm: str, weight_column=None) -> tuple[str, str | None]:
    """Resolve optional WLS weighting consistently for batch fitting and order search.

    Omitting weights means equal observation weights, exactly ordinary LS.
    A selected column still requires valid positive weights; invalid measurements
    must never silently fall back to an unweighted fit.
    """
    if algorithm not in ("ls", "wls", "ridge"):
        raise IdentificationError("离线算法应为 ls、wls 或 ridge。")
    if algorithm != "wls":
        return algorithm, None
    if weight_column is None:
        return "ls", None
    if not isinstance(weight_column, str):
        raise IdentificationError("权重列必须为列名或留空。")
    column = weight_column.strip()
    return ("wls", column) if column else ("ls", None)


def fit_linear(x: np.ndarray, y: np.ndarray, algorithm: str = "ls",
               weights: np.ndarray | None = None, alpha: float = 1.0,
               intercept: bool = False) -> dict:
    """Solve scaled LS/WLS/ridge via SVD-backed lstsq, never normal-equation inverse.

    X: (n, p), y: (n,). Scales are RMS values derived from TRAINING data only.
    WLS weights are relative inverse variances; None uses ordinary LS (equal
    weights). Explicit weights must remain positive and finite. Ridge penalizes coefficients in
    these scaled coordinates; its intercept is unpenalized. Rank deficiency is
    fatal for LS/WLS but explicitly reported (not cured) when using ridge.
    Batch complexity is O(n*p^2 + p^3); memory O(n*p).
    """
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if x.ndim != 2 or y.ndim != 1 or len(x) != len(y) or not 1 <= x.shape[1] <= 32 or len(y) <= x.shape[1]:
        raise IdentificationError("回归矩阵维度错误，或训练样本数不足（必须大于参数数）。")
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise IdentificationError("训练数据存在非有限数值。")
    if algorithm not in ("ls", "wls", "ridge"):
        raise IdentificationError("离线算法应为 ls、wls 或 ridge。")
    if algorithm == "wls" and weights is None:
        algorithm = "ls"
    scales = np.linalg.norm(x, axis=0) / np.sqrt(len(x))
    if np.any(scales == 0) and algorithm != "ridge":
        raise IdentificationError("存在全零回归列，无法唯一辨识对应参数。")
    scales[scales == 0] = 1.0
    design, target = x / scales, y.copy()
    if algorithm == "wls":
        weights = np.asarray(weights, dtype=float)
        if weights.shape != y.shape or not np.all(np.isfinite(weights)) or np.any(weights <= 0):
            raise IdentificationError("WLS 需要逐样本正权重，建议使用测量方差的倒数。")
        root_weight = np.sqrt(weights / np.mean(weights))
        design, target = design * root_weight[:, None], target * root_weight
    diagnostics = information_diagnostics(design)
    warnings = []
    if diagnostics["rank"] < x.shape[1]:
        if algorithm != "ridge":
            raise IdentificationError("回归矩阵秩不足，参数不能唯一辨识；请增加独立激励或减少参数。")
        warnings.append("训练矩阵秩不足：岭回归给出正则化解，不能据此认定物理参数可唯一辨识。")
    if diagnostics["condition"] is not None and diagnostics["condition"] > 1e8:
        warnings.append("缩放后回归矩阵条件数较大，参数对噪声敏感。")
    if algorithm == "ridge":
        alpha = finite_number(alpha, "岭参数 alpha")
        if alpha <= 0:
            raise IdentificationError("岭参数 alpha 必须为正。")
        penalty = np.eye(x.shape[1]) * np.sqrt(alpha)
        if intercept:
            penalty[-1, -1] = 0.0
        design = np.vstack((design, penalty))
        target = np.concatenate((target, np.zeros(x.shape[1])))
    theta = np.linalg.lstsq(design, target, rcond=None)[0] / scales
    if not np.all(np.isfinite(theta)):
        raise IdentificationError("数值溢出，请调整单位和特征尺度。")
    return {"theta": theta, "scales": scales, "diagnostics": diagnostics, "warnings": warnings}


class RLS:
    """Forgetting-factor RLS with fixed feature scales and Joseph covariance update.

    Internal model uses z=phi/scales and beta=theta*scales. The gain is
    K=P_prior*z/(1+z.T*P_prior*z), P_prior=P/lambda. Prediction is evaluated BEFORE
    correction. P is inverse information under unit observation noise, not an
    automatically calibrated parameter-confidence covariance.

    Joseph products are evaluated as rank-one operations, giving O(p^2) time and
    O(p^2) estimator memory per update. No hard real-time guarantee is made for
    Python. Excessive covariance growth rejects the update without mutation.
    """

    def __init__(self, size: int, forgetting: float = 0.995, p0: float = 1000,
                 scales=None, theta0=None):
        size = integer(size, "参数数", 1, 32)
        self.forgetting = finite_number(forgetting, "遗忘因子")
        p0 = finite_number(p0, "初始信息逆矩阵尺度")
        if not 0.9 <= self.forgetting <= 1 or not 0 < p0 <= 1e10:
            raise IdentificationError("遗忘因子须在 [0.9, 1]，P0 须在 (0, 1e10]。")
        self.scales = np.ones(size) if scales is None else np.asarray(scales, dtype=float)
        theta = np.zeros(size) if theta0 is None else np.asarray(theta0, dtype=float)
        if self.scales.shape != (size,) or not np.all(np.isfinite(self.scales)) or np.any(self.scales <= 0):
            raise IdentificationError("在线固定特征尺度必须为逐参数正数。")
        if theta.shape != (size,) or not np.all(np.isfinite(theta)):
            raise IdentificationError("初始参数维度不正确或包含非有限数。")
        self.beta = theta * self.scales
        if not np.all(np.isfinite(self.beta)):
            raise IdentificationError("初值与特征尺度的乘积溢出，请调整单位。")
        self.p_matrix = np.eye(size) * p0
        self.information = np.zeros((size, size))  # Excludes the artificial prior.
        self.count = 0

    @property
    def theta(self) -> np.ndarray:
        return self.beta / self.scales

    def step(self, phi, measurement: float) -> dict:
        phi = np.asarray(phi, dtype=float)
        y = finite_number(measurement, "测量输出")
        if phi.shape != self.scales.shape or not np.all(np.isfinite(phi)):
            raise IdentificationError("在线回归向量维度错误或包含非有限数。")
        z = phi / self.scales
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            prediction = float(z @ self.beta)
            residual = y - prediction
            p_prior = self.p_matrix / self.forgetting
            p_phi = p_prior @ z
            denominator = 1.0 + float(z @ p_phi)
            if not math.isfinite(denominator) or denominator <= 0:
                raise IdentificationError("RLS 数值失效，请检查尺度并重建会话。")
            gain = p_phi / denominator
            beta = self.beta + gain * residual
            # Joseph: A=(I-Kz^T)P; Pnew=A(I-zK^T)+KK^T, using rank-one products.
            left = p_prior - np.outer(gain, p_phi)
            covariance = left - np.outer(left @ z, gain) + np.outer(gain, gain)
            covariance = (covariance + covariance.T) * 0.5
            information = self.forgetting * self.information + np.outer(z, z)
        if (not np.all(np.isfinite(beta)) or not np.all(np.isfinite(covariance)) or
                not np.all(np.isfinite(information)) or not math.isfinite(residual) or
                np.max(np.abs(covariance)) > 1e14 or np.any(np.diag(covariance) <= 0)):
            raise IdentificationError("RLS 溢出或弱激励导致信息逆矩阵膨胀；本次更新未提交。请调整尺度、遗忘因子或激励。")
        self.beta, self.p_matrix, self.information = beta, covariance, information
        self.count += 1
        return {"prediction": prediction, "residual": residual, "theta": self.theta.tolist()}

    def diagnostics(self) -> dict:
        """Snapshot diagnostics are O(p^3); no prior is counted as observed excitation."""
        eig = np.linalg.eigvalsh(self.information)
        tolerance = max(float(eig[-1]), 1e-300) * max(self.count, len(eig)) * np.finfo(float).eps
        rank = int(np.count_nonzero(eig > tolerance))
        condition = float(np.sqrt(eig[-1] / eig[0])) if rank == len(eig) and eig[0] > 0 else None
        return {"rank": rank, "parameters": len(eig), "condition": condition,
                "updates": self.count, "forgetting": self.forgetting,
                "p_diagonal": np.diag(self.p_matrix).tolist()}
