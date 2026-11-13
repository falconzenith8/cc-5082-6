import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modhost.panel import ThreadingHTTPServer, PanelHandler, PanelSession, serve


def _request(url, payload=None):
    if payload is None:
        with urllib.request.urlopen(url) as response:
            return response.status, json.load(response)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


class PanelHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = serve(0, "127.0.0.1")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_state_is_kernel_view_with_catalog_controls(self):
        code, state = _request(f"{self.base}/api/state")
        self.assertEqual(code, 200)
        self.assertIn("greedy", state["catalog"])
        self.assertIn("greedy_v2", state["catalog"])
        control_ids = {c["id"] for c in state["controls"]}
        self.assertIn("unload", control_ids)
        self.assertIn("run_sequence", control_ids)

    def test_html_shell_is_served(self):
        with urllib.request.urlopen(f"{self.base}/") as response:
            html = response.read().decode()
        self.assertIn("Mod Host Panel", html)
        self.assertIn("/api/action", html)

    def test_kernel_driven_unload_reinstall_and_scene_switch(self):
        code, data = _request(f"{self.base}/api/action",
                              {"action": "load", "name": "greedy"})
        self.assertEqual((code, data["ok"]), (200, True))
        _request(f"{self.base}/api/action", {"action": "score", "amount": 100})
        code, data = _request(f"{self.base}/api/action",
                              {"action": "unload", "mod": "greedy"})
        self.assertEqual((code, data["ok"]), (200, True))
        self.assertTrue(data["state"]["retired"][-1]["retired"])

        code, data = _request(f"{self.base}/api/action",
                              {"action": "load", "name": "greedy_v2"})
        self.assertEqual((code, data["ok"]), (200, True))
        self.assertEqual(data["result"]["version"], "2.0.0")

        code, data = _request(f"{self.base}/api/action",
                              {"action": "switch_scene", "scene": "level_b"})
        self.assertEqual(data["state"]["epoch"], 2)

    def test_failure_returns_to_host_ledger_not_fake_success(self):
        code, data = _request(f"{self.base}/api/action",
                              {"action": "unload", "mod": "missing"})
        self.assertEqual(code, 409)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"]["kind"], "KeyError")
        self.assertIn("missing", data["error"]["message"])

    def test_lifecycle_case_replays_with_reclamation(self):
        cases = PanelSession().view()  # cheap catalog parse, no actions
        labels = [c["label"] for c in cases["controls"]
                  if c["id"] == "run_sequence"][0]
        code, data = _request(
            f"{self.base}/api/action",
            {"action": "run_sequence", "label": labels},
        )
        self.assertEqual((code, data["ok"]), (200, True))
        self.assertTrue(data["result"]["structural"]["ok"])
        self.assertNotIn("error", json.dumps(data["result"]["trace"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
