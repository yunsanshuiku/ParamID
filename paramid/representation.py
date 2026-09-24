"""Readable mathematical models derived from the estimator's physical coefficients.

ARX uses positive output coefficients in this project: A(z^-1) therefore has
coefficients [1, -a1, ...]. Input delay is retained explicitly in the numerator.
The state realization uses measured-output/input lags, never a guessed physical
state, and preserves affine offsets separately from the input transfer function.
"""

import numpy as np

from .core import IdentificationError, ModelSpec
from .continuous import continuous_model


def number(value):
    """Round only presentation; numerical exports retain full precision."""
    return "0" if value == 0 else f"{value:.8g}"


def expression(terms, latex=False):
    """Format signed sums, retaining small nonzero coefficients and avoiding '+ -'."""
    parts = []
    for term in terms:
        value = term["coefficient"]
        if value == 0:
            continue
        sign = ("-" if value < 0 else "") if not parts else (" - " if value < 0 else " + ")
        magnitude = number(abs(value))
        if latex and "e" in magnitude:
            mantissa, exponent = magnitude.split("e")
            magnitude = mantissa + r"\times 10^{" + str(int(exponent)) + "}"
        symbol, lag = term.get("symbol", ""), term.get("lag", 0)
        if symbol == "z":
            variable = (f"z^{{-{lag}}}" if latex else f"z^(-{lag})") if lag else ""
        elif symbol == "s":
            power = term.get("power", 0)
            variable = "s" if power == 1 else ((f"s^{{{power}}}" if latex else f"s^{power}") if power else "")
        elif symbol:
            variable = (rf"\varphi_{{{term['feature']}}}" if latex else f"phi_{term['feature']}") if symbol == "phi" else symbol
            variable += "[k]" if lag == 0 else f"[k-{lag}]"
        else:
            variable = ""
        parts.append(sign + magnitude + ((r"\," if latex else " ") + variable if variable else ""))
    return "".join(parts) or "0"


def matrix_text(matrix):
    return "[" + "\n ".join("[" + ", ".join(number(v) for v in row) + "]" for row in matrix) + "]"


def continuous_representation(spec, values):
    """Add human-readable polynomials while preserving exact conversion metadata."""
    converted = continuous_model(spec, values)
    lines = ["连续时间等效模型（由离散模型按零阶保持转换）"]
    if converted["status"] == "available":
        tf, ss = converted["transfer_function"], converted["state_space"]
        for name in ("numerator", "denominator"):
            tf[name + "_terms"] = [{"coefficient": value, "symbol": "s", "power": len(tf[name]) - i - 1}
                                  for i, value in enumerate(tf[name])]
        tau = converted["delay_seconds"]
        tf["text"] = f"G_c(s) = ({expression(tf['numerator_terms'])}) / ({expression(tf['denominator_terms'])})"
        tf["latex"] = r"G_c(s)=\frac{" + expression(tf["numerator_terms"], True) + "}{" + expression(tf["denominator_terms"], True) + "}"
        if tau:
            tf["text"] += f" exp(-{number(tau)} s)"
            tf["latex"] += r"\,e^{-" + number(tau) + "s}"
        lines += [tf["text"], f"连续纯延迟 tau = {number(tau)} s；采样周期 Ts = {number(spec.sample_time)} s",
                  f"重新离散化一致性误差 = {number(converted['roundtrip_relative_error'])}",
                  "", *ss["equations"], ss["state_definition"]]
        for name in ("A", "B", "C", "D", "g", "h"):
            lines += [name + "_c =", matrix_text(ss[name])]
    else:
        lines.append(converted["reason"])
    lines += ["", *converted.get("assumptions", [])]
    converted["text"] = "\n".join(lines)
    return converted


