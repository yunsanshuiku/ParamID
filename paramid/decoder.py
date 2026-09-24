"""Bounded binary framing and numerical decoder expressions for sample streams.

One complete frame represents one simultaneous sample. Serial read boundaries
are unrelated to frame boundaries. After lock-on, a framing/check failure stops
the experiment: silently removing a sample would corrupt ARX time lags.
Expressions are interpreted from a small AST, never executed as Python code.
"""

import ast
import math
import operator
import re
import struct

from .core import IdentificationError, finite_number, integer


TYPES = {"float64": "d", "float32": "f", "uint8": "B", "int8": "b",
         "uint16": "H", "int16": "h", "uint32": "I", "int32": "i",
         "uint64": "Q", "int64": "q"}
READERS = {"f64": "d", "f32": "f", "u8": "B", "i8": "b", "u16": "H",
           "i16": "h", "u32": "I", "i32": "i", "u64": "Q", "i64": "q"}
BINARY_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
              ast.Mod: operator.mod, ast.BitAnd: operator.and_, ast.BitOr: operator.or_,
              ast.BitXor: operator.xor, ast.LShift: operator.lshift, ast.RShift: operator.rshift}
COMPARE_OPS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
               ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}
FUNCTIONS = set(READERS) | {"bits", "sum8", "xor8", "abs", "min", "max"}


def hex_bytes(value, name, limit):
    """Accept spaced or contiguous hexadecimal bytes, without guessing text."""
    if not isinstance(value, str) or len(value) > limit * 4 + 128:
        raise IdentificationError(f"{name} 过长或不是十六进制文本。")
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise IdentificationError(f"{name} 请填写成对十六进制字节，例如 AA 55；无需 0x。") from exc
    if len(result) > limit:
        raise IdentificationError(f"{name} 最多 {limit} 字节。")
    return result


def sample_number(value, name):
    # The estimator and browser use float64. Do not silently round a wide ADC
    # counter/timestamp before the user has subtracted an epoch or scaled it.
    if isinstance(value, int) and abs(value) > 2 ** 53:
        raise IdentificationError(f"{name} 超出双精度精确整数范围；请在表达式中减去基准或换算单位。")
    return finite_number(value, name)


