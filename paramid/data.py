"""Strict CSV parsing and shared causal regressor construction."""

import csv
import io
import re

import numpy as np

from .core import IdentificationError, ModelSpec, ModelStream

MAX_ROWS = 50000


def detect_channels(headers: list[str], rows: list[dict] | None = None) -> dict:
    """Infer numbered I/O without relying on column order or pairing suffixes.

    Names are preserved for indexing; matching ignores case and an optional
    separator. Numeric suffixes define ordering, so input2 precedes input10.
    Ambiguous aliases are reported, never silently collapsed to one channel.
    Time is in seconds. Its median difference estimates Ts, but every interval
    must agree within 1%; irregular records are not silently resampled.
    """
    groups = {"input": {}, "output": {}}
    errors = []
    for name in headers:
        alias = {"u": "input1", "input": "input1", "y": "output1", "output": "output1"}.get(name.lower(), name)
        match = re.fullmatch(r"(input|output)[_\s-]?(\d+)", alias, re.IGNORECASE)
        if not match:
            continue
        role, number = match.group(1).lower(), int(match.group(2))
        if number in groups[role]:
            errors.append(f"通道编号重复：{groups[role][number]} 与 {name}。")
        else:
            groups[role][number] = name
    inputs = [name for _, name in sorted(groups["input"].items())]
    outputs = [name for _, name in sorted(groups["output"].items())]
    time_columns = [name for name in headers if name.lower() in ("time", "time_s", "t", "timestamp")]
    if len(time_columns) > 1:
        errors.append("存在多个时间列，请仅保留一个 time / time_s / t / timestamp 列（单位秒）。")
    time_column = time_columns[0] if len(time_columns) == 1 else ""
    dt = None
    if time_column and rows:
        try:
            times = np.asarray([float(row[time_column]) for row in rows])
            if len(times) < 2 or not np.all(np.isfinite(times)):
                raise ValueError
            differences = np.diff(times)
            dt = float(np.median(differences))
            if dt <= 0 or np.any(differences <= 0) or np.any(np.abs(differences - dt) > max(1e-10, .01 * dt)):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            dt = None
            errors.append("时间列必须为有限、严格递增且等间隔的秒数；当前记录无法自动确定采样周期。")
    return {"inputs": inputs, "outputs": outputs, "time": time_column, "sample_time": dt,
            "detected": bool(inputs and outputs), "valid": bool(inputs and outputs) and not errors,
            "system_type": ("S" if len(inputs) == 1 else "M") + "I" + ("S" if len(outputs) == 1 else "M") + "O" if inputs and outputs else None,
            "errors": errors, "ignored": [name for name in headers if name not in inputs + outputs + time_columns]}


def resolve_channels(headers: list[str], rows: list[dict], config: dict) -> dict:
    """Validate explicit channel roles, retaining actual headerless column names.

    Automatic recognition and manual selection share time validation; a time
    column cannot also be an input/output. Explicit selection may intentionally
    omit other numbered channels, so automatic alias errors do not override it.
    """
    if "inputs" not in config and "outputs" not in config:
        return detect_channels(headers, rows)
    inputs, outputs, time_column = config.get("inputs"), config.get("outputs"), config.get("time", "")
    for names, role in ((inputs, "输入"), (outputs, "输出")):
        if not isinstance(names, list) or not names or any(not isinstance(name, str) or name not in headers for name in names):
            raise IdentificationError(f"请至少选择一路有效{role}列。")
        if len(names) != len(set(names)):
            raise IdentificationError(f"{role}列不能重复。")
    if not isinstance(time_column, str) or (time_column and time_column not in headers):
        raise IdentificationError("请选择有效的时间列，或留空后填写采样周期。")
    if set(inputs) & set(outputs) or (time_column and time_column in inputs+outputs):
        raise IdentificationError("时间、输入和输出列必须互不重复。")
    # Reuse automatic interval checks without relying on the user's column name.
    timing = detect_channels(["time"], [{"time": row[time_column]} for row in rows]) if time_column else {}
    errors = timing.get("errors", [])
    return {"inputs": inputs, "outputs": outputs, "time": time_column, "sample_time": timing.get("sample_time"),
            "detected": True, "valid": not errors, "source": "manual", "errors": errors,
            "system_type": ("S" if len(inputs) == 1 else "M")+"I"+("S" if len(outputs) == 1 else "M")+"O",
            "ignored": [name for name in headers if name not in inputs+outputs+[time_column]]}


def parse_table(text: str, header_mode: str = "auto") -> dict:
    """Parse CSV with explicit or inferred headers, never discarding sample zero.

    A fully numeric first row is data in auto mode. Numeric column names are
    ambiguous, so callers may force 'present' or 'absent'. Headerless columns
    get stable column1..columnN identifiers, including the first data row.
    Selected values are subsequently validated by the estimator, not here.
    """
    if header_mode not in ("auto", "present", "absent"):
        raise IdentificationError("表头设置必须为 auto / present / absent。")
    if not isinstance(text, str) or not text.strip() or len(text) > 12 * 1024 * 1024:
        raise IdentificationError("CSV 不能为空，且文本大小须在 12MB 以内。")
    text = text.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect=dialect)
    try:
        first = next(reader)
        try:
            for value in first:
                float(value)
            numeric_first = bool(first)
        except ValueError:
            numeric_first = False
        has_header = header_mode == "present" or (header_mode == "auto" and not numeric_first)
        header = [name.strip() for name in first] if has_header else [f"column{i+1}" for i in range(len(first))]
        if not header or any(not x for x in header) or len(header) != len(set(header)) or len(header) > 128:
            raise IdentificationError("CSV 表头不能为空、重复或超过 128 列。")
        rows = [] if has_header else [dict(zip(header, first))]
        for line, values in enumerate(reader, start=2):
            if not values:
                continue
            if len(values) != len(header):
                raise IdentificationError(f"CSV 第 {line} 行列数与表头不一致。")
            rows.append(dict(zip(header, values)))
            if len(rows) > MAX_ROWS:
                raise IdentificationError(f"首版单文件最多 {MAX_ROWS} 个样本。")
    except (csv.Error, StopIteration) as exc:
        raise IdentificationError("CSV 格式错误。") from exc
    if not rows:
        raise IdentificationError("CSV 没有数据行。")
    return {"columns": header, "rows": rows, "samples": len(rows), "has_header": has_header, "header_mode": header_mode}


def parse_csv(text: str, header_mode: str = "auto") -> tuple[list[str], list[dict]]:
    """Compatibility tuple API shared by offline and streaming-file replay."""
    table = parse_table(text, header_mode)
    return table["columns"], table["rows"]


def prepare(rows: list[dict], spec: ModelSpec) -> dict:
    """Use exactly the same feature/history logic as a live session."""
    stream = ModelStream(spec)
    samples = []
    for index, row in enumerate(rows):
        try:
            item = stream.step(row)
        except IdentificationError as exc:
            raise IdentificationError(f"数据第 {index + 1} 个样本：{exc}") from exc
        if item is not None:
            samples.append(item)
    if not samples:
        raise IdentificationError("样本不足以完成模型预热。")
    x, y, t = zip(*samples)
    return {"x": np.asarray(x), "y": np.asarray(y), "time": np.asarray(t)}
