"""Travel-limited audit: bounded optimisation on the integer solution lattice.

Given the exact review of ``A x = b`` — a particular integer solution
``x0`` and a basis of the homogeneous solution lattice — this module
decides whether any lattice point ``x = x0 + B t`` (integer ``t``) lies
inside the per-item shim-travel box ``lower <= x <= upper``.  When at
least one does, it returns the adjustment minimising the total absolute
deviation ``sum |x_i - x0_i|`` from the current exact correction.

Ties are adjudicated stably: among adjustments with the same minimal
total absolute deviation, the one whose deviation vector — taken in the
original variable order — is lexicographically smallest wins.

The lattice basis is first LLL-reduced (an exact integer unimodular
change of lattice coordinates) so that the branch-and-bound boxes stay
tight.  The search branches over the reduced coordinates ``t`` starting
from the particular solution (``t = 0``); every node interval-tightens
the remaining coordinate box from the deviation rows, propagates an
objective-cap row on every deviation component whose sign is fixed in
the subtree, and prunes by row reachability, a Lagrangian lower bound
(the sign vector projected off the lattice, reused at every node) and a
convex relaxation lower bound.  It never rounds a continuous relaxation
and never greedily probes along a single free direction: every bounded
integer solution is either visited or ruled out by a proved bound, so
the returned adjustment is globally optimal.

All arithmetic uses arbitrary-precision integers (the one-time basis
reduction and coordinate box use exact :class:`~fractions.Fraction`
arithmetic).  No floating point is involved anywhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from math import gcd
from typing import List, Sequence


@dataclass
class BoundedOptimum:
    """Globally optimal in-bounds adjustment on the solution lattice."""

    adjustment: List[int]  # bounded optimal correction amounts x
    deviation: List[int]  # x - x0, in variable order
    total_abs_deviation: int  # sum |x_i - x0_i|


def _ceil_div(p: int, q: int) -> int:
    return -((-p) // q)


def _invert_spd(matrix: Sequence[Sequence[int]]) -> List[List[Fraction]]:
    """Exact inverse of a symmetric positive definite integer matrix."""
    n = len(matrix)
    aug = [
        [Fraction(matrix[i][j]) for j in range(n)]
        + [Fraction(1 if i == j else 0) for j in range(n)]
        for i in range(n)
    ]
    for col in range(n):
        pivot = next((r for r in range(col, n) if aug[r][col] != 0), None)
        if pivot is None:
            raise ValueError("齐次解格基线性相关，无法求逆")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pivot_value = aug[col][col]
        aug[col] = [value / pivot_value for value in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0:
                factor = aug[r][col]
                aug[r] = [v - factor * w for v, w in zip(aug[r], aug[col])]
    return [row[n:] for row in aug]


def lll_reduce(basis: Sequence[Sequence[int]], delta: Fraction = Fraction(3, 4)):
    """Exact integer LLL reduction.

    Returns another integer basis of the same lattice, with the shortest
    vector first.  Every reduction/swap step is an integer unimodular
    column operation, so the generated lattice is identical; all
    arithmetic is exact (Fractions only appear inside the
    Gram-Schmidt bookkeeping).
    """
    columns = [list(map(int, column)) for column in basis]
    k = len(columns)
    if k <= 1:
        return columns
    dim = len(columns[0])

    def dot(u, v):
        return sum(x * y for x, y in zip(u, v))

    def gram_schmidt():
        starred = []
        mu = [[Fraction(0) for _ in range(k)] for _ in range(k)]
        norm = [Fraction(0) for _ in range(k)]
        for i in range(k):
            projected = [Fraction(value) for value in columns[i]]
            for j in range(i):
                if norm[j] != 0:
                    mu[i][j] = dot(columns[i], starred[j]) / norm[j]
                    for c in range(dim):
                        projected[c] -= mu[i][j] * starred[j][c]
            starred.append(projected)
            norm[i] = dot(projected, projected)
        return mu, norm

    mu, norm = gram_schmidt()
    i = 1
    while i < k:
        if mu[i][i - 1] != 0:
            q = round(mu[i][i - 1])  # nearest integer size reduction
            if q != 0:
                for c in range(dim):
                    columns[i][c] -= q * columns[i - 1][c]
                mu, norm = gram_schmidt()
        if norm[i] >= (delta - mu[i][i - 1] ** 2) * norm[i - 1]:
            i += 1
        else:
            columns[i], columns[i - 1] = columns[i - 1], columns[i]
            mu, norm = gram_schmidt()
            i = max(i - 1, 1)
    return columns


def _gram_and_inverse(columns, n):
    k = len(columns)
    gram = [
        [sum(columns[j][i] * columns[l][i] for i in range(n)) for l in range(k)]
        for j in range(k)
    ]
    inverse = _invert_spd(gram)
    return gram, inverse


def _coordinate_box(
    columns: Sequence[Sequence[int]], a: Sequence[int], b: Sequence[int]
) -> tuple[List[int], List[int]]:
    """Exact integer box for the lattice coordinates ``t``.

    Any deviation ``delta = B t`` with ``a_i <= delta_i <= b_i`` satisfies
    ``t = W delta`` with ``W = (B^T B)^{-1} B^T`` (exact rationals), so
    interval arithmetic over the deviation box yields valid — and finite —
    integer bounds for every coordinate of ``t``.
    """
    k = len(columns)
    n = len(a)
    _, gram_inv = _gram_and_inverse(columns, n)
    tlo = []
    thi = []
    for j in range(k):
        lo_acc = Fraction(0)
        hi_acc = Fraction(0)
        for i in range(n):
            w = sum(gram_inv[j][l] * columns[l][i] for l in range(k))
            if w > 0:
                lo_acc += w * a[i]
                hi_acc += w * b[i]
            elif w < 0:
                lo_acc += w * b[i]
                hi_acc += w * a[i]
        tlo.append(math.ceil(lo_acc))
        thi.append(math.floor(hi_acc))
    return tlo, thi


def _tighten_box(
    columns: Sequence[Sequence[int]],
    a: Sequence[int],
    b: Sequence[int],
    tlo: List[int],
    thi: List[int],
    start: int = 0,
    base: Sequence[int] | None = None,
) -> bool:
    """Interval propagation: shrink the coordinate box using every row.

    Each row enforces ``a_i <= base_i + sum_{j >= start} columns[j][i] t_j
    <= b_i``; isolating one coordinate at a time yields valid tighter
    integer bounds.  Only infeasible lattice points are removed, so the
    fixpoint is sound.  Returns ``False`` when the box collapses (no
    feasible coordinate left).
    """
    n = len(a)
    k = len(columns)
    if base is None:
        base = [0] * n
    changed = True
    while changed:
        changed = False
        for i in range(n):
            lo_need_row = a[i] - base[i]
            hi_need_row = b[i] - base[i]
            for j in range(start, k):
                coeff = columns[j][i]
                if coeff == 0:
                    continue
                rest_min = 0
                rest_max = 0
                for l in range(start, k):
                    if l == j:
                        continue
                    c = columns[l][i]
                    p = c * tlo[l]
                    q = c * thi[l]
                    if p <= q:
                        rest_min += p
                        rest_max += q
                    else:
                        rest_min += q
                        rest_max += p
                low_need = lo_need_row - rest_max  # coeff * t_j >= low_need
                high_need = hi_need_row - rest_min  # coeff * t_j <= high_need
                if coeff > 0:
                    new_lo = max(tlo[j], _ceil_div(low_need, coeff))
                    new_hi = min(thi[j], high_need // coeff)
                else:
                    new_lo = max(tlo[j], _ceil_div(high_need, coeff))
                    new_hi = min(thi[j], low_need // coeff)
                if new_lo > new_hi:
                    return False
                if new_lo != tlo[j] or new_hi != thi[j]:
                    tlo[j] = new_lo
                    thi[j] = new_hi
                    changed = True
    return True


def _values_around(center: int, lo: int, hi: int):
    """Yield every integer in ``[lo, hi]``, nearest to ``center`` first."""
    if lo > hi:
        return
    start = min(max(center, lo), hi)
    yield start
    step = 1
    while True:
        low = start - step
        high = start + step
        if low < lo and high > hi:
            break
        if low >= lo:
            yield low
        if high <= hi:
            yield high
        step += 1


def _argmin_convex(lo: int, hi: int, f) -> int:
    """Minimiser of a convex integer function on ``[lo, hi]``."""
    while lo < hi:
        mid = (lo + hi) // 2
        if f(mid) <= f(mid + 1):
            hi = mid
        else:
            lo = mid + 1
    return lo


class _LatticeSearch:
    """Exact branch and bound over the reduced lattice coordinates.

    Each search node owns its remaining list of coordinate columns and the
    matching per-coordinate box; choosing a coordinate only permutes these
    local lists, so sibling subtrees never see one another's choices.
    """

    def __init__(self, columns, a, b, tlo, thi, lam_scaled, denominator):
        self._a = a
        self._b = b
        self._n = len(a)
        # Lagrangian dual vector, scaled by a common denominator so that
        # all bound arithmetic stays in arbitrary-precision integers.
        self._lam = lam_scaled
        self._den = denominator
        self._initial_columns = columns
        self._initial_lo = tlo
        self._initial_hi = thi
        self.best_key = None  # (total absolute deviation, deviation tuple)
        self.best_deviation = None

    def _cap_tighten(self, columns, base, rem_lo, rem_hi, cap, lo, hi) -> bool:
        """Propagate the objective cap through fixed-sign deviation rows.

        For every deviation component whose whole reachable interval in
        this subtree has a fixed sign, ``|delta_i|`` equals
        ``sign_i * delta_i`` for every candidate; the necessary inequality
        ``sum_fixed sign_i * delta_i <= cap`` is one linear row over the
        remaining coordinates, tightened by interval propagation.
        """
        a = self._a
        b = self._b
        n = self._n
        k = len(columns)
        coeff = [0] * k
        rhs = cap
        for i in range(n):
            rlo = base[i] + rem_lo[i]
            rhi = base[i] + rem_hi[i]
            if rlo >= 0:
                sign = 1
            elif rhi <= 0:
                sign = -1
            else:
                continue
            rhs -= sign * base[i]
            for l in range(k):
                coeff[l] += sign * columns[l][i]
        changed = True
        while changed:
            changed = False
            for l in range(k):
                c = coeff[l]
                if c == 0:
                    continue
                rest_min = 0
                rest_max = 0
                for l2 in range(k):
                    if l2 == l:
                        continue
                    q = coeff[l2]
                    p = q * lo[l2]
                    r = q * hi[l2]
                    if p <= r:
                        rest_min += p
                        rest_max += r
                    else:
                        rest_min += r
                        rest_max += p
                need = rhs - rest_min  # c * t_l <= need
                if c > 0:
                    new_hi = min(hi[l], need // c)
                    if new_hi != hi[l]:
                        hi[l] = new_hi
                        changed = True
                else:
                    new_lo = max(lo[l], _ceil_div(need, c))
                    if new_lo != lo[l]:
                        lo[l] = new_lo
                        changed = True
                if lo[l] > hi[l]:
                    return False
        return True

    def run(self):
        self._dfs(
            [0] * self._n,
            list(self._initial_columns),
            list(self._initial_lo),
            list(self._initial_hi),
        )
        if self.best_key is None:
            return None
        return self.best_key[0], list(self.best_deviation)

    def _dfs(self, base, columns_in, parent_lo, parent_hi):
        a = self._a
        b = self._b
        n = self._n
        lam = self._lam
        den = self._den
        columns = list(columns_in)
        k = len(columns)
        lo = list(parent_lo)
        hi = list(parent_hi)
        if not _tighten_box(columns, a, b, lo, hi, start=0, base=base):
            return
        # Dynamic variable selection: branch next on the remaining
        # coordinate with the tightest box (ties: larger reduced column).
        # Remaining coordinates are interchangeable, and the Lagrangian
        # dual vector lives in deviation space, so the local permutation
        # leaves the bound valid.
        if k > 1:
            choice = 0
            choice_width = hi[0] - lo[0]
            choice_norm = sum(c * c for c in columns[0])
            for l in range(1, k):
                width = hi[l] - lo[l]
                norm = sum(c * c for c in columns[l])
                if width < choice_width or (
                    width == choice_width and norm > choice_norm
                ):
                    choice = l
                    choice_width = width
                    choice_norm = norm
            if choice != 0:
                columns[0], columns[choice] = columns[choice], columns[0]
                lo[0], lo[choice] = lo[choice], lo[0]
                hi[0], hi[choice] = hi[choice], hi[0]
        rem_lo = [0] * n
        rem_hi = [0] * n
        for l in range(k):
            col = columns[l]
            for i in range(n):
                c = col[i]
                if c:
                    p = c * lo[l]
                    q = c * hi[l]
                    if p <= q:
                        rem_lo[i] += p
                        rem_hi[i] += q
                    else:
                        rem_lo[i] += q
                        rem_hi[i] += p
        if self.best_key is not None:
            if not self._cap_tighten(
                columns, base, rem_lo, rem_hi, self.best_key[0], lo, hi
            ):
                return
            # The cap row may have narrowed the box; rebuild intervals.
            rem_lo = [0] * n
            rem_hi = [0] * n
            for l in range(k):
                col = columns[l]
                for i in range(n):
                    c = col[i]
                    if c:
                        p = c * lo[l]
                        q = c * hi[l]
                        if p <= q:
                            rem_lo[i] += p
                            rem_hi[i] += q
                        else:
                            rem_lo[i] += q
                            rem_hi[i] += p
        # Reachability plus the (integer-scaled) Lagrangian dual bound.
        dbound = 0
        for i in range(n):
            rlo = base[i] + rem_lo[i]
            rhi = base[i] + rem_hi[i]
            if rlo > b[i] or rhi < a[i]:
                return  # no feasible deviation in this subtree
            dbound += lam[i] * base[i]
            low = max(a[i], rlo)
            high = min(b[i], rhi)
            li = lam[i]
            if li > den:
                dbound += den * abs(high) - li * high
            elif li < -den:
                dbound += den * abs(low) - li * low
            elif low <= 0 <= high:
                pass
            elif low > 0:
                dbound += (den - li) * low
            else:
                dbound += (den + li) * abs(high)
        if self.best_key is not None and dbound > den * self.best_key[0]:
            return
        if k == 0:
            total = 0
            for value in base:
                total += value if value >= 0 else -value
            key = (total, tuple(base))
            if self.best_key is None or key < self.best_key:
                self.best_key = key
                self.best_deviation = tuple(base)
            return
        # Convex relaxation for child ordering: coordinates past the first
        # are relaxed to their reachable intervals.  Used only to visit
        # promising values first; it never decides integrality.
        col = columns[0]
        after_lo = [0] * n
        after_hi = [0] * n
        for i in range(n):
            c = col[i]
            if c:
                p = c * lo[0]
                q = c * hi[0]
                if p <= q:
                    after_lo[i] = rem_lo[i] - p
                    after_hi[i] = rem_hi[i] - q
                else:
                    after_lo[i] = rem_lo[i] - q
                    after_hi[i] = rem_hi[i] - p
            else:
                after_lo[i] = rem_lo[i]
                after_hi[i] = rem_hi[i]

        def relax(value):
            total = 0
            for i in range(n):
                shift = base[i] + col[i] * value
                lo_i = shift + after_lo[i]
                hi_i = shift + after_hi[i]
                if lo_i > 0:
                    total += lo_i
                elif hi_i < 0:
                    total -= hi_i
            return total

        centre = _argmin_convex(lo[0], hi[0], relax)
        if self.best_key is not None and relax(centre) > self.best_key[0]:
            return
        rest_columns = columns[1:]
        rest_lo = lo[1:]
        rest_hi = hi[1:]
        for value in _values_around(centre, lo[0], hi[0]):
            for i in range(n):
                c = col[i]
                if c:
                    base[i] += c * value
            self._dfs(base, rest_columns, rest_lo, rest_hi)
            for i in range(n):
                c = col[i]
                if c:
                    base[i] -= c * value


def _sign_lagrangian(columns, a, b):
    """Dual vector for the per-node Lagrangian bound, integer-scaled.

    The sign vector of the deviation box is projected onto the orthogonal
    complement of the lattice column space (``s - B(B^T B)^{-1}B^T s``).
    Such a vector pairs identically with every lattice point, making it a
    valid Lagrangian dual direction at every search node.  The result is
    scaled by a common denominator so it consists solely of integers.
    """
    n = len(a)
    k = len(columns)
    _, gram_inv = _gram_and_inverse(columns, n)
    signs = [0] * n
    for i in range(n):
        if a[i] > 0:
            signs[i] = 1
        elif b[i] < 0:
            signs[i] = -1
    fractions = []
    for i in range(n):
        projected = sum(
            sum(columns[j][i] * gram_inv[j][l] for j in range(k))
            * sum(columns[l][i2] * signs[i2] for i2 in range(n))
            for l in range(k)
        )
        fractions.append(Fraction(signs[i]) - projected)
    denominator = 1
    for value in fractions:
        denominator = denominator * value.denominator // gcd(
            denominator, value.denominator
        )
    scaled = [int(value * denominator) for value in fractions]
    return scaled, denominator


def optimize_within_bounds(
    solution: Sequence[int],
    homogeneous_basis: Sequence[Sequence[int]],
    lower: Sequence[int],
    upper: Sequence[int],
) -> BoundedOptimum | None:
    """Find the in-bounds lattice point closest to the current correction.

    ``solution`` is the particular solution ``x0`` from the exact review,
    ``homogeneous_basis`` spans the integer null-space lattice, and
    ``lower`` / ``upper`` are the per-item shim-travel bounds.  Returns the
    globally optimal :class:`BoundedOptimum` (smallest total absolute
    deviation, ties broken by the lexicographically smallest deviation
    vector in variable order), or ``None`` when no integer solution lies
    inside the travel bounds.
    """
    n = len(solution)
    if n == 0:
        raise ValueError("校正量不能为空")
    if len(lower) != n or len(upper) != n:
        raise ValueError("边界数量必须与校正项数量一致")
    a = []
    b = []
    for i in range(n):
        lo = lower[i]
        hi = upper[i]
        if lo > hi:
            raise ValueError(f"第 {i + 1} 项的下界大于上界")
        a.append(lo - solution[i])
        b.append(hi - solution[i])
    for j, vector in enumerate(homogeneous_basis):
        if len(vector) != n:
            raise ValueError(f"齐次解基第 {j + 1} 个向量长度与校正项数量不一致")

    # The particular solution itself is the unique zero-deviation point;
    # when it lies inside the bounds it is trivially the global optimum.
    if all(a[i] <= 0 <= b[i] for i in range(n)):
        return BoundedOptimum(
            adjustment=list(solution),
            deviation=[0] * n,
            total_abs_deviation=0,
        )

    k = len(homogeneous_basis)
    if k == 0:
        return None  # the unique solution violates the travel bounds

    # LLL-reduce the homogeneous lattice (identical lattice, tighter
    # boxes) and branch on the longest reduced vectors first.
    columns = list(reversed(lll_reduce([list(v) for v in homogeneous_basis])))
    tlo, thi = _coordinate_box(columns, a, b)
    if any(tlo[j] > thi[j] for j in range(k)):
        return None
    if not _tighten_box(columns, a, b, tlo, thi):
        return None
    lam_scaled, denominator = _sign_lagrangian(columns, a, b)

    outcome = _LatticeSearch(
        columns, a, b, tlo, thi, lam_scaled, denominator
    ).run()
    if outcome is None:
        return None
    total, deviation = outcome
    adjustment = [solution[i] + deviation[i] for i in range(n)]
    return BoundedOptimum(
        adjustment=adjustment,
        deviation=deviation,
        total_abs_deviation=total,
    )
