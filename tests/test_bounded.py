"""Unit tests for the travel-limited bounded lattice optimizer."""

import random
import unittest
from fractions import Fraction

from app.bounded import lll_reduce, optimize_within_bounds
from app.diophantine import solve_diophantine


class LllReductionTest(unittest.TestCase):
    def test_single_vector(self):
        self.assertEqual(lll_reduce([[3, -6]]), [[3, -6]])

    def test_preserves_kernel_and_covolume(self):
        rng = random.Random(11)
        for _ in range(40):
            m = rng.randint(1, 3)
            n = rng.randint(2, 5)
            a = [[rng.randint(-5, 5) for _ in range(n)] for _ in range(m)]
            x0 = [rng.randint(-4, 4) for _ in range(n)]
            b = [sum(row[j] * x0[j] for j in range(n)) for row in a]
            result = solve_diophantine(a, b)
            if not result.solvable or not result.homogeneous_basis:
                continue
            reduced = lll_reduce(result.homogeneous_basis)
            # same number of basis vectors, same span dimension
            self.assertEqual(len(reduced), len(result.homogeneous_basis))
            # every reduced vector is a homogeneous solution
            for vector in reduced:
                for i, row in enumerate(a):
                    self.assertEqual(sum(row[j] * vector[j] for j in range(n)), 0)
            # each reduced basis spans the same lattice: the Gram
            # determinants (squared covolume) must agree exactly.
            def gram_det(cols):
                k = len(cols)
                matrix = [
                    [
                        Fraction(sum(cols[j][i] * cols[l][i] for i in range(n)))
                        for l in range(k)
                    ]
                    for j in range(k)
                ]
                det = Fraction(1)
                for c in range(k):
                    pivot = next((r for r in range(c, k) if matrix[r][c] != 0), None)
                    self.assertIsNotNone(pivot)
                    matrix[c], matrix[pivot] = matrix[pivot], matrix[c]
                    if pivot != c:
                        det = -det
                    det *= matrix[c][c]
                    for r in range(c + 1, k):
                        ratio = matrix[r][c] / matrix[c][c]
                        for cc in range(k):
                            matrix[r][cc] -= ratio * matrix[c][cc]
                return det

            self.assertEqual(
                gram_det(reduced), gram_det(result.homogeneous_basis)
            )


