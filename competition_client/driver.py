"""One serial Join/Observe/Action/Leave state machine."""
import logging
import threading
import time
from .adapter import ObservationAdapter, convert_decision, safe_decision
from .http_client import APIError, NetworkError
from .protocol import (deadline, require, validate_decision, validate_discovery,
                       validate_observation)

LOG = logging.getLogger(__name__)


class StopRequested(Exception):
    pass


class Driver:
    def __init__(self, config, client, store, worker, clock=None, journal=None, public_stream=None):
        self.config, self.client, self.store, self.worker = config, client, store, worker
        self.clock = clock or time
        self.stop = threading.Event()
        self.table_id = store.meta("table_id")
        self.agent_id = store.meta("agent_id")
        self.adapter = ObservationAdapter()
        self.competition_type = None
        self.journal = journal
        self.public_stream = public_stream
        bound = store.meta("competition_id")
        require(bound in (None, config.competition_id), "State directory belongs to another Competition; use a separate directory")
        store.meta("competition_id", config.competition_id)

    def _record(self, kind, **data):
        if self.journal:
            self.journal.event(kind, data)

    def request_stop(self, *_):
        self.stop.set()

    def _wait(self, seconds, leaving=False):
        # Small interruptible slices; Retry-After is never shortened by a cap.
        end = self.clock.monotonic() + seconds
        while self.clock.monotonic() < end:
            if self.stop.is_set() and not leaving:
                raise StopRequested()
            self.clock.sleep(min(0.2, end-self.clock.monotonic()))

    def _request(self, command, body=None, end=None):
        retries = 0
        context = {"competition_id": body["competitionId"], "table_id": body["tableId"],
                   "request_id": body["actionRequestId"]} if command == "action" else {}
        while True:
            if self.stop.is_set() and command != "leave":
                raise StopRequested()
            remaining = None if end is None else end-self.clock.monotonic()
            if remaining is not None and remaining <= 0:
                return None
            try:
                started = self.clock.monotonic()
                self._record("request", command=command, body=body, attempt=retries + 1, **context)
                response = self.client.request(command, body, timeout=remaining)
                self._record("response", command=command, attempt=retries + 1,
                             elapsed_s=self.clock.monotonic() - started, **context)
                return response
            except NetworkError:
                self._record("request_error", command=command, code="network_result_unknown", attempt=retries + 1, **context)
                delay = min(5.0, 2**min(retries, 3))
            except APIError as exc:
                self._record("request_error", command=command, code=exc.code, attempt=retries + 1, **context)
                if exc.code != "temporarily_unavailable":
                    raise
                delay = exc.retry_after
            retries += 1
            LOG.warning("%s retry=%d after ambiguous network/503", command, retries)
            if end is not None and self.clock.monotonic()+delay >= end:
                return None
            self._wait(delay, leaving=command == "leave")

    def discover(self):
        competition = validate_discovery(self._request("discovery"), self.config.competition_id,
                                         self.config.expected_summary)
        self.competition_type = competition["type"]
        self.store.meta("competition_summary", competition["summary"])
        self._record("discovery", competition=competition)
        return competition

    def _observe(self):
        body = {"competitionId": self.config.competition_id}
        if self.table_id:
            body["tableId"] = self.table_id
        return self._request("observe", body)

    def _accept(self, observation):
        validate_observation(observation, self.config.competition_id, self.agent_id)
        require(self.competition_type in (None, observation["competitionType"]), "Competition type changed")
        if self.journal:
            self.journal.observation(observation)
        self.agent_id = observation["agentId"]
        self.table_id = observation["table"]["id"] if observation["table"] else None
        if self.public_stream:
            self.public_stream.follow(self.table_id, self.agent_id)
        hand = (observation["table"] or {}).get("hand") or {}
        seen = self.store.meta("timeout_seen") or {"hand": None, "indices": []}
        if seen["hand"] != hand.get("id"):
            seen = {"hand": hand.get("id"), "indices": []}
        for index, action in enumerate(hand.get("actions", [])):
            if (action.get("agentId") == self.agent_id and action.get("cause") == "timeout"
                    and index not in seen["indices"]):
                LOG.warning("Server timeout action observed hand=%s index=%s", hand.get("id"), index)
                seen["indices"].append(index)
        self.store.meta_many({"agent_id": self.agent_id, "table_id": self.table_id,
                              "standing": observation["standing"], "bankroll": observation["bankroll"],
                              "timeout_seen": seen})
        request = observation["actionRequest"]
        current = (observation["competitionId"], self.table_id, request["id"]) if request else None
        self.store.archive_other_requests(current)
        LOG.info("state=%s competition=%s hands=%s rank=%s", observation["status"],
                 observation["competitionStatus"], (observation["standing"] or {}).get("handsPlayed"),
                 (observation["standing"] or {}).get("rank"))
        return observation

    def _action(self, observation):
        request = observation["actionRequest"]
        key = (observation["competitionId"], observation["table"]["id"], request["id"])
        remaining = deadline(request["deadlineAt"]) - self.clock.time()
        # Absolute wall-clock deadline projected onto monotonic clock once.
        end = self.clock.monotonic()+remaining
        old = self.store.get(key)
        if remaining <= 0:
            self._record("action_skipped", competition_id=key[0], table_id=key[1],
                         hand_id=observation["table"]["hand"]["id"], request_id=request["id"], reason="deadline_expired")
            if old:
                self.store.mark(key, "expired")
            return None
        if old and old[1] in {"done", "expired", "archived", "stale"}:
            self._wait(min(self.config.seated_interval, remaining))
            return None
        state = self.adapter.convert(observation)
        raw, fallback, calculation_s = None, None, None
        if old and old[1] == "rejected":
            # The server explicitly said it did not execute the old body;
            # this Observation must confirm the identical current causal token.
            body = dict(old[0], decision=safe_decision(request["allowedActions"]))
            require(body != old[0], "Safe decision was rejected; stop for protocol investigation")
            validate_decision(body["decision"], request["allowedActions"])
            self.store.revise_rejected(key, body, "400 invalid_decision; current request reconfirmed")
            fallback = "invalid_decision_correction"
        elif old:
            body = old[0]  # byte-equivalent JSON body; never call the worker again
            validate_decision(body["decision"], request["allowedActions"])
        else:
            budget = min(self.config.strategy_timeout, remaining-self.config.submit_reserve)
            started = self.clock.monotonic()
            if budget > 0:
                try:
                    raw = self.worker.decide(state, budget)
                    decision = convert_decision(raw, state, request["allowedActions"])
                except (TimeoutError, RuntimeError, EOFError, OSError):
                    LOG.warning("Strategy failed or exceeded time budget; using check/fold")
                    decision = safe_decision(request["allowedActions"])
                    fallback = "strategy_failure_or_timeout"
            else:
                decision = safe_decision(request["allowedActions"])
                fallback = "insufficient_time_budget"
            calculation_s = self.clock.monotonic() - started
            LOG.info("decision request=%s calculation_s=%.3f", request["id"], self.clock.monotonic()-started)
            if self.stop.is_set():
                raise StopRequested()
            if self.clock.monotonic() >= end:
                return None
            body = {"competitionId": key[0], "tableId": key[1], "actionRequestId": key[2], "decision": decision}
            self.store.save(body)
        self._record("decision", competition_id=key[0], table_id=key[1], hand_id=state["hand_id"],
                     request_id=key[2], state=state, raw_decision=raw, decision=body["decision"],
                     reused=bool(old and old[1] != "rejected"), fallback=fallback,
                     calculation_s=calculation_s, deadline_at=request["deadlineAt"])
        if self.stop.is_set():
            raise StopRequested()
        started = self.clock.monotonic()
        try:
            result = self._request("action", body, end=end)
        except APIError as exc:
            if exc.code == "invalid_decision":
                self.store.mark(key, "rejected", "400 invalid_decision")
                self._record("action_outcome", competition_id=key[0], table_id=key[1],
                             request_id=key[2], status="rejected", reason=exc.code)
                return None
            if exc.code in {"stale_action_request", "idempotency_conflict", "stale_table"}:
                self.store.mark(key, "stale", exc.code)
                self._record("action_outcome", competition_id=key[0], table_id=key[1],
                             request_id=key[2], status="stale", reason=exc.code)
            raise
        if result is None:
            self.store.mark(key, "expired")
            self._record("action_outcome", competition_id=key[0], table_id=key[1], request_id=key[2], status="expired")
            return None
        # Validate before marking done; invalid responses retain recovery body.
        validate_observation(result, self.config.competition_id, self.agent_id)
        if self.journal:
            self.journal.observation(result)
        self.store.mark(key, "done")
        self._record("action_outcome", competition_id=key[0], table_id=key[1], request_id=key[2], status="done",
                     submit_elapsed_s=self.clock.monotonic() - started)
        LOG.info("submitted request=%s elapsed_s=%.3f", request["id"], self.clock.monotonic()-started)
        return result

    def leave(self):
        record = self.store.meta("leave")
        body = record["body"] if record and record["status"] == "pending" else (
            {"tableId": self.table_id} if self.table_id else {"competitionId": self.config.competition_id})
        while True:
            self.store.meta("leave", {"body": body, "status": "pending"})
            try:
                result = self._request("leave", body)
                require(result.get("status") == "accepted" and all(result.get(k) == v for k, v in body.items()), "Invalid Leave response")
            except APIError as exc:
                if exc.code == "stale_table" and "tableId" in body:
                    self.table_id = None
                    self.store.meta("table_id", None)
                    body = {"competitionId": self.config.competition_id}
                    continue
                raise  # no Observe or Join in the leave path
            self.store.meta("leave", {"body": body, "status": "accepted"})
            self.store.meta("engaged", False)
            self.store.meta("table_id", None)
            LOG.info("Leave accepted; driver stopped")
            return

    def run(self):
        record = self.store.meta("leave")
        if record and record["status"] == "pending":
            self.leave()
            return
        observation, join_needed = None, not self.store.meta("engaged")
        try:
            if not join_needed and not self.stop.is_set():
                self.discover()  # identity/config and server clock check on restart
            # A stopped process needs only Leave, even if strategy/discovery fail.
            while True:
                if self.stop.is_set():
                    raise StopRequested()
                try:
                    if join_needed:
                        competition = self.discover()
                        if competition["status"] in {"ended", "cancelled"}:
                            if self.store.meta("engaged"):
                                join_needed = False
                                continue
                            LOG.info("Configured Competition has ended")
                            return
                        if competition["status"] != "active":
                            self._wait(self.config.idle_interval)
                            continue
                        self.store.meta("engaged", True)  # before ambiguous Join
                        joined = self._request("join", {"competitionId": self.config.competition_id})
                        require(joined.get("competitionId") == self.config.competition_id and joined.get("status") in {"idle", "queued", "seated"}, "Invalid Join response")
                        self.table_id = None
                        self.store.meta("table_id", None)
                        if joined["status"] == "idle":
                            self._wait(self.config.idle_interval)
                            continue
                        join_needed = False
                    if observation is None:
                        observation = self._observe()
                    observation = self._accept(observation)
                    if self.stop.is_set():
                        raise StopRequested()
                    status = observation["status"]
                    if status == "idle":
                        if observation["competitionStatus"] in {"ended", "cancelled"}:
                            self.store.meta("engaged", False)
                            LOG.info("Final standing=%s bankroll=%s", observation["standing"], observation["bankroll"])
                            return
                        self._wait(self.config.idle_interval)
                        observation, join_needed = None, True
                    elif observation["actionRequest"] is not None:
                        observation = self._action(observation)
                    else:
                        self._wait(self.config.queued_interval if status == "queued" else self.config.seated_interval)
                        observation = None
                except APIError as exc:
                    code = exc.code
                    if code == "stale_table":
                        self.table_id = None
                        self.store.meta("table_id", None)
                        observation, join_needed = None, False
                    elif code in {"stale_action_request", "idempotency_conflict"}:
                        observation = None
                    elif code in {"competition_not_active", "competition_not_found", "participation_not_found"}:
                        competition = self.discover()
                        if competition["status"] in {"ended", "cancelled"} and code == "participation_not_found":
                            self.store.meta("engaged", False)
                            return
                        # Ended seated sessions still need to Observe and drain.
                        join_needed = competition["status"] == "active" and code == "participation_not_found"
                        if competition["status"] == "scheduled":
                            self._wait(self.config.idle_interval)
                            join_needed = True
                        observation = None
                    else:
                        raise
        except StopRequested:
            self.leave()
