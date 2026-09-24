"""Automatic ARX structure selection using disjoint chronological data windows.

All candidates use identical raw training/selection indices. A shared QR
factorization reduces each candidate fit to a small least-squares problem without
forming X.T @ X. Free-run prediction is evaluated only on the selection window;
the final validation window is never used to select structure or scale features.
"""

from dataclasses import replace

import numpy as np
from scipy.signal import lfiltic, lfilter

from .core import IdentificationError, ModelSpec, ModelStream, finite_number, resolve_offline_algorithm

MAX_NA = 8
MAX_NB = 8
MAX_NK = 10
COMMON_HISTORY = max(MAX_NA, MAX_NK + MAX_NB - 1)
RELATIVE_TOLERANCE = 0.02


def select_arx_order(rows: list[dict], spec: ModelSpec, config: dict,
                     reserve_validation: bool = True) -> dict:
    """Recommend (na, nb, nk), preferring parsimony within 2% of best free-run RMSE.

    With reserve_validation, effective samples are split 60/20/20; otherwise
    70/30 is used for training/selection (independent validation or online initial
    calibration). Coefficients remain fitted to the training window only.
    A 1e-10 relative-output floor handles numerical ties on noiseless data.
    This validation rule is not a claim of globally optimal or true physical order.
    """
    if spec.kind != "arx":
        raise IdentificationError("自动定阶仅适用于 ARX 动态模型。")
    if len(rows) < 120:
        raise IdentificationError("自动定阶至少需要 120 个等间隔样本，请增加实验数据。")
    algorithm, weight_column = resolve_offline_algorithm(config.get("algorithm", "ls"), config.get("weight_column"))
    alpha = finite_number(config.get("alpha", 1), "岭参数 alpha") if algorithm == "ridge" else 0.0
    if algorithm == "ridge" and alpha <= 0:
        raise IdentificationError("岭参数 alpha 必须为正。")
    count = len(rows) - COMMON_HISTORY
    train_count = int(count * (0.6 if reserve_validation else 0.7))
    train_end = COMMON_HISTORY + train_count
    selection_end = COMMON_HISTORY + int(count * 0.8) if reserve_validation else len(rows)
    selection_count = selection_end - train_end

    # Only read the training/selection prefix here. Final validation values cannot
    # influence scales, rank, candidate coefficients, the tie threshold, or order.
    validator = ModelStream(spec)
    numeric = []
    for index, row in enumerate(rows[:selection_end]):
        try:
            validator.step(row)
        except IdentificationError as exc:
            raise IdentificationError(f"定阶数据第 {index + 1} 个样本：{exc}") from exc
        numeric.append(validator.rows[-1])
    output = np.asarray([row[spec.output] for row in numeric])
    input_signal = np.asarray([row[spec.input] for row in numeric])
    indices = np.arange(COMMON_HISTORY, selection_end)
    columns = [output[indices - lag] for lag in range(1, MAX_NA + 1)]
    columns += [input_signal[indices - lag] for lag in range(MAX_NK + MAX_NB)]
    if spec.intercept:
        columns.append(np.ones(len(indices)))
    design = np.column_stack(columns)
    x_train, x_selection = design[:train_count], design[train_count:]
    y_train, y_selection = output[COMMON_HISTORY:train_end], output[train_end:selection_end]
    scales = np.linalg.norm(x_train, axis=0) / np.sqrt(train_count)
    scales[scales == 0] = 1.0
    normalized = x_train / scales
    target = y_train.copy()
    if algorithm == "wls":
        weights = np.asarray([finite_number(row.get(weight_column), "权重列")
                              for row in rows[COMMON_HISTORY:train_end]])
        if np.any(weights <= 0):
            raise IdentificationError("WLS 的训练权重必须为正。")
        root_weight = np.sqrt(weights / np.mean(weights))
        normalized = normalized * root_weight[:, None]
        target = target * root_weight
    q_matrix, r_matrix = np.linalg.qr(normalized, mode="reduced")
    projected_target = q_matrix.T @ target

    candidates, fitted = [], {}
    for na in range(1, MAX_NA + 1):
        for nb in range(1, MAX_NB + 1):
            for nk in range(MAX_NK + 1):
                candidate = {"na": na, "nb": nb, "nk": nk, "parameters": na + nb + int(spec.intercept)}
                selected_columns = list(range(na)) + list(range(MAX_NA + nk, MAX_NA + nk + nb))
                if spec.intercept:
                    selected_columns.append(design.shape[1] - 1)
                try:
                    p = len(selected_columns)
                    if train_count < max(30, 3 * p):
                        raise IdentificationError("该候选的训练数据不足。")
                    small_design = r_matrix[:, selected_columns]
                    singular = np.linalg.svd(small_design, compute_uv=False)
                    tolerance = max(train_count, p) * np.finfo(float).eps * singular[0]
                    rank = int(np.count_nonzero(singular > tolerance))
                    if rank < p:
                        raise IdentificationError("回归矩阵秩不足。")
                    condition = float(singular[0] / singular[-1])
                    if condition > 1e10:
                        raise IdentificationError("缩放后条件数超过 1e10。")
                    small_target = projected_target
                    if algorithm == "ridge":
                        penalty = np.eye(p) * np.sqrt(alpha)
                        if spec.intercept:
                            penalty[-1, -1] = 0.0
                        small_design = np.vstack((small_design, penalty))
                        small_target = np.r_[projected_target, np.zeros(p)]
                    theta = np.linalg.lstsq(small_design, small_target, rcond=None)[0] / scales[selected_columns]
                    x_selected = x_selection[:, selected_columns]
                    denominator = np.r_[1.0, -theta[:na]]
                    initial = lfiltic([1.0], denominator, output[train_end - na:train_end][::-1])
                    with np.errstate(over="ignore", invalid="ignore"):
                        forcing = x_selected[:, na:] @ theta[na:]
                        simulation, _ = lfilter([1.0], denominator, forcing, zi=initial)
                        prediction = x_selected @ theta
                    if (not np.all(np.isfinite(simulation)) or np.max(np.abs(simulation)) > 1e100 or
                            not np.all(np.isfinite(prediction)) or not np.all(np.isfinite(theta))):
                        raise IdentificationError("选模段仿真超出数值范围。")
                    candidate.update(status="valid", selection_rmse=float(np.linalg.norm(y_selection - simulation) / np.sqrt(selection_count)),
                                     one_step_rmse=float(np.linalg.norm(y_selection - prediction) / np.sqrt(selection_count)),
                                     condition=condition)
                    fitted[(na, nb, nk)] = {"theta": theta, "scales": scales[selected_columns],
                        "diagnostics": {"rank": rank, "parameters": p, "condition": condition}, "warnings": []}
                except (IdentificationError, np.linalg.LinAlgError) as exc:
                    candidate.update(status="rejected", reason=str(exc))
                candidates.append(candidate)

    valid = [candidate for candidate in candidates if candidate["status"] == "valid"]
    if not valid:
        raise IdentificationError("自动定阶未找到可用模型：请检查输入激励、常值/相关通道及数据长度。")
    best_rmse = min(candidate["selection_rmse"] for candidate in valid)
    output_scale = max(float(np.linalg.norm(y_selection) / np.sqrt(selection_count)), 1e-12)
    threshold = best_rmse + max(RELATIVE_TOLERANCE * best_rmse, output_scale * 1e-10)
    shortlist = [candidate for candidate in valid if candidate["selection_rmse"] <= threshold]
    chosen = min(shortlist, key=lambda candidate: (candidate["parameters"], candidate["selection_rmse"],
                                                  candidate["na"], candidate["nb"], candidate["nk"]))
    for candidate in candidates:
        candidate["selected"] = candidate is chosen
        candidate["within_tolerance"] = candidate.get("selection_rmse", float("inf")) <= threshold
    candidates.sort(key=lambda candidate: (not candidate["selected"], candidate["status"] != "valid",
                                           candidate.get("selection_rmse", float("inf")), candidate["parameters"]))
    warnings = ["推荐阶次限定于当前候选范围和实验工况；不等同于真实系统阶数的唯一证明。"]
    if chosen["na"] == MAX_NA or chosen["nb"] == MAX_NB or chosen["nk"] == MAX_NK:
        warnings.append("推荐结构触及搜索上限，当前范围外可能仍有更合适的模型。")
    summary = {"mode": "auto", "status": "selected", "criterion": "选模段自由运行 RMSE；误差在最小值 2% 内优先更少参数",
        "search": {"na": [1, MAX_NA], "nb": [1, MAX_NB], "nk": [0, MAX_NK]},
        "candidate_count": len(candidates), "valid_count": len(valid), "rejected_count": len(candidates) - len(valid),
        "selected": {key: chosen[key] for key in ("na", "nb", "nk", "parameters", "selection_rmse", "one_step_rmse")},
        "best_selection_rmse": best_rmse, "tie_threshold": threshold, "relative_tolerance": RELATIVE_TOLERANCE,
        "common_history": COMMON_HISTORY, "train_samples": train_count, "selection_samples": selection_count,
        "train_start": COMMON_HISTORY, "train_end": train_end, "selection_end": selection_end,
        "candidates": candidates, "warnings": warnings}
    return {"spec": replace(spec, na=chosen["na"], nb=chosen["nb"], nk=chosen["nk"]),
            "fit": fitted[(chosen["na"], chosen["nb"], chosen["nk"])], "summary": summary}
