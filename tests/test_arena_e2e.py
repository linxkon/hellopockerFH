import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


class ArenaE2ETests(unittest.TestCase):
    def test_independent_existing_clients_2_to_6(self):
        for n in range(2, 7):
            with self.subTest(players=n), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "run"
                with socket.socket() as reservation:
                    # Avoid the OS ephemeral client-port range: concurrent
                    # outgoing HTTP connections can take a released port 0.
                    for candidate in range(18000, 19000):
                        try:
                            reservation.bind(("127.0.0.1", candidate))
                            break
                        except OSError:
                            continue
                    else:
                        self.fail("No free test service port")
                    port = reservation.getsockname()[1]
                base_url = f"http://127.0.0.1:{port}"
                hands = 4
                create = subprocess.run([sys.executable, "-m", "local_arena", "init", "--directory", str(directory),
                                         "--players", str(n), "--hands", str(hands), "--port", str(port),
                                         "--base-url", base_url, "--seed", "12"],
                                        cwd=ROOT, capture_output=True, text=True, timeout=10)
                self.assertEqual(create.returncode, 0, create.stderr)
                self.assertNotIn("sk_local_", create.stdout + create.stderr)
                self.assertEqual((directory / "server.json").stat().st_mode & 0o077, 0)
                bot = Path(temporary) / "bot.py"
                bot.write_text('def decide(state):\n    return {"action": "check" if state.get("can_check", True) else "call"}\n')
                handles, processes = [], []
                try:
                    server_log = open(Path(temporary) / "server.log", "w+")
                    handles.append(server_log)
                    server = subprocess.Popen([sys.executable, "-m", "local_arena", "serve", "--config", str(directory / "server.json")],
                                              cwd=ROOT, stdout=server_log, stderr=server_log)
                    processes.append(server)
                    until = time.monotonic() + 10
                    ready = False
                    while time.monotonic() < until and server.poll() is None:
                        try:
                            with urlopen(base_url + "/api/competitions", timeout=0.2) as response:
                                ready = response.status == 200
                            if ready:
                                break
                        except (URLError, TimeoutError):
                            time.sleep(0.02)
                    server_log.flush()
                    server_log.seek(0)
                    self.assertTrue(ready, "Arena service failed to start: " + server_log.read()[-2000:])
                    started = time.monotonic()
                    for i in range(n):
                        log = open(Path(temporary) / f"client-{i}.log", "w+")
                        handles.append(log)
                        key = directory / "participants" / f"agent-{i + 1}" / "agent.key"
                        self.assertEqual(key.stat().st_mode & 0o077, 0)
                        cli = subprocess.Popen([sys.executable, "-m", "competition_client", "--config",
                                                str(key.parent / "client.json"), "--strategy", str(bot)],
                                               cwd=ROOT, stdout=log, stderr=log)
                        processes.append(cli)
                    for p, log in zip(processes[1:], handles[1:]):
                        p.wait(timeout=60)
                        log.seek(0)
                        output = log.read()
                        self.assertEqual(p.returncode, 0, output[-3000:])
                        self.assertNotIn("sk_local_", output)
                        self.assertNotIn("ERROR", output)
                    elapsed = time.monotonic() - started
                    report = json.loads((directory / "report.json").read_text())
                    self.assertEqual(report["outcome"], "completed")
                    self.assertEqual(report["completedHands"], hands)
                    self.assertEqual(sum(p["netResult"] for p in report["players"]), 0)
                    self.assertEqual([p["handsPlayed"] for p in report["players"]], [hands] * n)
                    self.assertEqual(sum(p["timeouts"] + p["invalidDecisions"] for p in report["players"]), 0)
                    self.assertTrue(all(p["settledStack"] == 0 for p in report["players"]))
                    records = [json.loads(line) for line in (directory / "hands.jsonl").read_text().splitlines()]
                    for player in report["players"]:
                        a = player["agentId"]
                        net = sum(h["final_stacks"].get(a, 0) - h["starting_stacks"].get(a, 0) for h in records)
                        self.assertEqual(net, player["netResult"])
                    self.assertGreater(elapsed, 0)
                    server.send_signal(signal.SIGTERM)
                    server.wait(timeout=10)
                    self.assertEqual(server.returncode, 0)
                    before = (directory / "hands.jsonl").read_bytes()
                    retry = subprocess.run([sys.executable, "-m", "local_arena", "serve", "--config", str(directory / "server.json")],
                                           cwd=ROOT, capture_output=True, timeout=10)
                    self.assertEqual(retry.returncode, 1)
                    self.assertEqual((directory / "hands.jsonl").read_bytes(), before)
                finally:
                    for p in reversed(processes):
                        if p.poll() is None:
                            p.terminate()
                            try:
                                p.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                p.kill()
                                p.wait()
                    for handle in handles:
                        handle.close()


if __name__ == "__main__":
    unittest.main()
