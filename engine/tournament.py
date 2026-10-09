"""
Swiss-tournament pairing + standings + finalist selection.

Pure-Python helpers used by the demo server and any local tournament harness.
The production match queue (BullMQ workers) ships its own copy in the private
ops repo; this module is the canonical reference for participants who want to
run their own multi-bot bake-off locally.
"""


def swiss_pairing(standings, table_size=6):
    """
    Pair bots into tables. `standings` is a list of dicts with bot_id, bot_path,
    cumulative_delta. Returns a list of tables (each table is a list of bot dicts).

    Distribute stragglers across short tables without exceeding table_size.
    """
    sorted_bots = sorted(standings, key=lambda b: -b.get("cumulative_delta", 0))

    return bounded_tables(sorted_bots, table_size)


def bounded_tables(players, table_size=6):
    if type(table_size) is not int or table_size < 2:
        raise ValueError("table_size must be at least two")
    if not players:
        return []
    n_tables = (len(players)+table_size-1)//table_size
    base, extra = divmod(len(players), n_tables)
    tables, offset = [], 0
    for i in range(n_tables):
        size = base + (i < extra)
        tables.append(players[offset:offset+size])
        offset += size
    return tables


def sohu_standings(records, config, preliminary_ranks=None):
    """Exact rates with official tie rules. Equal keys remain explicitly tied."""
    from engine.competition import exact_bb_per_100
    totals = {}
    for record in records:
        bid = record["bot_id"]
        row = totals.setdefault(bid, {"bot_id": bid, "net_result": 0, "hands_played": 0,
                                      "late_net_result": 0, "late_hands": 0})
        row["net_result"] += record["net_result"]
        row["hands_played"] += record["hands_played"]
        if record.get("round", 0) >= 4:
            row["late_net_result"] += record["net_result"]
            row["late_hands"] += record["hands_played"]
    def rate(row):
        return exact_bb_per_100(row["net_result"], row["hands_played"], config.big_blind)
    def tie_key(row):
        value = rate(row)
        primary = (value is not None, value or 0)
        if preliminary_ranks is not None:
            return (*primary, -preliminary_ranks[row["bot_id"]])
        late = exact_bb_per_100(row["late_net_result"], row["late_hands"], config.big_blind)
        return (*primary, row["hands_played"], late or 0)
    ordered = sorted(totals.values(), key=tie_key, reverse=True)
    previous_key, rank, eligible_count = None, 0, 0
    for i, row in enumerate(ordered):
        key = tie_key(row)
        eligible = row["hands_played"] >= max(config.required_hands, config.minimum_hands)
        if eligible:
            eligible_count += 1
            if key != previous_key:
                rank = eligible_count
            previous_key = key
        row["rank"] = rank if eligible else None
        row["pairing_order"] = i+1
        row["bb_per_100"] = float(rate(row)) if rate(row) is not None else None
        row["qualification_eligible"] = eligible
        row["tiebreak_required"] = sum(tie_key(other) == key for other in ordered) > 1
    return ordered


def select_sohu_finalists(standings, n=12):
    eligible = [r for r in standings if r["qualification_eligible"]]
    if len(eligible) > n and eligible[n-1]["rank"] == eligible[n]["rank"]:
        raise ValueError("Qualification boundary tied; official playoff required")
    return eligible[:n]


def compute_standings(all_results):
    """
    `all_results`: list of {bot_id, bot_path, chip_delta}.
    Returns a sorted list of {bot_id, bot_path, cumulative_delta,
    matches_played, best_match_delta}, ranked by:

      1. cumulative_delta (DESC)             — primary: total chips won
      2. matches_played   (ASC)              — fewer matches with same chips = stronger
      3. best_match_delta (DESC)             — best single-match performance
      4. bot_id           (ASC)              — deterministic alphabetic last resort

    The matches_played tiebreaker rewards bots that scored a high
    cumulative chip delta over fewer matches (less variance exposure)
    over bots that ground it out across more rounds. Standard practice
    in chess-style Swiss tournaments adapted to chip scoring.
    """
    totals = {}
    for r in all_results:
        bid = r["bot_id"]
        delta = r["chip_delta"]
        if bid not in totals:
            totals[bid] = {
                "bot_id":           bid,
                "bot_path":         r.get("bot_path", ""),
                "cumulative_delta": 0,
                "matches_played":   0,
                "best_match_delta": delta,
            }
        totals[bid]["cumulative_delta"] += delta
        totals[bid]["matches_played"]   += 1
        if delta > totals[bid]["best_match_delta"]:
            totals[bid]["best_match_delta"] = delta

    return sorted(
        totals.values(),
        key=lambda b: (
            -b["cumulative_delta"],
             b["matches_played"],
            -b["best_match_delta"],
             b["bot_id"],
        ),
    )


def select_finalists(standings, n=64):
    """Top-n bots by cumulative chip delta with documented tiebreakers
    (see compute_standings). Default n=64 matches the qualifier cut."""
    return standings[:n]