class DecoderProgram:
    """Compile assignments/assertions once; evaluate O(AST nodes) per frame.

    Offsets refer to payload bytes, excluding framing markers. bits(start, n)
    numbers bit zero as the least significant bit of payload byte zero, and
    combines bits least-significant-first, independent of numeric byte order.
    All assignments become channels; previous assignments may be referenced.
    """

    def __init__(self, code):
        if not isinstance(code, str) or not 1 <= len(code) <= 16384:
            raise IdentificationError("解码表达式不能为空，且不能超过 16,384 个字符。")
        try:
            self.tree = ast.parse(code, mode="exec")
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise IdentificationError("解码表达式语法错误，请使用 input1 = f64(0) 这样的逐行赋值。") from exc
        if sum(1 for _ in ast.walk(self.tree)) > 1024:
            raise IdentificationError("解码表达式过于复杂（最多 1,024 个语法节点）。")
        self.columns = []
        for statement in self.tree.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
                name = statement.targets[0].id
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or name in FUNCTIONS or name in self.columns:
                    raise IdentificationError("通道名须以英文字母开头，只含字母、数字和下划线，且不能重复或使用函数名。")
                self._validate(statement.value)
                self.columns.append(name)
            elif isinstance(statement, ast.Assert) and statement.msg is None:
                self._validate(statement.test)
            else:
                raise IdentificationError("仅支持逐行赋值和 assert 校验；请勿填写循环、导入或函数定义。")
        if not 2 <= len(self.columns) <= 128:
            raise IdentificationError("解码表达式必须输出 2～128 个不同的通道。")

    def _validate(self, node, depth=0):
        if depth > 24:
            raise IdentificationError("解码表达式嵌套过深。")
        children = []
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            self._bounded(node.value)
        elif isinstance(node, ast.Name) and node.id in self.columns:
            pass
        elif isinstance(node, ast.BinOp) and type(node.op) in BINARY_OPS:
            children = [node.left, node.right]
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Invert, ast.Not)):
            children = [node.operand]
        elif isinstance(node, ast.Compare) and all(type(op) in COMPARE_OPS for op in node.ops):
            children = [node.left, *node.comparators]
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            children = node.values
        elif isinstance(node, ast.IfExp):
            children = [node.test, node.body, node.orelse]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords:
            count = len(node.args)
            expected = 1 if node.func.id in READERS or node.func.id == "abs" else 2
            if count != expected:
                raise IdentificationError(f"{node.func.id} 需要 {expected} 个参数。")
            children = node.args
        else:
            raise IdentificationError("不支持的表达式；请使用数值运算、已定义通道和内置读取函数。")
        for child in children:
            self._validate(child, depth + 1)

    @staticmethod
    def _bounded(value):
        if isinstance(value, int) and value.bit_length() > 256:
            raise IdentificationError("解码中间整数超过 256 位。")
        if isinstance(value, float) and (not math.isfinite(value) or abs(value) > 1e100):
            raise IdentificationError("解码得到非有限数值；请核对类型、字节序、偏移与帧对齐。")
        return value

    def decode(self, payload, endian):
        values = {}

        def index(value, limit):
            if type(value) is not int or not 0 <= value <= limit:
                raise IdentificationError("读取偏移、长度或位移必须是范围内的整数。")
            return value

        def call(name, args):
            if name in READERS:
                offset = index(args[0], len(payload))
                reader = struct.Struct(endian + READERS[name])
                if offset + reader.size > len(payload):
                    raise IdentificationError(f"{name}({offset}) 超出载荷边界（{len(payload)} 字节）。")
                return reader.unpack_from(payload, offset)[0]
            if name == "bits":
                begin, count = index(args[0], len(payload) * 8), index(args[1], 64)
                if count < 1 or begin + count > len(payload) * 8:
                    raise IdentificationError("bits 的位数须为 1～64，且不能超出载荷。")
                first, last = begin // 8, (begin + count + 7) // 8
                return (int.from_bytes(payload[first:last], "little") >> (begin % 8)) & ((1 << count) - 1)
            if name in ("sum8", "xor8"):
                offset, size = index(args[0], len(payload)), index(args[1], len(payload))
                if offset + size > len(payload):
                    raise IdentificationError("校验范围超出载荷。")
                data = payload[offset:offset + size]
                if name == "sum8":
                    return sum(data) & 255
                result = 0
                for byte in data:
                    result ^= byte
                return result
            return {"abs": abs, "min": min, "max": max}[name](*args)

        def evaluate(node):
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                return values[node.id]
            if isinstance(node, ast.Call):
                return self._bounded(call(node.func.id, [evaluate(arg) for arg in node.args]))
            if isinstance(node, ast.BinOp):
                left, right = evaluate(node.left), evaluate(node.right)
                if isinstance(node.op, (ast.LShift, ast.RShift)):
                    index(right, 64)
                return self._bounded(BINARY_OPS[type(node.op)](left, right))
            if isinstance(node, ast.UnaryOp):
                return self._bounded({ast.UAdd: operator.pos, ast.USub: operator.neg,
                                      ast.Invert: operator.invert, ast.Not: operator.not_}[type(node.op)](evaluate(node.operand)))
            if isinstance(node, ast.Compare):
                left = evaluate(node.left)
                for op, other in zip(node.ops, node.comparators):
                    right = evaluate(other)
                    if not COMPARE_OPS[type(op)](left, right):
                        return False
                    left = right
                return True
            if isinstance(node, ast.BoolOp):
                return all(evaluate(n) for n in node.values) if isinstance(node.op, ast.And) else any(evaluate(n) for n in node.values)
            if isinstance(node, ast.IfExp):
                return evaluate(node.body if evaluate(node.test) else node.orelse)
            raise IdentificationError("不支持的解码表达式。")

        for statement in self.tree.body:
            try:
                if isinstance(statement, ast.Assert):
                    if not evaluate(statement.test):
                        raise IdentificationError("数据帧未通过 assert 校验，已停止接收。")
                else:
                    values[statement.targets[0].id] = evaluate(statement.value)
            except (ArithmeticError, ValueError, TypeError) as exc:
                raise IdentificationError(f"解码第 {statement.lineno} 行：{exc}") from exc
        return {name: sample_number(value, name) for name, value in values.items()}


