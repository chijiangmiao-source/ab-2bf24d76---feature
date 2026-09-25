"""Unit tests for the exact travel-bounded integer audit."""

import itertools
import math
import random
import unittest

from fractions import Fraction

from app.bounded import (
    AuditSearchLimit,
    UnderconstrainedBounds,
    audit_bounded,
    _independent_row_subset,
    _invert_fraction,
)
from app.diophantine import solve_diophantine


def lattice_parameter_box(basis, offset_sides):
    """Exact finite box for ``t`` covering every bounded lattice point.

    Selects ``q`` independent double-sided offset rows, inverts their
    coefficient submatrix and maps the offset intervals through the exact
    rational inverse.  Returns ``None`` when the double-sided rows do not
    span the free directions (no finite enumeration box).
    """
    q = len(basis)
    n = len(offset_sides)
    incidence = [
        [basis[k][j] for k in range(q)] for j in range(n)
    ]
    double_sided = [
        j for j, (lo, hi) in enumerate(offset_sides)
        if lo is not None and hi is not None
    ]
    chosen = _independent_row_subset(
        [incidence[j] for j in double_sided], q
    )
    if chosen is None:
        return None
    pivot_rows = [double_sided[i] for i in chosen]
    matrix = [incidence[j] for j in pivot_rows]
    inverse = _invert_fraction(matrix)
    box = []
    for a in range(q):
        lo = Fraction(0)
        hi = Fraction(0)
        for b in range(q):
            coefficient = inverse[a][b]
            z_lo, z_hi = offset_sides[pivot_rows[b]]
            if coefficient >= 0:
                lo += coefficient * z_lo
                hi += coefficient * z_hi
            else:
                lo += coefficient * z_hi
                hi += coefficient * z_lo
        box.append(
            range(math.ceil(lo), math.floor(hi) + 1)
        )
    return box


def brute_force_lattice_optimum(audit, bounds):
    """Enumerate every bounded point via the integer lattice parameters."""
    particular = audit.particular
    basis = audit.basis
    q = len(basis)
    n = len(particular)
    offset_sides = []
    for j, (lo, hi) in enumerate(bounds):
        offset_sides.append(
            (None if lo is None else lo - particular[j],
             None if hi is None else hi - particular[j])
        )
    if q == 0:  # unique integer solution: only the particular exists
        if all((lo is None or 0 >= lo) and (hi is None or 0 <= hi)
               for lo, hi in offset_sides):
            return (0, tuple([0] * n))
        return None
    box = lattice_parameter_box(basis, offset_sides)
    if box is None:
        return "unbounded"
    volume = 1
    for interval in box:
        volume *= max(0, interval.stop - interval.start)
        if volume > 200000:
            return "too_large"
    best = None
    for t in itertools.product(*box):
        offsets = [0] * len(particular)
        for k in range(len(basis)):
            for j in range(len(particular)):
                offsets[j] += t[k] * basis[k][j]
        if any((offset_sides[j][0] is not None
                and offsets[j] < offset_sides[j][0])
               or (offset_sides[j][1] is not None
                   and offsets[j] > offset_sides[j][1])
               for j in range(len(particular))):
            continue
        key = (sum(abs(v) for v in offsets), tuple(offsets))
        if best is None or key < best:
            best = key
    return best


