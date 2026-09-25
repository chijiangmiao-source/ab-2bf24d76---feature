"""Exact travel-bounded integer audit for ``A x = b``.

Every integer solution of a solvable system has the form

    x = p + sum_k t_k * h_k,   t_k in Z

where ``p`` is the particular solution returned by :func:`solve_diophantine`
and the ``h_k`` are the free columns of the unimodular Smith matrix ``V``.
Because ``V`` is unimodular, those columns are not merely a rational basis of
the kernel: they are a basis of the *integer* solution lattice, so ranging
``t`` over ``Z^q`` enumerates every integer solution exactly once.

The audit imposes per-item travel bounds ``lo_j <= x_j <= hi_j`` (either side
may be omitted) and finds the bounded feasible point minimising the total
absolute offset ``sum_j |x_j - p_j|``.  Ties are adjudicated deterministically
by the lexicographic order of the signed offset vector in variable order.

Exact search strategy (no continuous rounding, no greedy walk along a single
free direction):

1. The particular solution is tried first (offset zero).
2. Filled *two-sided* bounds must span every free direction: ``q`` independent
   bounded items give an invertible square subsystem ``B`` of the lattice
   matrix, hence an exact finite box for every lattice parameter.  One-sided
   or missing bounds only tighten the search; when the double-sided bounds do
   not span the free space the request is rejected up front instead of
   guessing.
3. A find-any branch-and-bound scans successive per-coordinate caps
   ``|offset_j| <= C`` (doubled from a trivial lower bound up to the exact
   outer range of the finite box), with interval propagation through rows
   that have a single unassigned parameter and through the rational inverse
   of ``B``.  An empty final box proves "solvable equation, no integer point
   inside the travel bounds".
4. With the first feasible offset of L1 norm ``L`` in hand, an exhaustive
   pass over the finite sublevel set ``sum_j |offset_j| <= L`` proves the
   global optimum and settles the lexicographic tie.

All arithmetic is exact (``int`` / ``fractions.Fraction``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from itertools import combinations
from typing import List, Optional, Sequence, Tuple

from app.diophantine import SmithDecomposition, solve_diophantine

# Exhaustive enumeration safety valve.  Travel audits enumerate a bounded
# lattice box; pathological inputs (huge ranges, many free directions) can
# contain astronomically many points.  Exceeding the budget is reported as a
# request error, never as "no solution".
NODE_LIMIT = 2_000_000


class AuditSearchLimit(RuntimeError):
    """Exhaustive enumeration exceeded :data:`NODE_LIMIT` nodes."""


class UnderconstrainedBounds(ValueError):
    """Filled bounds cannot make the free-lattice search finite."""


@dataclass
class BoundedAudit:
    """Outcome of the travel-bounded audit."""

    solvable: bool
    feasible: bool
    reason: Optional[str] = None  # "equation_unsolvable" | "no_bounded_solution"
    particular: List[int] = field(default_factory=list)
    basis: List[List[int]] = field(default_factory=list)
    adjusted: Optional[List[int]] = None
    offsets: Optional[List[int]] = None
    total_abs_offset: Optional[int] = None
    nodes: int = 0
    smith: Optional[SmithDecomposition] = None
    transformed_target: List[int] = field(default_factory=list)
    obstruction: object = None

    @property
    def free_directions(self) -> int:
        return len(self.basis)


# ---------------------------------------------------------------------------
# Exact integer / rational helpers
# ---------------------------------------------------------------------------

def _ceil_div(a: int, b: int) -> int:
    """Ceiling of ``a / b`` for ``b > 0``, exact for negative numerators."""
    return -((-a) // b)


def _coefficient_interval(c: int, lo: int, hi: int) -> Tuple[int, int]:
    """Integer range of ``t`` satisfying ``lo <= c*t <= hi`` (``c != 0``)."""
    if c > 0:
        return _ceil_div(lo, c), hi // c
    # c < 0:  -hi <= (-c)*t <= -lo
    return _ceil_div(-hi, -c), (-lo) // (-c)


def _floor_fraction(value: Fraction) -> int:
    return value.numerator // value.denominator


def _ceil_fraction(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


def _independent_row_subset(
    rows: Sequence[Sequence[int]], columns: int
) -> Optional[List[int]]:
    """Pick ``columns`` linearly independent row indices, if they exist."""
    pivots: List[Tuple[int, List[Fraction]]] = []
    chosen: List[int] = []
    for idx, row in enumerate(rows):
        reduced = [Fraction(value) for value in row]
        for pivot_col, pivot_row in pivots:
            if reduced[pivot_col] != 0:
                factor = reduced[pivot_col] / pivot_row[pivot_col]
                reduced = [a - factor * b for a, b in zip(reduced, pivot_row)]
        pivot_col = next((c for c in range(columns) if reduced[c] != 0), None)
        if pivot_col is not None:
            pivots.append((pivot_col, reduced))
            chosen.append(idx)
            if len(chosen) == columns:
                return chosen
    return None


def _invert_fraction(matrix: Sequence[Sequence[int]]):
    """Exact rational inverse via Gauss-Jordan elimination."""
    r = len(matrix)
    aug = [
        [Fraction(matrix[i][j]) for j in range(r)]
        + [Fraction(1 if i == j else 0) for j in range(r)]
        for i in range(r)
    ]
    for col in range(r):
        pivot = next((i for i in range(col, r) if aug[i][col] != 0), None)
        if pivot is None:
            return None
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        scale = aug[col][col]
        aug[col] = [value / scale for value in aug[col]]
        for i in range(r):
            if i != col and aug[i][col] != 0:
                factor = aug[i][col]
                aug[i] = [a - factor * b for a, b in zip(aug[i], aug[col])]
    return [row[r:] for row in aug]


def _near_zero_order(lo: int, hi: int):
    """Yield every integer in ``[lo, hi]`` ordered by distance from zero.

    The order is deterministic (positive before negative at equal distance);
    it only affects how fast an incumbent is found, never which lattice
    points are visited.
    """
    if lo > hi:
        return
    if lo <= 0 <= hi:
        yield 0
    radius = 1
    upper_radius = max(hi, -lo)
    while radius <= upper_radius:
        if lo <= radius <= hi:
            yield radius
        if lo <= -radius <= hi:
            yield -radius
        radius += 1


# ---------------------------------------------------------------------------
# Branch-and-bound over the free lattice parameters
# ---------------------------------------------------------------------------

@dataclass
class _Mode:
    name: str  # "feasibility" (per-coordinate cap) | "proof" (L1 residual)
    cap: int


class _LatticeSearch:
    """Enumerate ``t`` with ``(H t)_j`` inside per-row travel intervals.

    ``incidence[j]`` lists ``(k, c)`` with ``c = h_k[j] != 0``; ``sides[j]``
    is ``(lo_j - p_j, hi_j - p_j)`` with optional ends.  ``pivot_rows`` are
    the ``q`` double-sided items whose coefficient submatrix is invertible;
    their rational inverse supplies a finite branch box at every node.
    """

    def __init__(self, incidence, sides, pivot_rows, inverse, mode: _Mode):
        self.incidence = incidence
        self.sides = sides
        self.q = len(inverse)
        self.n = len(incidence)
        self.pivot_rows = pivot_rows
        self.root_inverse = inverse
        self.mode = mode
        self.nodes = 0
        self.best_total: Optional[int] = None
        self.best_offsets: Optional[Tuple[int, ...]] = None
        self._box_cache: dict = {}

    def _boxed_inverse(self, remaining: Sequence[int]):
        """Inverse of the pivot subsystem restricted to remaining columns.

        Columns of the invertible pivot matrix stay independent under any
        column subset, so the restricted subsystem always has full column
        rank; ``q'`` independent pivot rows are reselected exactly.
        """
        key = tuple(remaining)
        cached = self._box_cache.get(key)
        if cached is not None:
            return cached
        r = len(remaining)
        subrows = []
        for j in self.pivot_rows:
            coeff = dict(self.incidence[j])
            subrows.append([coeff.get(k, 0) for k in remaining])
        chosen = _independent_row_subset(subrows, r)
        if chosen is None:  # pragma: no cover - pivot columns are independent
            raise ArithmeticError("内部校验失败：剩余参数子矩阵秩亏损")
        square = [subrows[i] for i in chosen]
        inverse = _invert_fraction(square)
        if inverse is None:  # pragma: no cover - guarded by rank check
            raise ArithmeticError("内部校验失败：边界子系统不可逆")
        result = ([self.pivot_rows[i] for i in chosen], inverse)
        self._box_cache[key] = result
        return result

    def _remaining_limits(self, row: int, partial: int, closed_sum: int):
        """[lo, hi] on the not-yet-assigned contribution of one row."""
        bound_lo, bound_hi = self.sides[row]
        if self.mode.name == "feasibility":
            cap = self.mode.cap
            lo = -cap - partial
            hi = cap - partial
        else:  # proof: |partial + remaining| <= L* - sum of closed |offset|
            budget = self.mode.cap - closed_sum
            lo, hi = -budget - partial, budget - partial
        if bound_lo is not None:
            lo = max(lo, bound_lo - partial)
        if bound_hi is not None:
            hi = min(hi, bound_hi - partial)
        return lo, hi

    def run(self, find_any: bool) -> bool:
        try:
            self._recurse(frozenset(), [0] * self.n, 0)
        except _StopEarly:
            pass
        return self.best_offsets is not None

    def _recurse(self, assigned: frozenset, partial: List[int],
                 closed_sum: int) -> None:
        self.nodes += 1
        if self.nodes > NODE_LIMIT:
            raise AuditSearchLimit(
                f"行程审计枚举超过 {NODE_LIMIT} 个节点的安全上限"
            )

        open_rows = []
        closed_sum = 0
        for j, entries in enumerate(self.incidence):
            remaining = [(k, c) for k, c in entries if k not in assigned]
            value = partial[j]
            if not remaining:
                bound_lo, bound_hi = self.sides[j]
                if bound_lo is not None and value < bound_lo:
                    return
                if bound_hi is not None and value > bound_hi:
                    return
                if self.mode.name == "feasibility":
                    if abs(value) > self.mode.cap:
                        return
                else:
                    closed_sum += abs(value)
                    if closed_sum > self.mode.cap:
                        return
            else:
                open_rows.append((j, remaining, value))
        if self.mode.name == "proof" and closed_sum > self.mode.cap:
            return

        unassigned = [k for k in range(self.q) if k not in assigned]
        if not unassigned:
            self._accept_leaf(partial)
            return

        intervals: dict = {k: [None, None] for k in unassigned}

        def constrain(k: int, lo: int, hi: int) -> bool:
            current = intervals[k]
            current[0] = lo if current[0] is None else max(current[0], lo)
            current[1] = hi if current[1] is None else min(current[1], hi)
            return current[0] <= current[1]

        # Tight propagation through rows with one unassigned parameter.
        for j, remaining, value in open_rows:
            if len(remaining) == 1:
                k, c = remaining[0]
                lo, hi = self._remaining_limits(j, value, closed_sum)
                t_lo, t_hi = _coefficient_interval(c, lo, hi)
                if not constrain(k, t_lo, t_hi):
                    return

        # Finite box for every remaining parameter via the invertible pivot
        # subsystem (exact rational bounds, then integer ceil/floor).
        ordered = sorted(unassigned)
        box_rows, inverse = self._boxed_inverse(ordered)
        z_bounds = [
            self._remaining_limits(j, partial[j], closed_sum) for j in box_rows
        ]
        r = len(ordered)
        for a in range(r):
            lo_fraction = Fraction(0)
            hi_fraction = Fraction(0)
            for b in range(r):
                coefficient = inverse[a][b]
                z_lo, z_hi = z_bounds[b]
                if coefficient >= 0:
                    lo_fraction += coefficient * z_lo
                    hi_fraction += coefficient * z_hi
                else:
                    lo_fraction += coefficient * z_hi
                    hi_fraction += coefficient * z_lo
            if not constrain(
                ordered[a],
                _ceil_fraction(lo_fraction),
                _floor_fraction(hi_fraction),
            ):
                return

        # Branch smallest interval first; near-zero values first.  Every
        # value in every interval is still visited, so this is purely a
        # pruning heuristic.
        k = min(
            unassigned,
            key=lambda key: (intervals[key][1] - intervals[key][0], key),
        )
        lo, hi = intervals[k]
        for value in _near_zero_order(lo, hi):
            updated = list(partial)
            for j, entries in enumerate(self.incidence):
                for kk, c in entries:
                    if kk == k:
                        updated[j] += c * value
            self._recurse(assigned | {k}, updated, closed_sum)

    def _accept_leaf(self, partial: List[int]) -> None:
        offsets = tuple(partial)
        total = sum(abs(value) for value in offsets)
        if self.best_offsets is None or (total, offsets) < (
            self.best_total,
            self.best_offsets,
        ):
            self.best_total = total
            self.best_offsets = offsets
        if self.mode.name == "feasibility":
            raise _StopEarly


class _StopEarly(Exception):
    """Internal signal: feasibility search only needs one leaf."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def audit_bounded(
    matrix: Sequence[Sequence[int]],
    target: Sequence[int],
    bounds: Sequence[Optional[Tuple[Optional[int], Optional[int]]]],
) -> BoundedAudit:
    """Solve ``A x = b`` and audit the bounded integer solution lattice."""
    result = solve_diophantine(matrix, target)
    common = dict(smith=result.smith, transformed_target=result.transformed_target)

    if not result.solvable:
        return BoundedAudit(
            solvable=False,
            feasible=False,
            reason="equation_unsolvable",
            obstruction=result.obstruction,
            **common,
        )

    n = len(matrix[0])
    m = len(matrix)
    particular = result.solution
    basis = result.homogeneous_basis  # adjusted = p + sum_k t_k * basis[k]
    q = len(basis)
    sides = [
        (None, None) if bound is None else bound for bound in bounds
    ]

    def within_bounds(values: Sequence[int]) -> bool:
        for j, value in enumerate(values):
            lo, hi = sides[j]
            if lo is not None and value < lo:
                return False
            if hi is not None and value > hi:
                return False
        return True

    # Zero-offset optimum: the current precise correction already travels.
    if within_bounds(particular):
        return BoundedAudit(
            solvable=True,
            feasible=True,
            particular=particular,
            basis=basis,
            adjusted=list(particular),
            offsets=[0] * n,
            total_abs_offset=0,
            nodes=0,
            **common,
        )

    if q == 0:  # unique integer solution and a bound is violated
        return BoundedAudit(
            solvable=True,
            feasible=False,
            reason="no_bounded_solution",
            particular=particular,
            basis=basis,
            **common,
        )

    # Offset coordinates d_j = x_j - p_j live in [lo_j - p_j, hi_j - p_j].
    # The lattice search enumerates offsets exclusively; every interval below
    # is therefore stated in offset coordinates.
    incidence = [
        [(k, basis[k][j]) for k in range(q) if basis[k][j] != 0]
        for j in range(n)
    ]
    offset_sides = []
    for j, (lo, hi) in enumerate(sides):
        d_lo = None if lo is None else lo - particular[j]
        d_hi = None if hi is None else hi - particular[j]
        offset_sides.append((d_lo, d_hi))

    # Items with no component along any free direction are fixed forever:
    # their offset is identically zero, so any open side excluding zero
    # makes the travel immediately infeasible.
    for j, entries in enumerate(incidence):
        if not entries:
            d_lo, d_hi = offset_sides[j]
            if (d_lo is not None and d_lo > 0) or (d_hi is not None and d_hi < 0):
                return BoundedAudit(
                    solvable=True,
                    feasible=False,
                    reason="no_bounded_solution",
                    particular=particular,
                    basis=basis,
                    **common,
                )

    def _finish(offsets: List[int], nodes: int) -> BoundedAudit:
        adjusted = [particular[j] + offsets[j] for j in range(n)]
        for i in range(m):
            if sum(matrix[i][j] * adjusted[j] for j in range(n)) != target[i]:
                raise ArithmeticError("内部校验失败：调整方案未复现目标向量")
        if not within_bounds(adjusted):
            raise ArithmeticError("内部校验失败：调整方案越界")
        return BoundedAudit(
            solvable=True,
            feasible=True,
            particular=particular,
            basis=basis,
            adjusted=adjusted,
            offsets=offsets,
            total_abs_offset=sum(abs(v) for v in offsets),
            nodes=nodes,
            **common,
        )

    def _infeasible(nodes: int) -> BoundedAudit:
        return BoundedAudit(
            solvable=True,
            feasible=False,
            reason="no_bounded_solution",
            particular=particular,
            basis=basis,
            nodes=nodes,
            **common,
        )

    # --- One free direction: exact closed form, no enumeration ----------
    # offsets = t * h0; the L1 objective is |t| * ||h0||_1, hence the exact
    # optimum is the feasible lattice parameter nearest zero.  This remains
    # exact for arbitrarily large travel (one-sided bounds only clip a ray)
    # and needs no double-sided pivot selection.
    if q == 1:
        h = basis[0]
        t_lo: Optional[int] = None
        t_hi: Optional[int] = None
        nodes = 0

        def intersect(lo_value: Optional[int], hi_value: Optional[int]) -> None:
            nonlocal t_lo, t_hi
            if lo_value is not None:
                t_lo = lo_value if t_lo is None else max(t_lo, lo_value)
            if hi_value is not None:
                t_hi = hi_value if t_hi is None else min(t_hi, hi_value)

        for j, entries in enumerate(incidence):
            if not entries:
                continue
            c = entries[0][1]
            d_lo, d_hi = offset_sides[j]
            if c > 0:
                if d_lo is not None:
                    intersect(_ceil_div(d_lo, c), None)
                if d_hi is not None:
                    intersect(None, d_hi // c)
            else:  # c < 0:  d_lo <= c*t  => t <= floor(d_lo/c); d_hi => t >= ceil(d_hi/c)
                if d_lo is not None:
                    intersect(None, d_lo // c)
                if d_hi is not None:
                    intersect(_ceil_div(d_hi, c), None)
            nodes += 1
        # Reaching this point means some bound excludes the particular, so at
        # least one finite side constrains t; guard the degenerate case too.
        if t_lo is None and t_hi is None:
            return _finish([0] * n, nodes)
        if (t_lo is not None and t_hi is not None and t_lo > t_hi):
            return _infeasible(nodes)
        if t_lo is not None and t_lo > 0:
            t_star = t_lo
        elif t_hi is not None and t_hi < 0:
            t_star = t_hi
        else:
            t_star = 0  # pragma: no cover - zero offset returned earlier
        return _finish([t_star * h[j] for j in range(n)], nodes)

    # The search box is finite only if q independent items bound both sides.
    double_sided = [
        j for j, (d_lo, d_hi) in enumerate(offset_sides)
        if d_lo is not None and d_hi is not None
    ]
    double_sided_rows = [
        [dict(incidence[j]).get(k, 0) for k in range(q)] for j in double_sided
    ]
    pivot_selection = _independent_row_subset(double_sided_rows, q)
    if pivot_selection is None:
        raise UnderconstrainedBounds(
            f"边界不足以限定全部 {q} 个自由校正方向：请为至少 {q} 个行程相互"
            "独立的校正项同时填写最小与最大垫片数"
        )
    pivot_rows = [double_sided[i] for i in pivot_selection]
    pivot_matrix = [
        [dict(incidence[j]).get(k, 0) for k in range(q)] for j in pivot_rows
    ]
    inverse = _invert_fraction(pivot_matrix)
    if inverse is None:  # pragma: no cover - selected as full rank
        raise ArithmeticError("内部校验失败：行程边界子系统不可逆")

    # Exact outer range of every offset coordinate over the parameter box.
    root_limits = [offset_sides[j] for j in pivot_rows]
    parameter_box = []
    for a in range(q):
        lo_fraction = Fraction(0)
        hi_fraction = Fraction(0)
        for b in range(q):
            coefficient = inverse[a][b]
            z_lo, z_hi = root_limits[b]
            if coefficient >= 0:
                lo_fraction += coefficient * z_lo
                hi_fraction += coefficient * z_hi
            else:
                lo_fraction += coefficient * z_hi
                hi_fraction += coefficient * z_lo
        parameter_box.append(
            (_ceil_fraction(lo_fraction), _floor_fraction(hi_fraction))
        )
    t_abs = [max(abs(lo), abs(hi)) for lo, hi in parameter_box]
    outer_max = 0
    for entries in incidence:
        coordinate_max = sum(abs(c) * t_abs[k] for k, c in entries)
        outer_max = max(outer_max, coordinate_max)

    # Trivial lower bound on the optimum: furthest distance any single item
    # must move to enter its interval.
    required = 0
    for d_lo, d_hi in offset_sides:
        if d_lo is not None and d_lo > 0:
            required = max(required, d_lo)
        if d_hi is not None and d_hi < 0:
            required = max(required, -d_hi)

    nodes = 0
    cap = max(1, required)
    while True:
        search = _LatticeSearch(
            incidence, offset_sides, pivot_rows, inverse,
            _Mode("feasibility", cap),
        )
        found = search.run(find_any=True)
        nodes += search.nodes
        if found:
            incumbent_total = search.best_total
            break
        if cap >= outer_max:
            return BoundedAudit(
                solvable=True,
                feasible=False,
                reason="no_bounded_solution",
                particular=particular,
                basis=basis,
                nodes=nodes,
                **common,
            )
        cap = min(2 * cap, outer_max)

    # Exhaustive proof over the finite L1 sublevel set of the incumbent.
    proof = _LatticeSearch(
        incidence, offset_sides, pivot_rows, inverse,
        _Mode("proof", incumbent_total),
    )
    proof.run(find_any=False)
    nodes += proof.nodes
    if proof.best_offsets is None:  # pragma: no cover - incumbent was visited
        raise ArithmeticError("内部校验失败：最优性证明丢失已知可行点")

    offsets = list(proof.best_offsets)
    adjusted = [particular[j] + offsets[j] for j in range(n)]

    # Exact self-check: original equations and every travel bound both hold.
    for i in range(m):
        if sum(matrix[i][j] * adjusted[j] for j in range(n)) != target[i]:
            raise ArithmeticError("内部校验失败：调整方案未复现目标向量")
    if not within_bounds(adjusted):
        raise ArithmeticError("内部校验失败：调整方案越界")

    return BoundedAudit(
        solvable=True,
        feasible=True,
        particular=particular,
        basis=basis,
        adjusted=adjusted,
        offsets=offsets,
        total_abs_offset=proof.best_total,
        nodes=nodes,
        **common,
    )
