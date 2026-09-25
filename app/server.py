"""HTTP front-end for the shim-correction review service.

Serves the review page, a health endpoint, the exact Diophantine review
API and the travel-limited audit API.  Standard library only; every
integer is handled as an arbitrary-precision Python ``int`` and
serialised as decimal text so values beyond the IEEE-754 safe-integer
range stay exact end to end.
"""

from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# Computed products/sums may exceed the default 4300-digit str() limit;
# the service is explicitly arbitrary-precision, so lift it (input sizes
# are still capped below).
if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)

from app.bounded import optimize_within_bounds
from app.diophantine import solve_diophantine

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = (BASE_DIR / "static").resolve()

MAX_DIMENSION = 64
MAX_DIGITS = 4096
MAX_VARIABLES = 64
MAX_BODY_BYTES = 1 << 20

_INT_RE = re.compile(r"^[+-]?\d+$")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class RequestError(ValueError):
    """Client-side payload problem, answered with HTTP 400."""


def parse_integer(value, where: str) -> int:
    """Parse an exact integer; floats are rejected, never rounded."""
    if isinstance(value, bool):
        raise RequestError(f"{where}：不接受布尔值，必须为整数字符串")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise RequestError(
            f"{where}：检测到浮点数 {value!r}，为保持精确性已拒绝；请以整数字符串提交"
        )
    if isinstance(value, str):
        text = value.strip()
        if not _INT_RE.match(text):
            raise RequestError(f"{where}：“{text}” 不是十进制整数")
        if len(text.lstrip("+-")) > MAX_DIGITS:
            raise RequestError(f"{where}：整数位数超过上限 {MAX_DIGITS}")
        return int(text)
    raise RequestError(f"{where}：类型不支持，必须为整数字符串")


def parse_payload(data) -> tuple[list[str], list[list[int]], list[int]]:
    if not isinstance(data, dict):
        raise RequestError("请求体必须是 JSON 对象")

    variables = data.get("variables")
    matrix_raw = data.get("matrix")
    target_raw = data.get("target")

    if not isinstance(variables, list) or not variables:
        raise RequestError("variables 必须是非空数组")
    if len(variables) > MAX_VARIABLES:
        raise RequestError(f"变量个数超过上限 {MAX_VARIABLES}")
    seen = set()
    for k, name in enumerate(variables):
        if not isinstance(name, str) or not name.strip():
            raise RequestError(f"第 {k + 1} 个变量标识为空或不是字符串")
        name = name.strip()
        if len(name) > 40:
            raise RequestError(f"变量标识 “{name}” 过长")
        if name in seen:
            raise RequestError(f"变量标识 “{name}” 重复")
        seen.add(name)
    variables = [name.strip() for name in variables]

    if not isinstance(matrix_raw, list) or not matrix_raw:
        raise RequestError("matrix 必须是非空行数组")
    if len(matrix_raw) > MAX_DIMENSION:
        raise RequestError(f"约束条数超过上限 {MAX_DIMENSION}")
    matrix = []
    for i, row in enumerate(matrix_raw):
        if not isinstance(row, list):
            raise RequestError(f"矩阵第 {i + 1} 行不是数组")
        if len(row) != len(variables):
            raise RequestError(
                f"矩阵第 {i + 1} 行有 {len(row)} 个系数，与变量数 {len(variables)} 不一致"
            )
        matrix.append(
            [parse_integer(cell, f"矩阵第 {i + 1} 行第 {j + 1} 列") for j, cell in enumerate(row)]
        )

    if not isinstance(target_raw, list) or len(target_raw) != len(matrix):
        raise RequestError("target 必须是长度与约束条数一致的数组")
    target = [parse_integer(value, f"目标向量第 {i + 1} 项") for i, value in enumerate(target_raw)]

    return variables, matrix, target


def parse_bounds(data, variables) -> tuple[list[int], list[int]]:
    """Parse per-variable shim-travel bounds for the travel-limited audit.

    Every variable must appear exactly once with integer ``min``/``max``
    and ``min <= max``; anything else is a client-side format error.
    """
    raw = data.get("bounds")
    if not isinstance(raw, list) or not raw:
        raise RequestError("bounds 必须是非空数组：为每个变量各给出一项 {variable, min, max}")
    if len(raw) != len(variables):
        raise RequestError(f"bounds 条目数 {len(raw)} 与变量数 {len(variables)} 不一致")
    known = set(variables)
    collected = {}
    for index, entry in enumerate(raw):
        where = f"bounds 第 {index + 1} 项"
        if not isinstance(entry, dict):
            raise RequestError(f"{where} 必须是对象（含 variable、min、max）")
        name = entry.get("variable")
        if not isinstance(name, str) or not name.strip():
            raise RequestError(f"{where} 的变量标识为空或不是字符串")
        name = name.strip()
        if name not in known:
            raise RequestError(f"{where} 引用了未知变量 “{name}”")
        if name in collected:
            raise RequestError(f"bounds 中变量 “{name}” 重复出现")
        if "min" not in entry or "max" not in entry:
            raise RequestError(f"变量 “{name}” 的边界必须同时给出 min 与 max")
        lower = parse_integer(entry["min"], f"变量 “{name}” 的最小垫片数")
        upper = parse_integer(entry["max"], f"变量 “{name}” 的最大垫片数")
        if lower > upper:
            raise RequestError(f"变量 “{name}” 的最小垫片数大于最大垫片数")
        collected[name] = (lower, upper)
    lower_bounds = [collected[name][0] for name in variables]
    upper_bounds = [collected[name][1] for name in variables]
    return lower_bounds, upper_bounds


