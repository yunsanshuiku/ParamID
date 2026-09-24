"""Automatic tabular MIMO transfer-matrix identification.

Each output is fitted jointly against ALL input channels, with its own common
denominator. This is a MIMO output-error model, not N4SID and not an innovations
noise model. ARX/SVD supplies initial estimates; nonlinear least squares then
minimizes simulated output error, including unknown initial filter states.
Independent final validation never participates in scaling, fitting or order
selection. Input/output normalization is inverted in the exported equations.
"""

import csv
import io
import warnings

import numpy as np
from scipy.linalg import block_diag
from scipy.optimize import least_squares
from scipy.signal import lfilter, lfiltic

from .core import IdentificationError, ModelSpec, finite_number
from .data import resolve_channels, parse_csv
from .representation import mathematical_model
from .frequency import fit_frequency, frequency_config, rescale_spectrum

MAX_ORDER, MAX_DELAY, MAX_CHANNELS = 6, 4, 8
HISTORY = MAX_ORDER + MAX_DELAY - 1


def _measure(y, prediction):
    """Metrics in this output's physical units; no aggregation across units."""
    residual = y - prediction
    norm = np.linalg.norm(y - np.mean(y))
    return {"rmse": float(np.sqrt(np.mean(residual ** 2))),
            "fit_percent": float(100 * (1 - np.linalg.norm(residual) / norm)) if norm > 1e-12 * max(1., np.linalg.norm(y)) else None}


def _arrays(rows, channels, dt):
    names = channels["inputs"] + channels["outputs"]
    values = np.empty((len(rows), len(names)))
    for k, row in enumerate(rows):
        for j, name in enumerate(names):
            try:
                values[k, j] = finite_number(row.get(name), name)
            except IdentificationError as exc:
                raise IdentificationError(f"第 {k + 1} 个样本，{exc}") from exc
    if channels["time"]:
        times = np.asarray([finite_number(row.get(channels["time"]), channels["time"]) for row in rows])
        if len(times) > 1 and np.any(np.abs(np.diff(times) - dt) > max(1e-10, .01 * dt)):
            raise IdentificationError("验证与训练时间列必须严格递增，且采样周期一致（允许 1% 偏差）。")
    else:
        times = np.arange(len(rows)) * dt
    return values[:, :len(channels["inputs"])], values[:, len(channels["inputs"]):], times


def _drive(u, theta, na, nb, nk, intercept):
    """Construct B(q)u+c with q=z^-1; no numerical differentiation of outputs."""
    force = np.full(len(u), theta[-1] if intercept else 0.)
    for j in range(u.shape[1]):
        force += lfilter(np.r_[np.zeros(nk), theta[na + j * nb:na + (j + 1) * nb]], [1.], u[:, j])
    return force


def _simulate(u, y_history, theta, na, nb, nk, intercept, start):
    """Warm up ONLY with observations preceding the scored block.

    Forcing uses the actual preceding input history; subsequent outputs are
    entirely simulated. This is free-run validation, not one-step prediction.
    """
    denominator = np.r_[1., -theta[:na]]
    initial = lfiltic([1.], denominator, y_history[max(0, start-na):start][::-1])
    with np.errstate(over="ignore", invalid="ignore"):
        predicted = lfilter([1.], denominator, _drive(u, theta, na, nb, nk, intercept)[start:], zi=initial)[0]
    if not np.all(np.isfinite(predicted)) or np.max(np.abs(predicted), initial=0.) > 1e50:
        raise IdentificationError("候选自由运行发散。")
    return predicted


