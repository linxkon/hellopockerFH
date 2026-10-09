"""Serialized finite-match lifecycle, independent of HTTP and strategy code."""
from copy import deepcopy
from fractions import Fraction
import hashlib
import threading
import time
import uuid

from competition_client.protocol import ProtocolError, validate_body, validate_decision
from .actions import ActionLedger
from .engine import ArenaEngine
from .protocol import legal_actions, legacy_decision, table_snapshot, timestamp


class ArenaError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Arena:
    def __init__(self, config, reporter=None, clock=time):
        self.config, self.reporter, self.clock = config, reporter, clock
        self.lock = threading.RLock()
        self.blinds = {"small": config.small_blind, "big": config.big_blind}
        self.seat_map = {p["agent_id"]: i for i, p in enumerate(config.participants)}
        self.members = {p["agent_id"]: {
            "name": p["name"], "bankroll": config.initial_bankroll, "stack": 0,
            "joined": False, "queued": False, "seated": False, "ever_seated": False,
            "leaving": False, "net": 0, "hands": 0, "rebuys": 0,
            "actions": 0, "timeouts": 0, "timeouts_in_row": 0,
            "invalid_decisions": 0, "latencies": [],
        } for p in config.participants}
        self.ledger = ActionLedger()
        self.started_at = self.clock.time()
        self.started_mono = self.clock.monotonic()
        self.started = False
        self.status = "active"
        self.outcome = "waiting"
        self.reason = None
        self.table_id = str(uuid.uuid4())
        self.engine = self.state = self.request = None
        self.history = []
        self.completed_hands = 0
        self.hand_start_stacks = {}
        self.last_dealer = config.dealer_seat - 1
        self._save_report()

    def discovery(self):
        cfg = self.config
        return {"competitions": [{
            "id": cfg.competition_id, "name": cfg.name, "type": "playground", "status": self.status,
            "schedule": {"startsAt": timestamp(self.started_at), "endsAt": timestamp(self.started_at + cfg.duration_seconds)},
            "summary": {
                "initialBankroll": cfg.initial_bankroll, "buyIn": cfg.buy_in, "blinds": self.blinds,
                "minPlayers": len(self.members), "maxPlayers": len(self.members),
                "formationWindowSeconds": 0, "actionTimeoutSeconds": cfg.action_timeout,
                "automaticRebuy": cfg.automatic_rebuy,
                "standing": {"metric": "bbPer100", "minimumHands": cfg.minimum_hands},
            },
        }]}

    def handle(self, command, agent_id, body):
        with self.lock:
            # chat is an optional best-effort field in the organizer protocol.
            structural = {k: v for k, v in body.items() if k != "chat"} if isinstance(body, dict) and command == "action" else body
            try:
                validate_body(command, structural)
            except ProtocolError:
                raise ArenaError("invalid_request") from None
            if agent_id not in self.members:
                raise ArenaError("authentication_required")
            if "competitionId" in body and body["competitionId"] != self.config.competition_id:
                raise ArenaError("competition_not_found")
            self.tick()
            if command == "join":
                return self._join(agent_id)
            if command == "leave":
                return self._leave(agent_id, body)
            member = self.members[agent_id]
            if not member["joined"]:
                raise ArenaError("participation_not_found")
            if command == "action":
                duplicate = self.ledger.duplicate(agent_id, body)
                if duplicate is False:
                    raise ArenaError("idempotency_conflict")
                if duplicate is True:
                    return self.observation(agent_id)
            if "tableId" in body and (body["tableId"] != self.table_id or not member["seated"]):
                raise ArenaError("stale_table")
            if command == "action":
                self._action(agent_id, body)
            return self.observation(agent_id)

    def _join(self, agent_id):
        m = self.members[agent_id]
        if self.status != "active":
            raise ArenaError("competition_not_active")
        m["joined"] = True
        if not self.started:
            m["queued"], m["leaving"] = True, False
            if all(p["queued"] for p in self.members.values()):
                self.started, self.outcome = True, "running"
                for p in self.members.values():
                    p["bankroll"] -= self.config.buy_in
                    p["stack"] = self.config.buy_in
                    p["queued"], p["seated"], p["ever_seated"] = False, True, True
                self._start_hand()
            self._save_report()
        return {"competitionId": self.config.competition_id,
                "status": "seated" if m["seated"] else "queued" if m["queued"] else "idle"}

    def _start_hand(self):
        ids = [a for a, m in self.members.items() if m["seated"]]
        next_dealer = min(ids, key=lambda a: (self.seat_map[a] - self.last_dealer - 1) % len(self.members))
        self.last_dealer = self.seat_map[next_dealer]
        self.hand_start_stacks = {a: self.members[a]["stack"] for a in ids}
        # Do not expose the seed to players while a match is running.
        digest = hashlib.sha256(f"{self.config.seed}:{self.completed_hands}".encode()).digest()
        self.engine = ArenaEngine(str(uuid.uuid4()), ids, dealer_seat=ids.index(next_dealer),
                                  starting_stacks=self.hand_start_stacks, seed=int.from_bytes(digest[:8], "big"),
                                  small_blind=self.config.small_blind, big_blind=self.config.big_blind)
        self.state = self.engine.start_hand()
        self.history = [{"street": "preflop", "agentId": self.engine.players[a["seat"]].bot_id,
                         "type": "smallBlind" if a["action"] == "small_blind" else "bigBlind", "amount": a["amount"]}
                        for a in self.engine.action_log[:2]]
        self._set_request()
        if self.state["type"] == "hand_complete":
            self._finish_hand()

    def _set_request(self):
        if self.state["type"] == "hand_complete":
            self.request = None
        else:
            now = self.clock.monotonic()
            self.request = {"id": self.ledger.token(), "started": now,
                            "expires": now + self.config.action_timeout,
                            "deadlineAt": timestamp(self.clock.time() + self.config.action_timeout)}

    def _action(self, agent_id, body):
        if (not self.request or body["actionRequestId"] != self.request["id"]
                or self.engine.players[self.state["seat_to_act"]].bot_id != agent_id
                or self.members[agent_id]["leaving"]):
            raise ArenaError("stale_action_request")
        seat = self.state["seat_to_act"]
        try:
            validate_decision(body["decision"], legal_actions(self.engine, seat))
        except ProtocolError:
            self.members[agent_id]["invalid_decisions"] += 1
            raise ArenaError("invalid_decision") from None
        m = self.members[agent_id]
        m["latencies"].append(self.clock.monotonic() - self.request["started"])
        m["actions"] += 1
        m["timeouts_in_row"] = 0
        self._apply(seat, body["decision"], chat=body.get("chat"))
        self.ledger.commit(agent_id, body)

    def _apply(self, seat, decision, cause=None, chat=None):
        p = self.engine.players[seat]
        street = self.engine.street
        kind = decision["type"]
        amount = (min(max(0, self.engine.current_bet - p.bet_this_street), p.stack) if kind == "call"
                  else p.stack if kind == "allIn" else decision.get("amount", 0))
        self.state = self.engine.apply_action(seat, legacy_decision(decision, p))
        item = {"street": street, "agentId": p.bot_id, "type": kind, "amount": amount}
        if cause:
            item["cause"] = cause
        if isinstance(chat, str):
            chat = chat.strip()[:140]
            if chat and "sk_" not in chat:
                item["chat"] = chat
        self.history.append(item)
        self._set_request()
        if self.state["type"] == "hand_complete":
            self._finish_hand()

    def _finish_hand(self):
        self.completed_hands += 1
        for p in self.engine.players:
            m = self.members[p.bot_id]
            m["net"] += p.stack - self.hand_start_stacks[p.bot_id]
            m["hands"] += 1
            m["stack"] = p.stack
        if self.reporter:
            self.reporter.hand({
                "number": self.completed_hands, "hand_id": self.engine.hand_id,
                "dealer_seat": self.last_dealer, "starting_stacks": self.hand_start_stacks,
                "final_stacks": self.state["final_stacks"], "actions": self.history,
                "community_cards": self.state["community_cards"], "result": self.engine.settlement,
                "hole_cards": {p.bot_id: [str(c) for c in p.hole_cards] for p in self.engine.players},
            })
        if self.completed_hands >= self.config.hands:
            self.status, self.outcome, self.reason = "ended", "completed", "hand_limit"
        self._save_report()

    def _leave(self, agent_id, body):
        m = self.members[agent_id]
        if "tableId" in body and (body["tableId"] != self.table_id or not m["ever_seated"]):
            raise ArenaError("stale_table")
        # An old exact-table leave never touches a new pre-match queue.
        if "competitionId" in body or m["seated"]:
            m["queued"], m["leaving"] = False, True
        if m["seated"] and self.request:
            seat = next(p.seat for p in self.engine.players if p.bot_id == agent_id)
            actor = self.state["seat_to_act"]
            street = self.engine.street
            state = self.engine.force_fold(seat, actor)
            if state is not None:
                self.history.append({"street": street, "agentId": agent_id,
                                     "type": "fold", "amount": 0, "cause": "leave"})
                self.state = state
                if state["type"] == "hand_complete":
                    self.request = None
                    self._finish_hand()
                elif state["seat_to_act"] != actor:
                    self._set_request()
        self._save_report()
        return dict(body, status="accepted")

    def tick(self):
        """Called under the match lock, both by HTTP and a background timer."""
        if self.status == "cancelled":
            return
        if self.status == "active" and self.clock.monotonic() - self.started_mono >= self.config.duration_seconds:
            self.status, self.outcome, self.reason = "ended", "incomplete", "time_limit"
            for m in self.members.values():
                m["queued"] = False
            self._save_report()
        if not self.engine:
            return
        if self.request:
            agent_id = self.engine.players[self.state["seat_to_act"]].bot_id
            if self.members[agent_id]["leaving"]:
                self._leave(agent_id, {"tableId": self.table_id})
                return
        if self.request and self.clock.monotonic() >= self.request["expires"]:
            seat = self.state["seat_to_act"]
            p = self.engine.players[seat]
            m = self.members[p.bot_id]
            m["timeouts"] += 1
            m["timeouts_in_row"] += 1
            if m["timeouts_in_row"] >= self.config.max_consecutive_timeouts:
                m["leaving"] = True
            decision = {"type": "check" if p.bet_this_street == self.engine.current_bet else "fold"}
            self._apply(seat, decision, cause="timeout")
            return
        if self.state["type"] != "hand_complete":
            return
        if self.status != "active" and not any(m["seated"] for m in self.members.values()):
            return
        for m in self.members.values():
            if not m["seated"]:
                continue
            if self.status == "active" and not m["leaving"] and m["stack"] == 0:
                if self.config.automatic_rebuy and m["bankroll"] >= self.config.buy_in:
                    m["bankroll"] -= self.config.buy_in
                    m["stack"] = self.config.buy_in
                    m["rebuys"] += 1
                else:
                    m["leaving"] = True
            if self.status != "active" or m["leaving"]:
                m["bankroll"] += m["stack"]
                m["stack"], m["seated"] = 0, False
        if self.status == "active" and sum(m["seated"] for m in self.members.values()) < 2:
            self.status, self.outcome, self.reason = "ended", "incomplete", "insufficient_players"
            for m in self.members.values():
                m["bankroll"] += m["stack"]
                m["stack"], m["seated"] = 0, False
        if self.status == "active":
            self._start_hand()
        self._save_report()

    def observation(self, agent_id):
        m = self.members[agent_id]
        data = {"competitionId": self.config.competition_id, "agentId": agent_id,
                "competitionType": "playground", "competitionStatus": self.status,
                "status": "seated" if m["seated"] else "queued" if m["queued"] else "idle",
                "bankroll": m["bankroll"], "standing": self._standing(agent_id),
                "table": table_snapshot(self, agent_id) if m["seated"] else None, "actionRequest": None}
        if (m["seated"] and not m["leaving"] and self.request
                and self.engine.players[self.state["seat_to_act"]].bot_id == agent_id):
            data["actionRequest"] = {"id": self.request["id"], "deadlineAt": self.request["deadlineAt"],
                                     "allowedActions": legal_actions(self.engine, self.state["seat_to_act"])}
        return deepcopy(data)

    def _standing(self, agent_id):
        m = self.members[agent_id]
        rate = Fraction(m["net"] * 100, self.config.big_blind * m["hands"]) if m["hands"] else None
        eligible = [a for a, p in self.members.items() if p["hands"] >= self.config.minimum_hands and p["hands"]]
        rank = None
        if agent_id in eligible:
            score = (rate, m["hands"])
            rank = 1 + sum((Fraction(self.members[a]["net"] * 100, self.config.big_blind * self.members[a]["hands"]),
                            self.members[a]["hands"]) > score for a in eligible)
        return {"handsPlayed": m["hands"], "netResult": m["net"], "bbPer100": float(rate) if rate is not None else None,
                "rank": rank}

    def report(self):
        players = []
        for a, m in self.members.items():
            timings = m["latencies"]
            players.append({"agentId": a, "name": m["name"], "seatIndex": self.seat_map[a],
                            **self._standing(a), "bankroll": m["bankroll"], "settledStack": m["stack"],
                            "rebuyCount": m["rebuys"], "acceptedActions": m["actions"],
                            "timeouts": m["timeouts"], "invalidDecisions": m["invalid_decisions"],
                            "meanResponseSeconds": sum(timings) / len(timings) if timings else None,
                            "maxResponseSeconds": max(timings) if timings else None})
        return {"environment": "local-sohu-arena-v1", "competitionId": self.config.competition_id,
                "name": self.config.name, "outcome": self.outcome, "reason": self.reason,
                "configuredHands": self.config.hands, "completedHands": self.completed_hands,
                "elapsedSeconds": self.clock.monotonic() - self.started_mono,
                "config": {k: v for k, v in self.config.to_dict().items() if k != "participants"},
                "players": players,
                "inProgressHand": self.engine.hand_id if self.engine and self.state["type"] != "hand_complete" else None}

    def _save_report(self):
        if self.reporter:
            self.reporter.report(self.report())

    def stop(self, reason="server_stopped"):
        with self.lock:
            unfinished = self.engine and self.state["type"] != "hand_complete"
            if self.outcome not in {"completed", "incomplete"} or unfinished:
                self.status, self.outcome, self.reason = "cancelled", "interrupted", reason
                self.request = None
            self._save_report()