def decoder_config(config):
    """Normalize the persisted wire contract before opening any physical port."""
    if not isinstance(config, dict):
        raise IdentificationError("decoder 必须是解码配置对象。")
    mode = config.get("mode", "preset")
    byte_order = config.get("byte_order", "little")
    if mode not in ("preset", "custom") or byte_order not in ("little", "big"):
        raise IdentificationError("请选择预设/自定义解码及小端/大端字节序。")
    result = {"mode": mode, "byte_order": byte_order,
              "header_hex": hex_bytes(config.get("header_hex", ""), "帧头", 32).hex(" ").upper(),
              "trailer_hex": hex_bytes(config.get("trailer_hex", ""), "帧尾", 32).hex(" ").upper()}
    if mode == "preset":
        dtype = {"double": "float64", "float": "float32"}.get(config.get("dtype"), config.get("dtype", "float64"))
        columns = config.get("columns", ["input1", "output1"])
        if not isinstance(dtype, str) or dtype not in TYPES:
            raise IdentificationError("不支持该数值类型；6 位打包整数请使用自定义 bits 解码。")
        if not isinstance(columns, list) or not 2 <= len(columns) <= 128 or any(not isinstance(c, str) or not c.strip() or len(c) > 100 for c in columns):
            raise IdentificationError("请按发送顺序填写 2～128 个通道名称。")
        columns = [c.strip() for c in columns]
        if len(set(columns)) != len(columns):
            raise IdentificationError("通道名称不能重复。")
        result.update(dtype=dtype, columns=columns, payload_bytes=struct.calcsize("<" + TYPES[dtype]) * len(columns))
    else:
        program = DecoderProgram(config.get("code", ""))
        result.update(code=config["code"], columns=program.columns,
                      payload_bytes=integer(config.get("payload_bytes", 16), "载荷字节数", 1, 4096))
    return result


class BinaryStream:
    """Decode fixed-size frames across arbitrary serial chunks, without drops.

    An optional header finds the first frame; later mismatches are fatal. Pure
    fixed-length streams must start at a frame boundary. Without a marker or
    checksum, a shifted stream cannot in general be detected or repaired.
    """

    def __init__(self, config):
        self.config = decoder_config(config)
        self.columns = self.config["columns"]
        self.header = bytes.fromhex(self.config["header_hex"])
        self.trailer = bytes.fromhex(self.config["trailer_hex"])
        self.size = self.config["payload_bytes"]
        self.frame_bytes = len(self.header) + self.size + len(self.trailer)
        self.endian = "<" if self.config["byte_order"] == "little" else ">"
        self.program = DecoderProgram(self.config["code"]) if self.config["mode"] == "custom" else None
        self.unpacker = None if self.program else struct.Struct(self.endian + TYPES[self.config["dtype"]] * len(self.columns))
        self.pending = bytearray()
        self.raw_tail = b""
        self.received_bytes = self.decoded_frames = self.discarded_bytes = 0
        self.aligned = not self.header

    def feed(self, chunk):
        self.received_bytes += len(chunk)
        self.raw_tail = (self.raw_tail + bytes(chunk))[-96:]
        # Process bounded slices even for the offline preview API or tests.
        for start in range(0, len(chunk), 4096):
            self.pending.extend(chunk[start:start + 4096])
            while True:
                if not self.aligned:
                    position = self.pending.find(self.header)
                    discard = position if position >= 0 else max(0, len(self.pending) - len(self.header) + 1)
                    self.discarded_bytes += discard
                    del self.pending[:discard]
                    if self.discarded_bytes > 65536:
                        raise IdentificationError("已跳过超过 64 KiB，仍未找到帧头；请核对波特率与帧头。")
                    if position < 0:
                        break
                    self.aligned = True
                if len(self.pending) < self.frame_bytes:
                    break
                frame = bytes(self.pending[:self.frame_bytes])
                if not frame.startswith(self.header) or (self.trailer and not frame.endswith(self.trailer)):
                    raise IdentificationError("二进制帧头/帧尾不匹配；可能丢字节或帧长度错误。已停止，避免错位数据进入模型。")
                payload = frame[len(self.header):len(self.header) + self.size]
                if self.program:
                    row = self.program.decode(payload, self.endian)
                else:
                    row = {name: sample_number(value, name) for name, value in zip(self.columns, self.unpacker.unpack(payload))}
                del self.pending[:self.frame_bytes]
                self.decoded_frames += 1
                yield row

    def diagnostics(self):
        return {"received_bytes": self.received_bytes, "decoded_frames": self.decoded_frames,
                "discarded_bytes": self.discarded_bytes, "pending_bytes": len(self.pending),
                "frame_bytes": self.frame_bytes, "raw_hex": self.raw_tail.hex(" ").upper()}


def preview_decode(payload):
    """Exercise the exact streaming parser with supplied bytes, without a port."""
    parser = BinaryStream(payload.get("decoder", {}))
    raw = hex_bytes(payload.get("hex", ""), "试解码数据", 8192)
    if not raw:
        raise IdentificationError("请粘贴至少一帧十六进制数据，或点击填入示例。")
    rows, error = [], ""
    try:
        for row in parser.feed(raw):
            if len(rows) < 120:
                rows.append(row)
    except IdentificationError as exc:
        error = str(exc)
    return {"config": parser.config, "columns": parser.columns, "rows": rows, "error": error,
            **parser.diagnostics()}