def build_constraints(variables, matrix, target, values) -> list[dict]:
    """Exact per-constraint recomputation for a given correction vector."""
    constraints = []
    for i, row in enumerate(matrix):
        terms = []
        total = 0
        for j, name in enumerate(variables):
            product = row[j] * values[j]
            total += product
            terms.append(
                {
                    "variable": name,
                    "coefficient": str(row[j]),
                    "correction": str(values[j]),
                    "product": str(product),
                }
            )
        constraints.append(
            {
                "index": i,
                "terms": terms,
                "sum": str(total),
                "target": str(target[i]),
                "satisfied": total == target[i],
            }
        )
    return constraints


def build_review_response(variables, matrix, target, result=None) -> dict:
    if result is None:
        result = solve_diophantine(matrix, target)
    body = {
        "ok": True,
        "solvable": result.solvable,
        "variables": variables,
        "smith": {
            "rank": result.smith.rank,
            "diagonal": [str(d) for d in result.smith.diagonal],
            "transformedTarget": [str(v) for v in result.transformed_target],
        },
    }
    if result.solvable:
        body["solution"] = [str(value) for value in result.solution]
        body["constraints"] = build_constraints(
            variables, matrix, target, result.solution
        )
        body["homogeneousBasis"] = [
            [str(value) for value in vector] for vector in result.homogeneous_basis
        ]
    else:
        obstruction = result.obstruction
        u_terms = [
            {
                "row": k,
                "coefficient": str(coefficient),
                "target": str(target[k]),
                "product": str(coefficient * target[k]),
            }
            for k, coefficient in enumerate(obstruction.u_row)
            if coefficient != 0
        ]
        body["obstruction"] = {
            "type": obstruction.kind,
            "row": obstruction.row,
            "pivot": str(obstruction.pivot),
            "transformedTarget": str(obstruction.transformed_target),
            "remainder": str(obstruction.remainder),
            "uRow": [str(coefficient) for coefficient in obstruction.u_row],
            "uRowTerms": u_terms,
        }
    return body


def build_audit_response(variables, matrix, target, lower, upper) -> dict:
    """Travel-limited audit on top of the exact review.

    The review conclusion (solution + constraints, or the canonical
    obstruction) is always included verbatim so the page keeps the
    reviewable original result; the ``audit`` block then reports one of:

    - ``unsolvable``: the equations themselves have no integer solution;
    - ``infeasible``: solvable, but no integer solution inside the bounds;
    - ``optimal``: the in-bounds adjustment minimising the total absolute
      deviation from the current exact correction (ties adjudicated by
      the deviation vector in variable order), with the exact
      recomputation of every constraint.
    """
    result = solve_diophantine(matrix, target)
    body = build_review_response(variables, matrix, target, result=result)
    bounds_payload = [
        {"variable": name, "min": str(lo), "max": str(hi)}
        for name, lo, hi in zip(variables, lower, upper)
    ]
    if not result.solvable:
        body["audit"] = {"status": "unsolvable", "bounds": bounds_payload}
        return body
    optimum = optimize_within_bounds(
        result.solution, result.homogeneous_basis, lower, upper
    )
    if optimum is None:
        body["audit"] = {"status": "infeasible", "bounds": bounds_payload}
        return body
    body["audit"] = {
        "status": "optimal",
        "bounds": bounds_payload,
        "adjustment": [str(value) for value in optimum.adjustment],
        "deviation": [str(value) for value in optimum.deviation],
        "totalAbsDeviation": str(optimum.total_abs_deviation),
        "constraints": build_constraints(
            variables, matrix, target, optimum.adjustment
        ),
    }
    return body


class Handler(BaseHTTPRequestHandler):
    server_version = "ShimReview/1.0"

    def log_message(self, fmt, *args):  # keep container logs concise
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers ---------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, path: Path) -> None:
        data = path.read_bytes()
        content_type = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/":
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if not target.is_file() or STATIC_DIR not in target.parents:
            self._send_json(404, {"ok": False, "error": "资源不存在"})
            return
        self._send_static(target)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in ("/api/review", "/api/audit"):
            self._send_json(404, {"ok": False, "error": "未知路径"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "请求体缺失或超过大小上限"})
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"ok": False, "error": f"JSON 解析失败：{exc}"})
            return
        try:
            variables, matrix, target = parse_payload(data)
            bounds = parse_bounds(data, variables) if path == "/api/audit" else None
        except RequestError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        try:
            if bounds is not None:
                lower, upper = bounds
                response = build_audit_response(variables, matrix, target, lower, upper)
            else:
                response = build_review_response(variables, matrix, target)
            self._send_json(200, response)
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json(500, {"ok": False, "error": f"服务器内部错误：{exc}"})


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"shim-review listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