def mathematical_model(spec: ModelSpec, theta) -> dict | None:
    """Build equivalent equations without estimating or altering parameters.

    For ARX let L=max(0,nk+nb-1), x[k]=[y[k-1..k-na],u[k-1..k-L]].
    Then y[k]=C*x[k]+D*u[k]+h. The next state's first row is this output;
    remaining rows shift the lag registers. A,B,g follow directly. This affine
    realization is causal for nk=0 as well, and need not be minimal.
    Cost is O((na+L)^2); current model limits bound na+L to 65.
    """
    if theta is None:
        return None
    values = np.asarray(theta, dtype=float)
    if values.shape != (len(spec.names),) or not np.all(np.isfinite(values)):
        raise IdentificationError("数学模型的参数维度或数值无效。")
    values = values.tolist()
    offset = values[-1] if spec.intercept else 0.
    terms, signals = [], [{"symbol": "y[k]", "column": spec.output, "role": "output"}]
    result = {"kind": spec.kind, "sample_time": spec.sample_time, "offset": offset,
              "transfer_function": None, "state_space": None,
              "notes": ["公式显示 8 位有效数字；JSON 数值系数保留完整精度。"]}
    if spec.kind == "regression":
        for i, feature in enumerate(spec.features):
            terms.append({"coefficient": values[i], "symbol": "phi", "feature": i + 1, "lag": 0})
            signals.append({"symbol": f"phi_{i + 1}[k]", "column": feature, "role": "feature"})
        result["notes"].append("这是关于所选特征的线性参数表达式；特征包含非线性变换时，对原变量未必线性。")
    else:
        a, b = values[:spec.na], values[spec.na:spec.na + spec.nb]
        terms = [{"coefficient": value, "symbol": "y", "lag": i + 1} for i, value in enumerate(a)]
        terms += [{"coefficient": value, "symbol": "u", "lag": spec.nk + j} for j, value in enumerate(b)]
        signals.insert(0, {"symbol": "u[k]", "column": spec.input, "role": "input"})
        numerator, denominator = [0.] * spec.nk + b, [1.] + [-v for v in a]
        num_terms = [{"coefficient": v, "symbol": "z", "lag": j} for j, v in enumerate(numerator)]
        den_terms = [{"coefficient": v, "symbol": "z", "lag": j} for j, v in enumerate(denominator)]
        result["transfer_function"] = {
            "numerator": numerator, "denominator": denominator,
            "coefficient_order": "ascending powers of z^-1", "delay_samples": spec.nk,
            "delay_seconds": spec.nk * spec.sample_time,
            "numerator_terms": num_terms, "denominator_terms": den_terms,
            "text": f"G(z) = ({expression(num_terms)}) / ({expression(den_terms)})",
            "latex": r"G(z)=\frac{" + expression(num_terms, True) + "}{" + expression(den_terms, True) + "}",
        }
        length = max(0, spec.nk + spec.nb - 1)
        dimension = spec.na + length
        A, B, C = np.zeros((dimension, dimension)), np.zeros((dimension, 1)), np.zeros((1, dimension))
        g, D = np.zeros((dimension, 1)), 0.
        C[0, :spec.na] = a
        for j, coefficient in enumerate(b):
            lag = spec.nk + j
            if lag == 0:
                D = coefficient
            else:
                C[0, spec.na + lag - 1] = coefficient
        A[0] = C[0]
        B[0, 0], g[0, 0] = D, offset
        for i in range(1, spec.na):
            A[i, i - 1] = 1.
        if length:
            B[spec.na, 0] = 1.
            for i in range(1, length):
                A[spec.na + i, spec.na + i - 1] = 1.
        states = [f"y[k-{i}]" for i in range(1, spec.na + 1)] + [f"u[k-{i}]" for i in range(1, length + 1)]
        result["state_space"] = {"A": A.tolist(), "B": B.tolist(), "C": C.tolist(), "D": [[D]],
                                 "g": g.tolist(), "h": [[offset]], "dimension": dimension,
                                 "state_definition": "x[k] = [" + ", ".join(states) + "]^T",
                                 "equations": ["x[k+1] = A x[k] + B u[k] + g", "y[k] = C x[k] + D u[k] + h"]}
        result["notes"] += [
            "差分方程和状态方程显示确定性模型，省略测量噪声/预测误差项。",
            "G(z) 描述零初始条件下输入 u 对输出的贡献。常值截距 c 单独保留在方程中，不并入输入传递函数。",
            "z^(-1) 表示延迟一个采样周期；G(z) 与差分方程描述离散时间模型。",
            "传递函数及状态矩阵对应一组固定系数；在线辨识时表示当前参数快照。",
            "状态向量由输入、输出历史构成，可能不是最小实现，也不直接代表物理状态。",
        ]
    if spec.intercept:
        terms.append({"coefficient": offset, "symbol": "", "lag": 0})
    result.update(equation_terms=terms, equation_text="y[k] = " + expression(terms),
                  equation_latex="y[k] = " + expression(terms, True), signals=signals)
    result["continuous_model"] = continuous_representation(spec, values)
    lines = ["辨识得到的数学模型", "", result["equation_text"],
             *[f"{s['symbol']} 对应数据列：{s['column']}" for s in signals]]
    if spec.kind == "arx":
        tf, ss = result["transfer_function"], result["state_space"]
        lines += ["", f"采样周期 Ts = {number(spec.sample_time)} s；输入延迟 = {spec.nk} 点 = {number(tf['delay_seconds'])} s",
                  tf["text"], f"常值截距 c = {number(offset)}", "", "等价状态空间表示（滞后状态，包含常值项）",
                  ss["state_definition"], *ss["equations"]]
        for name in ("A", "B", "C", "D", "g", "h"):
            lines += [name + " =", matrix_text(ss[name])]
    lines += ["", *result["notes"], "", result["continuous_model"]["text"]]
    result["text"] = "\n".join(lines)
    return result


def model_report(model):
    """Shared report content for offline exports and acquisition experiment ZIPs."""
    if model is None:
        return ["", "## 辨识得到的数学模型", "", "尚未获得可显示的模型参数。"]
    return ["", "## 辨识得到的数学模型", "", "```text", model["text"], "```"]
