"""Arena-specific rule handling without changing historical backtest results."""
import eval7

from engine.game import PokerEngine


class ArenaEngine(PokerEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, competition_rules=True, **kwargs)
        self.acted_at = {}
        self.settlement = None

    def _post_blinds(self):
        super()._post_blinds()
        # A short big blind does not reduce the opening bet when at least two
        # players can still bet. With one active player, only match actual chips.
        if sum(p.is_active for p in self.players) >= 2:
            self.current_bet = max(self.current_bet, self.big_blind)

    def start_hand(self):
        state = super().start_hand()
        if state["type"] == "hand_complete":
            return state
        active = [p for p in self.players if p.is_active]
        if len(active) <= 1:
            if not active or active[0].bet_this_street >= self.current_bet:
                return self._run_it_out()
            return self._build_state(active[0].seat)
        # A heads-up dealer can already be all-in from the small blind.
        if not self.players[state["seat_to_act"]].is_active:
            return self._build_state(self._next_actor(state["seat_to_act"]))
        return state

    def apply_action(self, seat, raw):
        street = self.street
        state = super().apply_action(seat, raw)
        if street == self.street:
            self.acted_at[seat] = self.current_bet
        return state

    def _handle_aggression(self, seat, raise_size):
        super()._handle_aggression(seat, raise_size)
        # Several short all-ins can cumulatively reopen a previous caller.
        if raise_size > 0:
            self._acted_since_full_raise = {
                s for s in self._acted_since_full_raise
                if s == seat or self.current_bet - self.acted_at.get(s, self.current_bet) < self.min_raise
            }

    def _advance_if_street_over(self, last_seat):
        active = [p for p in self.players if p.is_active]
        if len(active) <= 1:
            if not active or active[0].bet_this_street >= self.current_bet:
                return self._run_it_out()
        return super()._advance_if_street_over(last_seat)

    def _advance_street(self):
        self.acted_at.clear()
        if sum(p.is_active for p in self.players) <= 1:
            return self._run_it_out()
        return super()._advance_street()

    def force_fold(self, seat, actor):
        """Leave may fold out of turn without moving the other player's turn."""
        p = self.players[seat]
        if not p.is_active:
            return None
        p.is_folded = True
        self._needs_to_act.discard(seat)
        self.action_log.append({"seat": seat, "action": "fold", "amount": 0})
        self._emit_action(seat, "fold", 0)
        remaining = [p for p in self.players if not p.is_folded]
        if len(remaining) == 1:
            return self._award_uncontested(remaining[0])
        if actor != seat and self.players[actor].is_active:
            if sum(p.is_active for p in self.players) > 1 or self.players[actor].bet_this_street < self.current_bet:
                return self._build_state(actor)
        return self._advance_if_street_over(actor)

    def _showdown(self):
        return self._settle(showdown=True)

    def _award_uncontested(self, winner):
        return self._settle(showdown=False)

    def _compute_side_pots(self):
        # Folded blind amounts should not invent extra pools: adjacent layers
        # with the same eligible winners are one pot.
        merged = []
        for pot in super()._compute_side_pots():
            ids = {p.bot_id for p in pot["eligible"]}
            if merged and ids == {p.bot_id for p in merged[-1]["eligible"]}:
                merged[-1]["amount"] += pot["amount"]
            else:
                merged.append({"amount": pot["amount"], "eligible": pot["eligible"]})
        return merged

    def _settle(self, showdown):
        returned = []
        contributions = sorted(self.players, key=lambda p: p.total_invested, reverse=True)
        excess = contributions[0].total_invested - contributions[1].total_invested
        if excess:
            p = contributions[0]
            p.stack += excess
            p.total_invested -= excess
            self.pot -= excess
            returned.append({"agentId": p.bot_id, "amount": excess})
        contenders = [p for p in self.players if not p.is_folded]
        scores = {p.bot_id: eval7.evaluate(p.hole_cards + self.community_cards)
                  for p in contenders} if showdown else {}
        pots = self._compute_side_pots()
        payouts, winners = {}, []
        for index, pot in enumerate(pots):
            eligible = pot["eligible"]
            best = max(scores[p.bot_id] for p in eligible) if showdown else None
            winning = [p for p in eligible if not showdown or scores[p.bot_id] == best]
            winning.sort(key=lambda p: (p.seat - self.dealer_seat - 1) % self.n)
            share, remainder = divmod(pot["amount"], len(winning))
            for i, p in enumerate(winning):
                amount = share + (i < remainder)
                if not amount:
                    continue
                p.stack += amount
                award = {"amount": amount, "potIndex": index, "potKind": "main" if index == 0 else "side"}
                payout = payouts.setdefault(p.bot_id, {"agentId": p.bot_id, "amount": 0, "potAwards": []})
                payout["amount"] += amount
                payout["potAwards"].append(award)
                winners.append({"bot_id": p.bot_id, "seat": p.seat, "amount": amount, "pot_type": award["potKind"]})
        self.settlement = {"payouts": list(payouts.values()), "returnedBets": returned}
        revealed = {p.bot_id: [str(c) for c in p.hole_cards] for p in contenders} if showdown else {}
        self._check_invariants()
        return self._build_result(winners, showdown, revealed=revealed)