def _refine(u, y, seed, intercept):
    """Bounded OE optimization with jointly estimated unknown initial states.

    The loss uses at most 4000 deterministically spaced training residuals;
    every input sample still participates in simulation (no time decimation).
    SISO/MISO row coefficients and filter states are optimized together. This
    improves output-measurement-noise handling but is not unbiased for arbitrary
    process noise, noisy inputs or closed-loop correlation.
    """
    na, nb, nk = (seed[key] for key in ("na", "nb", "nk"))
    theta = seed["theta"]
    initial = lfiltic([1.], np.r_[1., -theta[:na]], np.repeat(y[0], na))
    indices = np.unique(np.linspace(0, len(y)-1, min(len(y), 4000), dtype=int))
    def residual(parameters):
        with np.errstate(over="ignore", invalid="ignore"):
            pred = lfilter([1.], np.r_[1., -parameters[:na]],
                           _drive(u, parameters[:-na], na, nb, nk, intercept), zi=parameters[-na:])[0]
        error = pred[indices] - y[indices]
        return np.clip(np.nan_to_num(error, nan=1e30, posinf=1e30, neginf=-1e30), -1e30, 1e30)
    start = np.r_[theta, initial]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        optimized = least_squares(residual, start, method="trf", x_scale="jac", max_nfev=70,
                                  ftol=1e-7, xtol=1e-7, gtol=1e-7)
    improved = np.linalg.norm(residual(optimized.x)) < np.linalg.norm(residual(start))
    return {**seed, "theta": optimized.x[:-na] if improved else theta,
            "refined": bool(improved), "optimizer_converged": bool(optimized.success),
            "evaluations": int(optimized.nfev)}


def _fit_output(u, y, train_end, select_end, intercept):
    """Search a finite model family, then refine one candidate per output order.

    A thin QR of the maximal training regressor avoids repeatedly processing
    the entire record for every structure. Singular-value rank and condition
    checks reject indistinguishable input parameters instead of fabricating a
    transfer matrix. The common denominator is per OUTPUT, not per channel pair.
    """
    t = np.arange(HISTORY, select_end)
    max_lag = MAX_ORDER + MAX_DELAY
    columns = [y[t-i] for i in range(1, MAX_ORDER+1)]
    for j in range(u.shape[1]):
        columns.extend(u[t-i, j] for i in range(max_lag))
    if intercept:
        columns.append(np.ones(len(t)))
    design = np.column_stack(columns)
    count = train_end-HISTORY
    scales = np.maximum(np.linalg.norm(design[:count], axis=0) / np.sqrt(count), 1e-12)
    q, r = np.linalg.qr(design[:count]/scales, mode="reduced")
    target = q.T @ y[HISTORY:train_end]
    candidates = []
    for na in range(1, MAX_ORDER+1):
        for nb in range(1, MAX_ORDER+1):
            for nk in range(MAX_DELAY+1):
                selected = list(range(na))
                for j in range(u.shape[1]):
                    selected += list(range(MAX_ORDER+j*max_lag+nk, MAX_ORDER+j*max_lag+nk+nb))
                if intercept:
                    selected.append(design.shape[1]-1)
                if count < max(40, 4*len(selected)):
                    continue
                compact = r[:, selected]
                solution, _, rank, sv = np.linalg.lstsq(compact, target, rcond=None)
                condition = float(sv[0]/sv[-1]) if sv[-1] > 0 else float("inf")
                if rank < len(selected) or condition > 1e8:
                    continue
                theta = solution/scales[selected]
                try:
                    pred = _simulate(u[:select_end], y, theta, na, nb, nk, intercept, train_end)
                    error = float(np.sqrt(np.mean((pred-y[train_end:select_end])**2)))
                except IdentificationError:
                    continue
                candidates.append({"na": na, "nb": nb, "nk": nk, "parameters": len(theta),
                                   "theta": theta, "selection_rmse": error, "condition": condition,
                                   "refined": False, "optimizer_converged": None})
    if not candidates:
        raise IdentificationError("输入滞后列无法独立区分，或样本不足。请保留输入变化前后的数据，并使各输入分别变化。")
    # Each denominator order gets a chance at output-error refinement; final
    # holdout observations are absent from both the seed and optimization.
    refined = []
    for na in range(1, MAX_ORDER+1):
        seeds = [item for item in candidates if item["na"] == na]
        if not seeds:
            continue
        count_valid = select_end-train_end
        seed = min(seeds, key=lambda item: (np.log(max(item["selection_rmse"]**2, 1e-18))
                                           + item["parameters"]*np.log(count_valid)/count_valid))
        try:
            candidate = _refine(u[:train_end], y[:train_end], seed, intercept)
            pred = _simulate(u[:select_end], y, candidate["theta"], candidate["na"], candidate["nb"], candidate["nk"], intercept, train_end)
            candidate["selection_rmse"] = float(np.sqrt(np.mean((pred-y[train_end:select_end])**2)))
            refined.append(candidate)
        except (ValueError, np.linalg.LinAlgError, IdentificationError):
            pass
    candidates += refined
    # A BIC-style complexity penalty on held-out simulation loss discourages
    # high-order models which improve only the validation noise realization.
    # This is a selection heuristic, not a likelihood-based confidence bound.
    validation_count = select_end-train_end
    for item in candidates:
        item["selection_score"] = float(np.log(max(item["selection_rmse"]**2, 1e-18))
                                        + item["parameters"]*np.log(validation_count)/validation_count)
    chosen = min(candidates, key=lambda item: (item["selection_score"], item["parameters"]))
    chosen = dict(chosen)
    chosen["refined_candidates"] = sum(item["refined"] for item in refined)
    chosen["candidates"] = [{key: value for key, value in item.items() if key != "theta"}
                             for item in sorted(candidates, key=lambda item: item["selection_score"])[:12]]
    return chosen


