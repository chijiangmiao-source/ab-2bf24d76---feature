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

    def post_audit(self, payload):
        request = urllib.request.Request(
            self.url("/api/audit"),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_audit_optimal_zero_deviation(self):
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2"],
                "matrix": [["2", "3"]],
                "target": ["1"],
                "bounds": [
                    {"variable": "K1", "min": "-1", "max": "5"},
                    {"variable": "K2", "min": "0", "max": "5"},
                ],
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["solvable"])
        audit = body["audit"]
        self.assertEqual(audit["status"], "optimal")
        self.assertEqual(audit["adjustment"], ["-1", "1"])
        self.assertEqual(audit["deviation"], ["0", "0"])
        self.assertEqual(audit["totalAbsDeviation"], "0")
        # every audit constraint is an exact recomputation and satisfied
        self.assertTrue(all(c["satisfied"] for c in audit["constraints"]))
        self.assertEqual(audit["constraints"][0]["sum"], "1")
        # the original review conclusion is retained alongside the audit
        self.assertEqual(body["solution"], ["-1", "1"])

    def test_audit_optimal_moves_along_lattice(self):
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2"],
                "matrix": [["2", "3"]],
                "target": ["1"],
                "bounds": [
                    {"variable": "K1", "min": "0", "max": "4"},
                    {"variable": "K2", "min": "-3", "max": "0"},
                ],
            }
        )
        self.assertEqual(status, 200)
        audit = body["audit"]
        self.assertEqual(audit["status"], "optimal")
        self.assertEqual(audit["adjustment"], ["2", "-1"])
        self.assertEqual(audit["deviation"], ["3", "-2"])
        self.assertEqual(audit["totalAbsDeviation"], "5")
        terms = audit["constraints"][0]["terms"]
        self.assertEqual([t["product"] for t in terms], ["4", "-3"])

    def test_audit_infeasible_keeps_review(self):
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2"],
                "matrix": [["2", "3"]],
                "target": ["1"],
                "bounds": [
                    {"variable": "K1", "min": "0", "max": "2"},
                    {"variable": "K2", "min": "0", "max": "2"},
                ],
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["solvable"])
        self.assertEqual(body["audit"]["status"], "infeasible")
        # the reviewable original conclusion is preserved
        self.assertEqual(body["solution"], ["-1", "1"])
        self.assertTrue(all(c["satisfied"] for c in body["constraints"]))

    def test_audit_unsolvable_equations_reported(self):
        status, body = self.post_audit(
            {
                "variables": ["D1", "D2"],
                "matrix": [["2", "0"], ["0", "4"]],
                "target": ["9007199254740993", "8"],
                "bounds": [
                    {"variable": "D1", "min": "0", "max": "10"},
                    {"variable": "D2", "min": "0", "max": "10"},
                ],
            }
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["solvable"])
        self.assertEqual(body["audit"]["status"], "unsolvable")
        self.assertEqual(body["obstruction"]["type"], "non_divisible")

    def test_audit_min_greater_than_max_rejected(self):
        status, body = self.post_audit(
            {
                "variables": ["x"],
                "matrix": [["2"]],
                "target": ["4"],
                "bounds": [{"variable": "x", "min": "9", "max": "1"}],
            }
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_audit_bounds_count_mismatch_rejected(self):
        status, body = self.post_audit(
            {
                "variables": ["x", "y"],
                "matrix": [["1", "1"]],
                "target": ["2"],
                "bounds": [{"variable": "x", "min": "0", "max": "1"}],
            }
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_audit_unknown_variable_rejected(self):
        status, body = self.post_audit(
            {
                "variables": ["x"],
                "matrix": [["2"]],
                "target": ["4"],
                "bounds": [{"variable": "z", "min": "0", "max": "1"}],
            }
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_audit_duplicate_variable_rejected(self):
        status, body = self.post_audit(
            {
                "variables": ["x", "y"],
                "matrix": [["1", "1"]],
                "target": ["2"],
                "bounds": [
                    {"variable": "x", "min": "0", "max": "1"},
                    {"variable": "x", "min": "0", "max": "1"},
                ],
            }
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_audit_missing_bound_field_rejected(self):
        status, body = self.post_audit(
            {
                "variables": ["x"],
                "matrix": [["2"]],
                "target": ["4"],
                "bounds": [{"variable": "x", "min": "0"}],
            }
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_audit_float_bound_rejected(self):
        status, body = self.post_audit(
            {
                "variables": ["x"],
                "matrix": [["2"]],
                "target": ["4"],
                "bounds": [{"variable": "x", "min": 1.5, "max": "3"}],
            }
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_audit_missing_bounds_rejected(self):
        status, body = self.post_audit(
            {"variables": ["x"], "matrix": [["2"]], "target": ["4"]}
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_audit_big_integer_exact_recomputation(self):
        big = "9007199254740993"
        status, body = self.post_audit(
            {
                "variables": ["K1", "K2", "K3"],
                "matrix": [
                    [big, "1", "0"],
                    ["1", "3", "1"],
                    ["0", "2", "4"],
                ],
                "target": ["18014398509481989", "12", "10"],
                "bounds": [
                    {"variable": "K1", "min": "2", "max": "2"},
                    {"variable": "K2", "min": "3", "max": "3"},
                    {"variable": "K3", "min": "1", "max": "1"},
                ],
            }
        )
        self.assertEqual(status, 200)
        audit = body["audit"]
        self.assertEqual(audit["status"], "optimal")
        self.assertEqual(audit["adjustment"], ["2", "3", "1"])
        first = audit["constraints"][0]
        self.assertEqual(first["terms"][0]["product"], str(9007199254740993 * 2))
        self.assertEqual(first["sum"], "18014398509481989")
        self.assertTrue(all(c["satisfied"] for c in audit["constraints"]))

    def test_unknown_route_404(self):
        request = urllib.request.Request(self.url("/nope"))
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
