from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest

from tests.sohu_helpers import COMPETITION_ID, DISCOVERY, observation
from competition_client.replay import load_replay


class CLITests(unittest.TestCase):
    def exercise(self, check=False, stop=False):
        calls, bodies, seen = [], [], threading.Event()
        obs = observation(request=not stop)
        if obs["actionRequest"]:
            obs["actionRequest"]["deadlineAt"] = (datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                calls.append("discovery")
                self.reply(DISCOVERY)

            def do_POST(self):
                command = self.path.rsplit("/", 1)[-1]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(command)
                bodies.append(body)
                if command == "join":
                    result = {"competitionId": COMPETITION_ID, "status": "queued"}
                elif command == "observe":
                    result = obs
                    seen.set()
                elif command == "action":
                    result = observation("idle", "ended")
                elif command == "leave":
                    result = dict(body, status="accepted")
                else:
                    raise AssertionError(command)
                self.reply(result)

            def reply(self, data):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                key = directory/"key"
                key.write_text("sk_dummy_cli_test")
                key.chmod(0o600)
                config = directory/"client.json"
                config.write_text(json.dumps({"competition_id": COMPETITION_ID, "key_file": str(key),
                                              "base_url": f"http://127.0.0.1:{server.server_port}",
                                              "state_dir": str(directory/"state")}))
                args = [sys.executable, "-W", "ignore", "-m", "competition_client", "--config", str(config)]
                if check:
                    args.append("--check")
                process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    if stop:
                        self.assertTrue(seen.wait(10), "CLI did not reach Observe")
                        process.send_signal(signal.SIGTERM)
                    stdout, stderr = process.communicate(timeout=15)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                self.assertEqual(process.returncode, 0, stderr)
                self.assertNotIn("sk_dummy_cli_test", stdout+stderr)
                journal_path = directory/"state"/"telemetry"/"local_arena"/"replay.sqlite3"
                if check:
                    self.assertFalse(journal_path.exists())
                else:
                    report = load_replay(journal_path)
                    self.assertEqual(report["summary"]["source"], "local_arena")
                    self.assertGreater(report["summary"]["observation_count"], 0)
                    logs = list(journal_path.parent.glob("*.log"))
                    self.assertEqual(len(logs), 1)
                    self.assertIn("source=local_arena", logs[0].read_text())
                    self.assertNotIn("sk_dummy_cli_test", logs[0].read_text())
                    if not stop:
                        previous_calls = list(calls)
                        exported = directory/"export"
                        replay = subprocess.run([sys.executable, "-m", "competition_client.replay", "--config", str(config),
                                                 "--output", str(exported), "--include-snapshots"],
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(replay.returncode, 0, replay.stderr)
                        self.assertEqual(json.loads(replay.stdout)["source"], "local_arena")
                        self.assertEqual(calls, previous_calls)
                        decisions = [json.loads(line) for line in (exported/"decisions.jsonl").read_text().splitlines()]
                        self.assertEqual(decisions[0]["outcome"]["status"], "done")
                        self.assertEqual(decisions[0]["source"], "local_arena")
                        for path in exported.iterdir():
                            self.assertNotIn("sk_dummy_cli_test", path.read_text())
                if check:
                    self.assertEqual(calls, ["discovery"])
                elif stop:
                    self.assertEqual(calls[-1], "leave")
                    self.assertEqual(bodies[-1], {"tableId": obs["table"]["id"]})
                else:
                    self.assertEqual(calls, ["discovery", "join", "observe", "action"])
                    self.assertEqual(set(bodies[-1]), {"competitionId", "tableId", "actionRequestId", "decision"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_check_never_joins(self):
        self.exercise(check=True)

    def test_live_cli_against_local_server(self):
        self.exercise()

    def test_sigterm_leaves_and_stops(self):
        self.exercise(stop=True)

    def test_paired_backtest_cli_preserves_actual_hands(self):
        command = [sys.executable, "-W", "ignore", "backtest.py", "ab",
                   "--a", "bots/custom/nit_folder/bot.py", "--b", "bots/custom/nit_folder/bot.py",
                   "--field", "bots/custom/calling_station/bot.py",
                   "--competition-config", "configs/sohu.backtest.example.json",
                   "--hands", "2", "--matches", "2", "--no-ev", "--json"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["A_minus_B"]["total_delta"], 0)
        self.assertIsNone(data["A_minus_B"]["bb_per_100"])
        self.assertEqual(data["A"]["hands_played"], 4)