def _representation(channels, rows, dt):
    """Build all p*m transfer paths and one affine discrete state realization.

    Observable companion coordinates share output dynamics across all inputs
    within one row; block diagonals combine output rows. This realization is
    equivalent but not guaranteed minimal, nor are states physical coordinates.
    """
    m, p = len(channels["inputs"]), len(channels["outputs"])
    blocks, bs, cs, gs, direct, offsets, paths, equations = [], [], [], [], [], [], [], []
    for i, row in enumerate(rows):
        na, nb, nk = row["na"], row["nb"], row["nk"]
        a, b, offset = np.asarray(row["a"]), np.asarray(row["b"]), row["offset"]
        n = max(na, nk+nb-1)
        a_extended = np.pad(a, (0, n-na))
        A = np.zeros((n, n)); A[:, 0] = a_extended
        A[np.arange(n-1), np.arange(1, n)] = 1.
        B = np.zeros((n, m)); D = np.zeros(m)
        C = np.zeros((p, n)); C[i, 0] = 1.
        pair_row = []
        input_terms = []
        for j, input_name in enumerate(channels["inputs"]):
            numerator = np.pad(b[j], (nk, n+1-nk-nb))
            B[:, j] = numerator[1:] + a_extended*numerator[0]
            D[j] = numerator[0]
            spec = ModelSpec(kind="arx", input=input_name, output=channels["outputs"][i],
                             sample_time=dt, na=na, nb=nb, nk=nk, intercept=False)
            pair = mathematical_model(spec, np.r_[a, b[j]])
            pair_row.append({"input": input_name, "output": channels["outputs"][i],
                             "transfer_function": pair["transfer_function"], "continuous_model": pair["continuous_model"]})
            input_terms.extend(f"({value:.9g}) {input_name}[k-{nk+l}]" for l, value in enumerate(b[j]))
        output_terms = [f"({value:.9g}) {channels['outputs'][i]}[k-{lag+1}]" for lag, value in enumerate(a)]
        equations.append(f"{channels['outputs'][i]}[k] = " + " + ".join(output_terms+input_terms+[f"({offset:.9g})"]))
        blocks.append(A); bs.append(B); cs.append(C); gs.append((a_extended*offset).reshape(-1, 1))
        direct.append(D); offsets.append([offset]); paths.append(pair_row)
    ss = {"A": block_diag(*blocks).tolist(), "B": np.vstack(bs).tolist(), "C": np.hstack(cs).tolist(),
          "D": np.asarray(direct).tolist(), "g": np.vstack(gs).tolist(), "h": offsets,
          "dimension": sum(len(block) for block in blocks),
          "equations": ["x[k+1] = A x[k] + B u[k] + g", "y[k] = C x[k] + D u[k] + h"],
          "state_definition": "按输出组合的可观测规范形；保留输入延迟状态，不保证最小实现。状态不是唯一物理参数。"}
    lines = ["多通道离散模型", f"Ts = {dt:.12g} s", "输入顺序："+", ".join(channels["inputs"]),
             "输出顺序："+", ".join(channels["outputs"]), *equations, "", "各输入到各输出的传递函数（其他输入置零；偏置单独表示）"]
    for pair_row in paths:
        for pair in pair_row:
            lines.append(f"{pair['input']} → {pair['output']}: {pair['transfer_function']['text']}")
            cont = pair["continuous_model"]
            lines.append("连续："+(cont["transfer_function"]["text"] if cont["status"] == "available" else cont["reason"]))
    for key in ("A", "B", "C", "D", "g", "h"):
        lines.append(f"{key} = {ss[key]}")
    return {"kind": "multichannel", "sample_time": dt, "equations": equations, "transfer_matrix": paths,
            "state_space": ss, "text": "\n".join(lines),
            "notes": ["所有输入共同参与每一路输出辨识；编号不表示一一配对。",
                      "连续传递函数是各通道的 ZOH 等效模型，不能唯一恢复真实连续系统；不是直接连续时间辨识。",
                      "各输出分别使用共同分母；未建模多输出噪声相关性，不是子空间辨识或完整噪声模型 PEM。"]}


