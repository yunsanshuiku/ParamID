"""Conservative ZOH reconstruction of a continuous ARX input/output model.

This is a discrete-to-continuous equivalence under stated assumptions, not a
second fit to continuous measurements and not a unique recovery of the plant.
Never take log of the lag-register realization: its artificial zero eigenvalues
cannot be exponentials of finite continuous modes. Instead separate only the
integer delay needed to obtain a proper, invertible dynamic core, construct an
observable canonical realization, and use the principal augmented matrix log.
"""

import copy
from functools import lru_cache
import warnings

import numpy as np
from scipy.linalg import expm, logm
from scipy.signal import ss2tf


ASSUMPTIONS = [
    "由当前离散模型按零阶保持（ZOH）转换：假设输入在每个采样周期内保持不变，采样周期准确。",
    "使用矩阵对数的主值分支；采样数据不能唯一确定连续系统，高于奈奎斯特频率的动态无法据此恢复。",
    "连续纯延迟按最少整数采样延迟分离约定给出，不直接等于离散 nk；小于一个采样周期的延迟未单独估计。",
    "参数、噪声和采样时间误差会影响转换结果；在线输出表示当前参数快照。",
    "重新离散化误差只检查转换的数值一致性，不代表对真实对象的辨识精度。",
]


def _base(dt):
    return {"status": "unavailable", "method": "zoh_principal_logarithm", "sample_time": dt,
            "assumptions": list(ASSUMPTIONS), "reason": "", "transfer_function": None,
            "state_space": None, "roundtrip_relative_error": None}


def continuous_model(spec, theta):
    """Cache repeated snapshots, keeping mutable response objects independent.

    The dynamic order is at most 8, irrespective of a long discrete delay. The
    matrix log/exponential therefore cost O((na+2)^3) on matrices of size <=10.
    A 32-entry cache bounds memory and avoids recalculating unchanged snapshots.
    """
    if spec.kind != "arx":
        return {"status": "not_applicable", "method": None, "reason":
                "当前是特征回归模型，未定义动态状态及微分关系，不能自动推出连续时间动态模型。"}
    coefficients = tuple(float(v) for v in theta)
    return copy.deepcopy(_convert(spec.na, spec.nb, spec.nk, spec.intercept, spec.sample_time, coefficients))


