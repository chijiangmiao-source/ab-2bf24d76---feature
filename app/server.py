"""HTTP front-end for the shim-correction review service.

Serves the review page, a health endpoint and the exact Diophantine
review API.  Standard library only; every integer is handled as an
arbitrary-precision Python ``int`` and serialised as decimal text so
values beyond the IEEE-754 safe-integer range stay exact end to end.
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

from app.diophantine import solve_diophantine
from app.bounded import AuditSearchLimit, UnderconstrainedBounds, audit_bounded

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


def _empty_bound(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_bounds(raw, count: int) -> list:
    """Parse optional per-item ``[min, max]`` shim-count travel bounds.

    An entry may be ``null``, an empty string, or a two-element array whose
    elements are exact integers or empty (an open side).  Missing sides mean
    that side of the travel is not limited.
    """
    if raw is None:
        return [None] * count
    if not isinstance(raw, list):
        raise RequestError("bounds 必须是每项一条 [最小垫片数, 最大垫片数] 的数组")
    if len(raw) != count:
        raise RequestError(
            f"bounds 有 {len(raw)} 条，与校正项数 {count} 不一致"
        )
    bounds = []
    for i, entry in enumerate(raw):
        where = f"第 {i + 1} 项行程边界"
        if _empty_bound(entry):
            bounds.append(None)
            continue
        if not isinstance(entry, list) or len(entry) != 2:
            raise RequestError(f"{where}：必须是 [最小垫片数, 最大垫片数] 两元素数组")
        lo_raw, hi_raw = entry
        lo = None if _empty_bound(lo_raw) else parse_integer(lo_raw, f"{where}的最小值")
        hi = None if _empty_bound(hi_raw) else parse_integer(hi_raw, f"{where}的最大值")
        if lo is not None and hi is not None and lo > hi:
            raise RequestError(f"{where}：最小垫片数 {lo} 大于最大垫片数 {hi}")
        bounds.append((lo, hi))
    return bounds


def build_review_response(variables, matrix, target) -> dict:
    result = solve_diophantine(matrix, target)
    body = {
        "ok": True,
        "solvable": result.solvable,
        "variables": variables,
        "smith": _smith_block(result),
    }
    if result.solvable:
        body["solution"] = [str(value) for value in result.solution]
        body["constraints"] = _constraint_checks(
            variables, matrix, target, result.solution
        )
        body["homogeneousBasis"] = [
            [str(value) for value in vector] for vector in result.homogeneous_basis
        ]
    else:
        body["obstruction"] = _obstruction_block(result, target)
    return body


def _constraint_checks(variables, matrix, target, solution):
    """Exact per-constraint product/sum recomputation for one candidate."""
    constraints = []
    for i, row in enumerate(matrix):
        terms = []
        total = 0
        for j, name in enumerate(variables):
            product = row[j] * solution[j]
            total += product
            terms.append(
                {
                    "variable": name,
                    "coefficient": str(row[j]),
                    "correction": str(solution[j]),
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


def _smith_block(result) -> dict:
    return {
        "rank": result.smith.rank,
        "diagonal": [str(d) for d in result.smith.diagonal],
        "transformedTarget": [str(v) for v in result.transformed_target],
    }


def _obstruction_block(result, target) -> dict:
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
    return {
        "type": obstruction.kind,
        "row": obstruction.row,
        "pivot": str(obstruction.pivot),
        "transformedTarget": str(obstruction.transformed_target),
        "remainder": str(obstruction.remainder),
        "uRow": [str(coefficient) for coefficient in obstruction.u_row],
        "uRowTerms": u_terms,
    }


def build_audit_response(variables, matrix, target, bounds) -> dict:
    audit = audit_bounded(matrix, target, bounds)
    body = {
        "ok": True,
        "solvable": audit.solvable,
        "feasible": audit.feasible,
        "reason": audit.reason,
        "variables": variables,
        "bounds": [
            None
            if bound is None
            else [
                None if bound[0] is None else str(bound[0]),
                None if bound[1] is None else str(bound[1]),
            ]
            for bound in bounds
        ],
        "smith": _smith_block(audit),
        "nodes": audit.nodes,
    }
    if not audit.solvable:
        body["obstruction"] = _obstruction_block(audit, target)
        return body

    body["solution"] = [str(value) for value in audit.particular]
    body["homogeneousBasis"] = [
        [str(value) for value in vector] for vector in audit.basis
    ]
    if not audit.feasible:
        # Solvable equation, no integer lattice point inside the travel.
        body["items"] = [
            {
                "variable": name,
                "min": None if bounds[j] is None or bounds[j][0] is None
                else str(bounds[j][0]),
                "max": None if bounds[j] is None or bounds[j][1] is None
                else str(bounds[j][1]),
                "current": str(audit.particular[j]),
                "within": (
                    bounds[j] is None
                    or (
                        (bounds[j][0] is None or audit.particular[j] >= bounds[j][0])
                        and (bounds[j][1] is None or audit.particular[j] <= bounds[j][1])
                    )
                ),
            }
            for j, name in enumerate(variables)
        ]
        return body

    adjusted = audit.adjusted
    offsets = audit.offsets
    items = []
    for j, name in enumerate(variables):
        lo = bounds[j][0] if bounds[j] is not None else None
        hi = bounds[j][1] if bounds[j] is not None else None
        items.append(
            {
                "variable": name,
                "min": None if lo is None else str(lo),
                "max": None if hi is None else str(hi),
                "current": str(audit.particular[j]),
                "adjusted": str(adjusted[j]),
                "offset": str(offsets[j]),
                "absOffset": str(abs(offsets[j])),
                "within": (lo is None or adjusted[j] >= lo)
                and (hi is None or adjusted[j] <= hi),
            }
        )
    body["adjusted"] = [str(value) for value in adjusted]
    body["offsets"] = [str(value) for value in offsets]
    body["totalAbsOffset"] = str(audit.total_abs_offset)
    body["items"] = items
    # Exact recomputation of every original constraint at the adjusted point.
    body["constraints"] = _constraint_checks(variables, matrix, target, adjusted)
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
            if path == "/api/audit":
                bounds = parse_bounds(data.get("bounds"), len(variables))
                response = build_audit_response(variables, matrix, target, bounds)
            else:
                response = build_review_response(variables, matrix, target)
        except RequestError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except (UnderconstrainedBounds, AuditSearchLimit) as exc:
            self._send_json(422, {"ok": False, "error": str(exc)})
            return
        try:
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