def offline_multichannel(payload):
    """Identify original I/O records with automatic or manual channel selection.

    Training-only normalization preserves physical coefficients and offsets on
    export. All samples retain their time coordinates; no temporal differences
    or zero-row removal participate in fitting, selection or validation.
    """
    from .service import reject_removed_filter
    reject_removed_filter(payload)
    spectral = payload.get("algorithm") == "frequency"
    frequency_settings = frequency_config(payload.get("frequency")) if spectral else None
    headers, rows = parse_csv(payload.get("csv", ""), payload.get("header_mode", "auto"))
    config = payload.get("model", {})
    channels = resolve_channels(headers, rows, config)
    if not channels["detected"]:
        raise IdentificationError("请按 input1 / output1 表头自动识别，或手动选择输入、输出和时间列。")
    if channels["errors"]:
        raise IdentificationError(" ".join(channels["errors"]))
    if max(len(channels["inputs"]), len(channels["outputs"])) > MAX_CHANNELS:
        raise IdentificationError("多通道模式每次最多支持 8 路输入与 8 路输出。")
    dt = channels["sample_time"]
    if dt is None:
        dt = finite_number(config.get("sample_time"), "无时间列时的采样周期")
    if dt <= 0:
        raise IdentificationError("采样周期必须为正。")
    intercept = config.get("intercept", True)
    if not isinstance(intercept, bool):
        raise IdentificationError("intercept 必须为布尔值。")
    if payload.get("weight_column") or payload.get("algorithm", "oe") not in ("oe", "auto", "frequency"):
        raise IdentificationError("多通道模式使用联合 ARX 初始化与输出误差精修；不接受旧的 LS/WLS/Ridge 设置。")
    u, y, times = _arrays(rows, channels, dt)
    independent = bool(payload.get("validation_csv"))
    train_end, select_end = int(len(rows)*(.7 if independent else .6)), len(rows) if independent else int(len(rows)*.8)
    if train_end < 100 or select_end-train_end < 20 or (not independent and len(rows)-select_end < 20):
        raise IdentificationError("多通道自动辨识至少需要 170 点（独立验证时训练文件至少 150 点）。")
    um, ym = (u[:train_end].mean(axis=0), y[:train_end].mean(axis=0)) if intercept else (np.zeros(u.shape[1]), np.zeros(y.shape[1]))
    us, ys = np.std(u[:train_end], axis=0), np.std(y[:train_end], axis=0)
    for j, scale in enumerate(us):
        if scale <= 1e-12*max(1., np.max(np.abs(u[:train_end, j]))):
            raise IdentificationError(f"{channels['inputs'][j]} 在训练段没有有效变化，无法辨识该输入通道；请保留阶跃前基线或补充变化。")
    centered_inputs = (u[:train_end]-u[:train_end].mean(axis=0))/us
    if np.linalg.matrix_rank(centered_inputs) < u.shape[1] or np.linalg.cond(centered_inputs) > 1e8:
        raise IdentificationError("输入通道互相成比例或高度相关，无法分别辨识各通道；请让各输入独立变化或错开阶跃。")
    if np.any(ys <= 1e-12*np.maximum(1., np.max(np.abs(y[:train_end]), axis=0))):
        raise IdentificationError("至少一路输出在训练段没有有效变化，无法自动确定动态模型。")
    un, yn = (u-um)/us, (y-ym)/ys
    if independent:
        valid_headers, valid_rows = parse_csv(payload["validation_csv"], payload.get("validation_header_mode", "auto"))
        if any(name not in valid_headers for name in channels["inputs"]+channels["outputs"]+([channels["time"]] if channels["time"] else [])):
            raise IdentificationError("独立验证文件必须包含训练文件的全部输入、输出和时间列。")
        vu, vy, vt = _arrays(valid_rows, channels, dt)
        valid_start = 16 if spectral else HISTORY
        if len(vy)-valid_start < 20:
            raise IdentificationError("独立验证文件在历史预热后至少需要 20 点。")
    else:
        vu, vy, vt, valid_start = u, y, times, select_end
    warnings_out = ["输出误差法适用于确定性动态与输出测量噪声；有过程噪声、输入噪声或闭环反馈时仍可能有偏。",
                    "自动阶次限定 na=1–6、nb=1–6、公共起始延迟 nk=0–4；各输入可通过前导零系数表达不同延迟。",
                    "选模依据为留出自由运行误差加复杂度惩罚；推荐结构不是实际物理阶数的唯一证明。"]
    if np.max(np.abs(np.diff(un[train_end:select_end], axis=0)), initial=0.) < 1e-6:
        warnings_out.append("选模段没有输入变化，主要检验稳态延续，不能据此证明动态阶次或零点准确；建议补充多次阶跃或独立动态实验。")
    output_models, output_results, parameters, selections = [], [], [], []
    if spectral:
        warnings_out = ["窗口频域：Welch H1 联合输入谱估计 + 稳定有理传递函数拟合。",
                        "适用于稳定线性系统、准确输入和与输入不相关的输出噪声；输入噪声及闭环反馈仍可能导致偏差。",
                        "分窗有泄漏与分辨率误差；选阶搜索 na=1–6、nb=1–7、nk=0–10，最终验证未参与拟合或选阶。"]
    for i, name in enumerate(channels["outputs"]):
        try:
            chosen = (fit_frequency(un[:select_end], yn[:select_end, i], train_end, select_end, intercept, frequency_settings, dt=dt)
                      if spectral else _fit_output(un[:select_end], yn[:select_end, i], train_end, select_end, intercept))
        except IdentificationError as exc:
            raise IdentificationError(f"{name}：{exc}") from exc
        na, nb, nk = (chosen[key] for key in ("na", "nb", "nk"))
        a = chosen["theta"][:na]
        b = chosen["theta"][na:na+u.shape[1]*nb].reshape(u.shape[1], nb)*ys[i]/us[:, None]
        c = float(ym[i]*(1-a.sum()) - np.sum(b.sum(axis=1)*um) + (ys[i]*chosen["theta"][-1] if intercept else 0.))
        theta = np.r_[a, b.ravel(), c] if intercept else np.r_[a, b.ravel()]
        pred = _simulate(vu, vy[:, i], theta, na, nb, nk, intercept, valid_start)
        model = {"output": name, "na": na, "nb": nb, "nk": nk, "a": a.tolist(), "b": b.tolist(),
                 "offset": c, "refined": chosen["refined"], "optimizer_converged": chosen["optimizer_converged"]}
        output_models.append(model)
        measure = _measure(vy[valid_start:, i], pred)
        idx = np.unique(np.linspace(valid_start, len(vy)-1, min(1500, len(vy)-valid_start), dtype=int))
        series = [{"time": float(vt[k]), "measured": float(vy[k, i]), "simulation": float(pred[k-valid_start])} for k in idx]
        output_results.append({"output": name, "metrics": measure, "series": series, "prediction": pred})
        selections.append({"output": name, **{key: chosen[key] for key in ("na", "nb", "nk", "parameters", "condition", "refined", "refined_candidates", "candidates")},
                           "selection_rmse": chosen["selection_rmse"]*ys[i]})
        for lag, value in enumerate(a):
            parameters.append({"name": f"{name}.a{lag+1}", "value": float(value)})
        for j, input_name in enumerate(channels["inputs"]):
            for lag, value in enumerate(b[j]):
                parameters.append({"name": f"{name}←{input_name}.b{lag}", "value": float(value)})
        if intercept:
            parameters.append({"name": f"{name}.offset", "value": c})
        if max(abs(np.roots(np.r_[1., -a]))) >= 1:
            warnings_out.append(f"{name} 的模型含单位圆上或外的极点，请结合积分、不稳定对象或模型失配解释。")
        if spectral:
            rescale_spectrum(chosen["frequency"], ys[i], us)
            output_results[-1]["frequency"] = chosen["frequency"]
            if chosen["frequency"]["mean_coherence"] < .6:
                warnings_out.append(f"{name} 平均相干度偏低，请检查激励频段、窗长、噪声和线性假设。")
        if not spectral and not chosen["refined"]:
            warnings_out.append(f"{name}：选模保留了 ARX 初始解，输出误差精修未提供更合适的候选。")
        elif not spectral and not chosen["optimizer_converged"]:
            warnings_out.append(f"{name}：输出误差精修达到迭代上限，保留已改善的候选，未宣称优化完全收敛。")
    math_model = _representation(channels, output_models, dt)
    buf = io.StringIO(newline=""); writer = csv.writer(buf)
    suffixes = ("measured", "free_run", "residual")
    writer.writerow(["time_s"]+[f"{name}_{suffix}" for name in channels["outputs"] for suffix in suffixes])
    for k in range(len(vy)-valid_start):
        row = [vt[valid_start+k]]
        for i, result in enumerate(output_results):
            value, predicted = vy[valid_start+k, i], result["prediction"][k]
            row += [value, predicted, value-predicted]
        writer.writerow(row)
    for item in output_results:
        item.pop("prediction")
    report = ["# 多通道参数辨识报告", "", f"输入：{', '.join(channels['inputs'])}；输出：{', '.join(channels['outputs'])}",
              "算法：窗口频域 H1 + 稳定有理模型拟合。" if spectral else "算法：各输出的多输入 ARX 初始化 + 输出误差候选精修。",
              "拟合、选模与最终验证均使用原始输入输出信号。",
              f"训练 {train_end} 点；选模 {select_end-train_end} 点；最终验证 {len(vy)-valid_start} 点。", "",
              *[f"{item['output']}：自由运行 RMSE={item['metrics']['rmse']:.9g}" for item in output_results],
              "", math_model["text"], "", *math_model["notes"], "", *warnings_out]
    return {"version": "0.7.0", "mode": "offline", "algorithm": "frequency" if spectral else "arx_oe", "channels": channels,
            "model": {"kind": "multichannel", "inputs": channels["inputs"], "outputs": channels["outputs"],
                      "sample_time": dt, "intercept": intercept, "output_models": output_models},
            "parameters": parameters, "output_results": output_results, "output_selections": selections,
            "train_samples": train_end, "selection_samples": select_end-train_end, "validation_samples": len(vy)-valid_start,
            "validation_source": "独立文件" if independent else "按时间连续留出", "warnings": warnings_out,
            "mathematical_model": math_model, "csv_export": buf.getvalue(), "report": "\n".join(report)}