class OptimizeWithinBoundsTest(unittest.TestCase):
    def test_particular_solution_in_bounds_is_returned(self):
        # 2x + 3y = 1, particular solution (-1, 1)
        result = solve_diophantine([[2, 3]], [1])
        optimum = optimize_within_bounds(
            result.solution, result.homogeneous_basis, [-1, 0], [1, 5]
        )
        self.assertEqual(optimum.adjustment, [-1, 1])
        self.assertEqual(optimum.deviation, [0, 0])
        self.assertEqual(optimum.total_abs_deviation, 0)

    def test_moves_along_lattice(self):
        # solutions (-1, 1) + t*(3, -2); box K1 in [0,4], K2 in [-3,0]
        result = solve_diophantine([[2, 3]], [1])
        optimum = optimize_within_bounds(
            result.solution, result.homogeneous_basis, [0, -3], [4, 0]
        )
        self.assertEqual(optimum.adjustment, [2, -1])
        self.assertEqual(optimum.deviation, [3, -2])
        self.assertEqual(optimum.total_abs_deviation, 5)
        # the adjustment still satisfies the original coupling exactly
        self.assertEqual(2 * optimum.adjustment[0] + 3 * optimum.adjustment[1], 1)

    def test_infeasible_box(self):
        result = solve_diophantine([[2, 3]], [1])
        self.assertIsNone(
            optimize_within_bounds(
                result.solution, result.homogeneous_basis, [0, 0], [2, 2]
            )
        )

    def test_unique_solution_outside_bounds(self):
        result = solve_diophantine([[2, 0], [0, 2]], [2, 2])
        self.assertEqual(result.homogeneous_basis, [])
        self.assertIsNone(
            optimize_within_bounds(result.solution, [], [5, 0], [9, 9])
        )

    def test_stable_tie_break_by_variable_order(self):
        # x + y + z = 3; in the box x <= 0, 1 <= y,z <= 2 the two total-6
        # moves (0,1,2) and (0,2,1) tie; deviation vectors from (3,0,0):
        # (-3,1,2) vs (-3,2,1) — the former is lexicographically smaller.
        result = solve_diophantine([[1, 1, 1]], [3])
        optimum = optimize_within_bounds(
            result.solution, result.homogeneous_basis, [-10, 1, 1], [0, 2, 2]
        )
        self.assertEqual(optimum.adjustment, [0, 1, 2])
        self.assertEqual(optimum.deviation, [-3, 1, 2])
        self.assertEqual(optimum.total_abs_deviation, 6)

    def test_tie_break_prefers_smaller_early_component(self):
        # force a genuine tie on total deviation with distinct vectors:
        # x + y + z = 0, particular solution (0,0,0); bounds force movement
        # and (-1,1,0) vs (-1,0,1) both total 2; the latter compares
        # smaller at the second component.
        result = solve_diophantine([[1, 1, 1]], [0])
        optimum = optimize_within_bounds(
            result.solution, result.homogeneous_basis, [-1, -1, -1], [-1, 1, 1]
        )
        self.assertEqual(optimum.total_abs_deviation, 2)
        self.assertEqual(optimum.adjustment, [-1, 0, 1])

    def test_big_integer_exactness(self):
        big = 2**62
        result = solve_diophantine([[big, big]], [2**63])
        optimum = optimize_within_bounds(
            result.solution, result.homogeneous_basis, [-2**70, -3], [2**70, -1]
        )
        self.assertEqual(optimum.adjustment, [3, -1])
        self.assertEqual(optimum.total_abs_deviation, 2)
        self.assertEqual(
            big * optimum.adjustment[0] + big * optimum.adjustment[1], 2**63
        )

    def test_invalid_bounds_rejected(self):
        result = solve_diophantine([[2, 3]], [1])
        with self.assertRaises(ValueError):
            optimize_within_bounds(
                result.solution, result.homogeneous_basis, [1, 0], [0, 2]
            )

    def test_random_systems_match_brute_force(self):
        rng = random.Random(20260925)
        for _ in range(120):
            m = rng.randint(1, 3)
            n = rng.randint(1, 4)
            a = [[rng.randint(-5, 5) for _ in range(n)] for _ in range(m)]
            x0 = [rng.randint(-4, 4) for _ in range(n)]
            b = [sum(row[j] * x0[j] for j in range(n)) for row in a]
            result = solve_diophantine(a, b)
            self.assertTrue(result.solvable)
            lower = [rng.randint(-4, 1) for _ in range(n)]
            upper = [lo + rng.randint(0, 4) for lo in lower]
            optimum = optimize_within_bounds(
                result.solution, result.homogeneous_basis, lower, upper
            )
            best = None
            ranges = [range(lo, hi + 1) for lo, hi in zip(lower, upper)]
            for point in _product(ranges):
                if any(
                    sum(row[j] * point[j] for j in range(n)) != b[i]
                    for i, row in enumerate(a)
                ):
                    continue
                deviation = tuple(point[i] - result.solution[i] for i in range(n))
                key = (sum(abs(d) for d in deviation), deviation)
                if best is None or key < best:
                    best = key
            if best is None:
                self.assertIsNone(optimum)
            else:
                self.assertIsNotNone(optimum)
                self.assertEqual(optimum.total_abs_deviation, best[0])
                self.assertEqual(tuple(optimum.deviation), best[1])


def _product(ranges):
    if not ranges:
        yield ()
        return
    for head in ranges[0]:
        for tail in _product(ranges[1:]):
            yield (head,) + tail


if __name__ == "__main__":
    unittest.main()