class BoundedAuditTest(unittest.TestCase):
    def test_particular_already_inside_bounds(self):
        audit = audit_bounded([[1, 1]], [5], [(0, 5), (0, 5)])
        self.assertTrue(audit.feasible)
        self.assertEqual(audit.adjusted, audit.particular)
        self.assertEqual(audit.total_abs_offset, 0)

    def test_minimum_move_along_lattice(self):
        # x + y = 5, particular [5, 0]; [0, 4]^2 excludes it.
        audit = audit_bounded([[1, 1]], [5], [(0, 4), (0, 4)])
        self.assertTrue(audit.feasible)
        self.assertEqual(audit.adjusted, [4, 1])
        self.assertEqual(audit.offsets, [-1, 1])
        self.assertEqual(audit.total_abs_offset, 2)

    def test_lexicographic_tie_break_in_variable_order(self):
        # p = [3, 0, 0]; first item capped at 2.  Both [-1, 1, 0] and
        # [-1, 0, 1] cost 2; the signed-offset lexicographic minimum is the
        # latter.
        audit = audit_bounded([[1, 1, 1]], [3], [(0, 2), (0, 3), (0, 3)])
        self.assertTrue(audit.feasible)
        self.assertEqual(audit.offsets, [-1, 0, 1])
        self.assertEqual(audit.adjusted, [2, 0, 1])

    def test_solvable_but_no_bounded_integer_point(self):
        audit = audit_bounded([[1, 1]], [5], [(0, 1), (0, 1)])
        self.assertTrue(audit.solvable)
        self.assertFalse(audit.feasible)
        self.assertEqual(audit.reason, "no_bounded_solution")

    def test_equation_unsolvable_reports_obstruction(self):
        audit = audit_bounded([[2]], [3], [(0, 10)])
        self.assertFalse(audit.solvable)
        self.assertFalse(audit.feasible)
        self.assertEqual(audit.reason, "equation_unsolvable")
        self.assertEqual(audit.obstruction.kind, "non_divisible")

    def test_unique_solution_outside_bounds(self):
        audit = audit_bounded([[1, 0], [0, 1]], [3, 4], [(0, 2), (0, 10)])
        self.assertTrue(audit.solvable)
        self.assertFalse(audit.feasible)
        self.assertEqual(audit.reason, "no_bounded_solution")

    def test_fixed_coordinate_outside_bounds(self):
        # Kernel only moves the third correction; the first is locked at 0.
        audit = audit_bounded(
            [[1, 1, 0], [0, 1, 0]], [1, 1], [(5, 10), (1, 1), None]
        )
        self.assertTrue(audit.solvable)
        self.assertFalse(audit.feasible)
        self.assertEqual(audit.reason, "no_bounded_solution")

    def test_underconstrained_bounds_rejected(self):
        with self.assertRaises(UnderconstrainedBounds):
            audit_bounded([[1, 1, 1]], [100], [(0, 5), None, None])

    def test_one_sided_bounds_compatible_when_particular_travels(self):
        audit = audit_bounded([[1, 1]], [5], [(0, None), (None, 4)])
        self.assertTrue(audit.feasible)
        self.assertEqual(audit.total_abs_offset, 0)

    def test_big_integer_exactness(self):
        big = 9007199254740993
        audit = audit_bounded(
            [[big, 1]], [2 * big + 7], [(0, 5), (0, 20)]
        )
        self.assertTrue(audit.feasible)
        self.assertEqual(audit.adjusted, [2, 7])
        # The adjusted point reproduces the >2^53 target without float loss.
        self.assertEqual(big * 2 + 7, 2 * big + 7)

    def test_non_unit_homogeneous_basis_lattice(self):
        # Smith free columns [-2, 1, 0], [-3, 0, 1] span the integer kernel
        # of [2, 4, 6]; offsets that need half-steps must not appear.
        matrix = [[2, 4, 6]]
        audit = audit_bounded(matrix, [10], [(-3, 3), (-3, 3), (-3, 3)])
        self.assertTrue(audit.feasible)
        self.assertEqual(audit.offsets, [-2, 1, 0])
        self.assertEqual(audit.total_abs_offset, 3)

    def test_wide_box_requires_large_exact_move(self):
        audit = audit_bounded(
            [[1, 1]], [10**6], [(0, 10), (0, 10**6)]
        )
        self.assertTrue(audit.feasible)
        self.assertEqual(audit.adjusted, [10, 10**6 - 10])
        self.assertEqual(audit.total_abs_offset, 1999980)

    def test_single_free_direction_closed_form_huge_move(self):
        # The 1-D lattice optimum is the feasible parameter nearest zero;
        # a billion-shim move must resolve in a couple of nodes, not by
        # enumerating billions of points.
        audit = audit_bounded(
            [[1, 1]], [5],
            [(10**9, 2 * 10**9), (-2 * 10**9, 0)],
        )
        self.assertTrue(audit.feasible)
        self.assertEqual(audit.adjusted, [10**9, 5 - 10**9])
        self.assertEqual(audit.total_abs_offset, 2 * (10**9 - 5))
        self.assertLess(audit.nodes, 10)

    def test_single_free_direction_one_sided_only(self):
        # No double-sided pivot exists, but the one free direction is still
        # optimised exactly on the clipped ray: p=[5,0], x1 <= 3.
        audit = audit_bounded([[1, 1]], [5], [(None, 3), None])
        self.assertTrue(audit.feasible)
        self.assertEqual(audit.adjusted, [3, 2])
        self.assertEqual(audit.offsets, [-2, 2])

    def test_single_free_direction_negative_basis_coefficient(self):
        # p=[5,0]; x1 <= 0 forces x2 >= 5, bounded [4,10] -> x2 = 5.
        audit = audit_bounded([[1, 1]], [5], [(None, 0), (4, 10)])
        self.assertTrue(audit.feasible)
        self.assertEqual(audit.adjusted, [0, 5])

    def test_zero_matrix_free_lattice(self):
        # Everything is free; the nearest bounded point is chosen.
        audit = audit_bounded(
            [[0, 0], [0, 0]], [0, 0], [(1, 3), (2, 4)]
        )
        self.assertTrue(audit.feasible)
        self.assertEqual(audit.adjusted, [1, 2])
        self.assertEqual(audit.total_abs_offset, 3)

    def test_random_systems_match_brute_force(self):
        rng = random.Random(20260925)
        compared = 0
        skipped = 0
        for _ in range(400):
            m = rng.randint(1, 2)
            n = rng.randint(2, 4)
            matrix = [[rng.randint(-3, 3) for _ in range(n)] for _ in range(m)]
            if all(all(v == 0 for v in row) for row in matrix):
                continue
            seed = [rng.randint(-3, 3) for _ in range(n)]
            target = [
                sum(matrix[i][j] * seed[j] for j in range(n)) for i in range(m)
            ]
            center = [seed[j] + rng.randint(-2, 2) for j in range(n)]
            bounds = []
            for j in range(n):
                roll = rng.random()
                if roll < 0.75:
                    bounds.append(
                        (center[j] - rng.randint(0, 2),
                         center[j] + rng.randint(0, 2))
                    )
                elif roll < 0.875:
                    bounds.append((center[j] - rng.randint(0, 2), None))
                else:
                    bounds.append((None, center[j] + rng.randint(0, 2)))
            try:
                audit = audit_bounded(matrix, target, bounds)
            except UnderconstrainedBounds:
                skipped += 1
                continue
            expected = brute_force_lattice_optimum(audit, bounds)
            if expected in ("unbounded", "too_large"):
                skipped += 1
                continue
            # Keep brute enumeration cheap; skip boxes that are huge.
            if expected is not None and audit.nodes > 20000:
                skipped += 1
                continue
            if expected is None:
                self.assertFalse(
                    audit.feasible,
                    msg=f"expected infeasible: {matrix} {target} {bounds}",
                )
            else:
                self.assertTrue(audit.feasible)
                self.assertEqual(
                    (audit.total_abs_offset, tuple(audit.offsets)),
                    expected,
                    msg=f"{matrix} {target} {bounds} p={audit.particular}",
                )
                compared += 1
        self.assertGreater(compared, 80)

    def test_two_dimensional_kernel_brute_force(self):
        matrix = [[1, 1, 1]]
        bounds = [(-5, 5), (-5, 5), (20, 40)]
        audit = audit_bounded(matrix, [30], bounds)
        self.assertTrue(audit.feasible)
        p = audit.particular
        best = None
        for t1 in range(-60, 61):
            for t2 in range(-60, 61):
                x = [p[0] - t1 - t2, p[1] + t1, p[2] + t2]
                if all(bounds[j][0] <= x[j] <= bounds[j][1] for j in range(3)):
                    offsets = tuple(x[j] - p[j] for j in range(3))
                    key = (sum(map(abs, offsets)), offsets)
                    if best is None or key < best:
                        best = key
        self.assertEqual((audit.total_abs_offset, tuple(audit.offsets)), best)

    def test_result_adjusted_point_reproduces_target(self):
        # Target built from x = [1, 2, 3] so the system is certainly solvable.
        matrix = [[2, -3, 1], [1, 1, -2]]
        target = [-1, -3]
        audit = audit_bounded(matrix, target, [(-6, 6)] * 3)
        self.assertTrue(audit.feasible)
        for i, row in enumerate(matrix):
            self.assertEqual(
                sum(row[j] * audit.adjusted[j] for j in range(3)), target[i]
            )

    def test_lattice_basis_is_integer_kernel(self):
        matrix = [[2, -3, 1], [1, 1, -2]]
        result = solve_diophantine(matrix, [-1, -3])
        self.assertTrue(result.solvable)
        for h in result.homogeneous_basis:
            for row in matrix:
                self.assertEqual(sum(a * b for a, b in zip(row, h)), 0)


if __name__ == "__main__":
    unittest.main()
