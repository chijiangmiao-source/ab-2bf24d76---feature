"""HTTP-level tests for the review service."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.server import Handler


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def post_review(self, payload):
        request = urllib.request.Request(
            self.url("/api/review"),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post_audit(self, payload):
        request = urllib.request.Request(
            self.url("/api/audit"),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, text = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text), {"status": "ok"})

    def test_index_page_served(self):
        status, text = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("复核", text)

    def test_static_assets_served(self):
        for path in ("/app.js", "/style.css"):
            status, _ = self.get(path)
            self.assertEqual(status, 200, path)

    def test_path_traversal_blocked(self):
        request = urllib.request.Request(self.url("/../app/server.py"))
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertIn(status, (400, 404))

    def test_review_solvable_exact_bigint(self):
        status, body = self.post_review(
            {
                "variables": ["K1", "K2", "K3"],
                "matrix": [
                    ["9007199254740993", "1", "0"],
                    ["1", "3", "1"],
                    ["0", "2", "4"],
                ],
                "target": ["18014398509481989", "12", "10"],
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["solvable"])
        self.assertEqual(body["solution"], ["2", "3", "1"])
        first = body["constraints"][0]
        self.assertEqual(first["terms"][0]["product"], str(9007199254740993 * 2))
        self.assertEqual(first["sum"], "18014398509481989")
        self.assertTrue(all(c["satisfied"] for c in body["constraints"]))

    def test_review_unsolvable_obstruction(self):
        status, body = self.post_review(
            {
                "variables": ["D1", "D2"],
                "matrix": [["2", "0"], ["0", "4"]],
                "target": ["9007199254740993", "8"],
            }
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["solvable"])
        obstruction = body["obstruction"]
        self.assertEqual(obstruction["type"], "non_divisible")
        self.assertEqual(obstruction["pivot"], "2")
        self.assertEqual(obstruction["transformedTarget"], "9007199254740993")
        self.assertEqual(obstruction["remainder"], "1")
        total = sum(int(term["product"]) for term in obstruction["uRowTerms"])
        self.assertEqual(str(total), obstruction["transformedTarget"])

    def test_json_number_integers_accepted(self):
        status, body = self.post_review(
            {"variables": ["x"], "matrix": [[4]], "target": [8]}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["solvable"])
        self.assertEqual(body["solution"], ["2"])

    def test_float_coefficient_rejected(self):
        status, body = self.post_review(
            {"variables": ["x"], "matrix": [[1.5]], "target": [1]}
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_dimension_mismatch_rejected(self):
        status, body = self.post_review(
            {
                "variables": ["x", "y"],
                "matrix": [["1", "2"]],
                "target": ["1", "2"],
            }
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_unknown_route_404(self):
        request = urllib.request.Request(self.url("/nope"))
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 404)

    # ------------------------------------------------------------- audit
    def test_audit_global_optimum_and_exact_recheck(self):
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2"],
                "matrix": [["1", "1"]],
                "target": ["5"],
                "bounds": [["0", "4"], ["0", "4"]],
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["solvable"])
        self.assertTrue(body["feasible"])
        self.assertEqual(body["solution"], ["5", "0"])
        self.assertEqual(body["adjusted"], ["4", "1"])
        self.assertEqual(body["offsets"], ["-1", "1"])
        self.assertEqual(body["totalAbsOffset"], "2")
        self.assertTrue(all(item["within"] for item in body["items"]))
        constraint = body["constraints"][0]
        self.assertEqual(constraint["terms"][0]["product"], "4")
        self.assertEqual(constraint["terms"][1]["product"], "1")
        self.assertEqual(constraint["sum"], "5")
        self.assertEqual(constraint["target"], "5")
        self.assertTrue(constraint["satisfied"])

    def test_audit_lexicographic_tie_in_variable_order(self):
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2", "K3"],
                "matrix": [["1", "1", "1"]],
                "target": ["3"],
                "bounds": [["0", "2"], ["0", "3"], ["0", "3"]],
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["feasible"])
        self.assertEqual(body["offsets"], ["-1", "0", "1"])

    def test_audit_solvable_but_no_bounded_point(self):
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2"],
                "matrix": [["1", "1"]],
                "target": ["5"],
                "bounds": [["0", "1"], ["0", "1"]],
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["solvable"])
        self.assertFalse(body["feasible"])
        self.assertEqual(body["reason"], "no_bounded_solution")
        # The original reviewable conclusion is preserved.
        self.assertEqual(body["solution"], ["5", "0"])
        self.assertIn("homogeneousBasis", body)

    def test_audit_equation_unsolvable_preserves_obstruction(self):
        status, body = self.post_audit(
            {
                "variables": ["D1", "D2"],
                "matrix": [["2", "0"], ["0", "2"]],
                "target": ["9007199254740993", "4"],
                "bounds": [["0", "100"], ["0", "100"]],
            }
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["solvable"])
        self.assertFalse(body["feasible"])
        self.assertEqual(body["reason"], "equation_unsolvable")
        self.assertEqual(body["obstruction"]["type"], "non_divisible")
        self.assertEqual(body["obstruction"]["transformedTarget"],
                         "9007199254740993")

    def test_audit_illegal_bounds_min_above_max(self):
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2"],
                "matrix": [["1", "1"]],
                "target": ["5"],
                "bounds": [["9", "4"], None],
            }
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("最小垫片数", body["error"])

    def test_audit_illegal_bounds_shape(self):
        status, body = self.post_audit(
            {
                "variables": ["K1"],
                "matrix": [["2"]],
                "target": ["4"],
                "bounds": [["0"]],
            }
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_audit_bounds_float_rejected(self):
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2"],
                "matrix": [["1", "1"]],
                "target": ["5"],
                "bounds": [[1.5, 4], None],
            }
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_audit_bounds_length_mismatch(self):
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2"],
                "matrix": [["1", "1"]],
                "target": ["5"],
                "bounds": [["0", "4"]],
            }
        )
        self.assertEqual(status, 400)
        self.assertIn("bounds", body["error"])

    def test_audit_underconstrained_bounds_422(self):
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2", "K3"],
                "matrix": [["1", "1", "1"]],
                "target": ["100"],
                "bounds": [["0", "5"], None, None],
            }
        )
        self.assertEqual(status, 422)
        self.assertFalse(body["ok"])
        self.assertIn("自由校正方向", body["error"])

    def test_audit_open_sides_accepted(self):
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2"],
                "matrix": [["1", "1"]],
                "target": ["5"],
                "bounds": [["0", None], [None, "4"]],
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["feasible"])
        self.assertEqual(body["totalAbsOffset"], "0")

    def test_audit_bounds_omitted_runs_baseline_review_data(self):
        # No bounds field: audit endpoint still returns the exact solution
        # and the original conclusion stays reviewable.
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2"],
                "matrix": [["1", "1"]],
                "target": ["5"],
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["feasible"])
        self.assertEqual(body["totalAbsOffset"], "0")
        self.assertEqual(body["bounds"], [None, None])

    def test_audit_big_integer_stays_text(self):
        big = "9007199254740993"
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2"],
                "matrix": [[big, "1"]],
                "target": [str(9007199254740993 * 2 + 7)],
                "bounds": [["0", "5"], ["0", "20"]],
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["feasible"])
        self.assertEqual(body["adjusted"], ["2", "7"])
        product = body["constraints"][0]["terms"][0]["product"]
        self.assertEqual(product, str(9007199254740993 * 2))


if __name__ == "__main__":
    unittest.main()