@lru_cache(maxsize=32)
def _convert(na, nb, nk, intercept, dt, coefficients):
    """Find a real, numerically verified principal-ZOH equivalent or explain failure.

    With q=z^-1 and A(q)=1-a1*q-...-an*q^n, let m be the last nonzero
    numerator lag, l its first. The smallest delay removing artificial z=0
    poles is d=max(0,m-n). It is causal only when d<=l. This convention gives
    d=0 for b*z^-1/(1-a*z^-1), the usual ZOH image of a delay-free first-order
    strictly proper plant; blindly declaring nk*Ts a dead time would be wrong.

    Observable canonical form uses C=[1,0,...], companion A_d transposed,
    B_d=b[1:]+a*b[0], D=b[0]. A separate constant channel has g_d=a*c,h=c,
    realizing c/A(q). Converting [B_d,g_d] together preserves affine forcing
    even for integrators, where dividing c by A(1) would fail.
    """
    result = _base(dt)
    try:
        a = np.asarray(coefficients[:na], dtype=float)
        b = np.r_[np.zeros(nk), coefficients[na:na + nb]]
        c = coefficients[-1] if intercept else 0.
        if not np.isfinite(dt) or dt <= 0 or not np.all(np.isfinite(coefficients)):
            result["reason"] = "采样周期或模型参数无效，无法转换。"
            return result
        # Trim only exact structural zeros; never discard a small identified pole.
        active_a = np.flatnonzero(a != 0)
        order = int(active_a[-1] + 1) if len(active_a) else 0
        a = a[:order]
        active_b = np.flatnonzero(b != 0)
        first = int(active_b[0]) if len(active_b) else 0
        last = int(active_b[-1]) if len(active_b) else 0
        delay = max(0, last - order)
        if delay > first:
            result["reason"] = ("输入滞后结构包含不能分离为一个整体纯延迟的零极点。"
                                "当前 ZOH 方法无法给出可靠的有限维连续模型；离散辨识结果仍可使用。")
            return result
        delay_seconds = delay * dt
        result.update(delay_samples=delay, delay_seconds=delay_seconds, dynamic_order=order,
                      discrete_onset_lag_samples=nk)
        core = np.zeros(order + 1)
        if len(active_b):
            for lag in active_b:
                core[int(lag) - delay] = b[lag]
        if order == 0:
            result.update(status="available", roundtrip_relative_error=0., poles=[],
                          state_space={"A": [], "B": [], "C": [[]], "D": [[float(core[0])]],
                                       "g": [], "h": [[c]], "dimension": 0,
                                       "equations": ["y(t) = D_c u(t-tau) + h_c"],
                                       "state_definition": "无动态状态；tau 为下方给出的连续纯延迟。"},
                          transfer_function={"numerator": [float(core[0])], "denominator": [1.],
                                             "coefficient_order": "descending powers of s", "delay_seconds": delay_seconds})
            return result
        Ad = np.zeros((order, order))
        Ad[:, 0] = a
        for i in range(order - 1):
            Ad[i, i + 1] = 1.
        Bd = (core[1:] + a * core[0]).reshape(-1, 1)
        gd = (a * c).reshape(-1, 1)
        C = np.zeros((1, order))
        C[0, 0] = 1.
        D = np.array([[core[0]]])
        eigenvalues = np.linalg.eigvals(Ad)
        if np.min(np.abs(eigenvalues)) <= 1e-12 * max(1., np.max(np.abs(eigenvalues))):
            result["reason"] = "离散动态极点为零或过于接近零，矩阵对数转换数值不可靠，未生成连续模型。"
            return result
        if np.linalg.cond(Ad) > 1e12:
            result["reason"] = "离散动态矩阵条件数过大，连续转换可能严重放大误差，未生成连续模型。"
            return result
        forcing = np.column_stack((Bd, gd))
        scales = np.maximum(np.max(np.abs(forcing), axis=0), 1.)
        augmented = np.eye(order + 2)
        augmented[:order, :order] = Ad
        augmented[:order, order:] = forcing / scales
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            # SciPy < 1.17 exposed ``disp=False`` and returned its residual
            # estimate as a second value.  Newer versions removed that
            # argument, so calculate the same normalized reconstruction
            # residual explicitly.  This also keeps the acceptance threshold
            # independent of SciPy's warning/display API.
            logarithm = logm(augmented)
        estimated_error = (np.linalg.norm(expm(logarithm) - augmented, ord=1)
                           / max(np.linalg.norm(augmented, ord=1), np.finfo(float).tiny))
        if not np.all(np.isfinite(logarithm)) or not np.isfinite(estimated_error) or estimated_error > 1e-8:
            result["reason"] = "矩阵对数未达到数值精度要求，未生成连续模型。"
            return result
        imaginary = np.linalg.norm(np.imag(logarithm), ord=np.inf)
        if imaginary > 1e-9 * max(1., np.linalg.norm(np.real(logarithm), ord=np.inf)):
            result["reason"] = ("该离散模型的主值矩阵对数含显著复数（常见于负实极点）。"
                                "当前方法不支持可靠的实系数连续转换；这不等于不存在其他分支或更高阶连续实现。")
            return result
        logarithm = np.real(logarithm)
        Ac = logarithm[:order, :order] / dt
        Bc = logarithm[:order, order:order + 1] * scales[0] / dt
        gc = logarithm[:order, order + 1:order + 2] * scales[1] / dt
        # Independent round-trip on the UNROUNDED augmented system. Scaling is
        # retained to avoid a large input or offset hiding errors in A_d.
        generator = np.zeros_like(augmented)
        generator[:order, :order] = Ac
        generator[:order, order:] = np.column_stack((Bc, gc)) / scales
        reconstructed = expm(generator * dt)
        errors = [float(np.linalg.norm(reconstructed[:order, :order] - Ad, ord=np.inf) / max(1., np.linalg.norm(Ad, ord=np.inf)))]
        for i in range(2):
            expected = forcing[:, i] / scales[i]
            errors.append(float(np.linalg.norm(reconstructed[:order, order + i] - expected, ord=np.inf) / max(1., np.linalg.norm(expected, ord=np.inf))))
        error = max(errors)
        result["roundtrip_relative_error"] = error if np.isfinite(error) else None
        if not np.isfinite(error) or error > 1e-8:
            result["reason"] = "连续模型重新离散化后与原模型不一致，已拒绝该转换。"
            return result
        numerator, denominator = ss2tf(Ac, Bc, C, D)
        numerator = np.trim_zeros(numerator[0], "f")
        if not len(numerator):
            numerator = np.array([0.])
        numeric = [Ac, Bc, gc, C, D, numerator, denominator]
        if any(not np.all(np.isfinite(item)) or np.max(np.abs(item)) > 1e100 for item in numeric):
            result["reason"] = "连续模型系数超出安全数值范围，未生成连续模型。"
            return result
        poles = np.linalg.eigvals(Ac)
        result.update(status="available", poles=[{"real": float(p.real), "imag": float(p.imag)} for p in poles],
                      state_space={"A": Ac.tolist(), "B": Bc.tolist(), "C": C.tolist(), "D": D.tolist(),
                                   "g": gc.tolist(), "h": [[c]], "dimension": order,
                                   "equations": ["dx_c(t)/dt = A_c x_c(t) + B_c u(t-tau) + g_c",
                                                 "y(t) = C_c x_c(t) + D_c u(t-tau) + h_c"],
                                   "state_definition": "x_c 为连续内部状态，采用转换后的可观测规范形坐标，不是离散滞后向量或已知物理状态。"},
                      transfer_function={"numerator": numerator.tolist(), "denominator": denominator.tolist(),
                                         "coefficient_order": "descending powers of s", "delay_seconds": delay_seconds})
        return result
    except (ValueError, np.linalg.LinAlgError, OverflowError, FloatingPointError, ZeroDivisionError):
        # Conversion is explanatory output; it must never roll back valid RLS data.
        result["reason"] = "连续转换遇到数值问题，未生成连续模型；离散模型与已接收数据仍然保留。"
        return result
