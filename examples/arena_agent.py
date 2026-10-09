"""Minimal check/call example. Replace choose() with your own poker strategy.

Run from the repository root: python -m examples.arena_agent --config ...
For durable restart recovery and isolated strategies, use competition_client.
"""
import argparse
import json
from pathlib import Path
import time

from competition_client.config import read_key
from competition_client.http_client import APIError, HTTPClient, NetworkError
from competition_client.protocol import validate_discovery, validate_observation


def choose(observation):
    types = {a["type"] for a in observation["actionRequest"]["allowedActions"]}
    return {"type": "check" if "check" in types else "call" if "call" in types else "fold"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).expanduser().read_text(encoding="utf-8"))
    client = HTTPClient(config["base_url"], read_key(config["key_file"]))
    competition_id = config["competition_id"]

    def request(command, body=None):
        while True:
            try:
                return client.request(command, body)
            except NetworkError:
                time.sleep(0.05)  # Keep the exact same Action body on retry.
            except APIError as exc:
                if exc.code != "temporarily_unavailable":
                    raise
                time.sleep(exc.retry_after)

    competition = validate_discovery(request("discovery"), competition_id)
    if competition["status"] != "active":
        return
    request("join", {"competitionId": competition_id})
    table_id = None
    try:
        while True:
            try:
                observation = request("observe", {"competitionId": competition_id})
                validate_observation(observation, competition_id)
                if observation["status"] == "idle" and observation["competitionStatus"] in {"ended", "cancelled"}:
                    print(json.dumps(observation["standing"], ensure_ascii=False))
                    return
                if observation["table"]:
                    table_id = observation["table"]["id"]
                if observation["actionRequest"]:
                    body = {"competitionId": competition_id, "tableId": table_id,
                            "actionRequestId": observation["actionRequest"]["id"], "decision": choose(observation)}
                    request("action", body)
                else:
                    time.sleep(0.02)
            except APIError as exc:
                if exc.code not in {"stale_action_request", "stale_table"}:
                    raise
    except KeyboardInterrupt:
        body = {"tableId": table_id} if table_id else {"competitionId": competition_id}
        try:
            request("leave", body)
        except APIError as exc:
            if exc.code != "stale_table":
                raise
            request("leave", {"competitionId": competition_id})


if __name__ == "__main__":
    main()
