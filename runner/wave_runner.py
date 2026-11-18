#!/usr/bin/env python3
"""Wave runner engine — Dead Brands MCP community waves.

One process = ONE runner's session in ONE wave (see ../DESIGN.md).
Deterministic rotation: handle + district are pure functions of
(runner_id, wave_number), identical on every platform. Credentials: the
runner holds only its handles' recovery codes (env WAVE_CODES_JSON or
the VM custody file); a fresh key is minted per session via arena_recover
(repeatable; revokes older keys — sessions for one handle never overlap).

Stdlib only. No secret value is ever printed or logged.
"""
import hashlib, json, os, random, subprocess, sys, threading, time
from datetime import datetime, timezone

MCP = "https://mcp.deadbrands.co/arena/mcp"
LIVE_URL = "https://mcp.deadbrands.co/arena/live.json"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EPOCH0 = 1794182400          # 2026-11-09T00:00:00Z (Monday) — grid origin; every UTC midnight is a grid point
WAVE_LEN = 2700              # 45 minutes
WAVES_PER_WEEK = 224         # 32/day * 7
DISTRICT_AREA = {"office-lobby": "offices", "plaza": "plaza",
                 "lounge": "commons", "library": "academy",
                 "tower": "hub", "bazaar": "market", "games": "arena"}
DISTRICTS = list(DISTRICT_AREA.keys())
# Gate districts (David 2026-11-12): the Outsider Entrance and the MCP
# Teleporter, each with its own beacon in the city. NOT part of the
# regular rotation — every batch fields ONE dedicated greeter (the
# first persona past the lineup in the slot's ranking) whose whole
# beat is these two stops. The districts are new and may not be live
# yet: the greeter probes candidate presence areas and degrades
# gracefully (holds the Commons, logs the skip). Mirrors the
# Cloudflare worker (cloudflare/worker/src/index.js) bit-for-bit.
GREETER_DISTRICTS = ["outsider-entrance", "mcp-teleporter"]
GREETER_NAMES = {"outsider-entrance": "Outsider Entrance",
                 "mcp-teleporter": "MCP Teleporter"}
GREETER_AREA_CANDIDATES = {"outsider-entrance": ["entrance", "outsider-entrance"],
                           "mcp-teleporter": ["teleporter", "mcp-teleporter"]}
GREETER_FALLBACK_AREA = "commons"  # where the gates stand
NO_CARRY = frozenset()
ROAST_WORDS = ("trash", "noob", "loser", "worst", "pathetic", "easy win",
               "washed", "botched", "terrible", "suck")
POLLS = [
    ("Best way to spend a quiet hour in the city?",
     ["Library deep-dive", "Bazaar haggling", "Tower spectating", "Lounge, obviously"]),
    ("Most underrated game on the board?",
     ["cipher_break", "doodle_duel", "turbo_snail_gp", "bid_wars"]),
    ("Lobby polls: democracy or noise?",
     ["Democracy", "Beautiful noise", "Both, gloriously"]),
]

_id = [0]

def H(*parts):
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)

# ---------------------------------------------------------------- rotation
# Slots: slot = wave * BATCHES + batch (6 batches per 45-min cycle).
# Handle law walks a persona's appearances: 2 in a row free, hard max 3,
# then >=1 appearance out. Cross-over: at each slot boundary, 1-2
# designated runners present in both lineups keep their previous slot's
# handle (law permitting). Scoring is chunk-based (chained sha256 score
# streams) and MUST agree bit-for-bit with the Cloudflare JS mirror
# (cloudflare/worker/src/index.js). Batches 0-2 = CF-home group,
# batches 3-5 = the separable group that lifts to the GitHub lane
# (one process per batch there, WAVE_BATCH=3|4|5).
BATCHES = 6
SLOTS_PER_WEEK = WAVES_PER_WEEK * BATCHES  # 1344
CHAT_DISTRICTS = ["plaza", "lounge", "office-lobby"]

def Hhex(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()

def chunk_scores(hex_digest, n):
    return [int(hex_digest[i * 12:(i + 1) * 12], 16) for i in range(n)]

def scores_for(parts, n):
    """n deterministic 48-bit scores for any n: chained digests,
    5 chunks each. Mirrors the JS scoreStream exactly."""
    out, k = [], 0
    while len(out) < n:
        hex_digest = Hhex(*parts, f"chunk{k}")
        k += 1
        for i in range(5):
            if len(out) >= n:
                break
            out.append(int(hex_digest[i * 12:(i + 1) * 12], 16))
    return out

LINEUP_SIZE = 5  # runner slots per batch — fixed even as the pool grows

def _ranked(runners, slot):
    """All personas for `slot`, in lineup-score order."""
    scores = scores_for(("lineup", slot), len(runners))
    return [r for _, r in sorted(zip(scores, runners), key=lambda p: p[0])]

def lineup_for(runners, slot):
    """The LINEUP_SIZE personas filling `slot`, in lineup-score order.
    Pure function of the slot: chunk scores of one sha256."""
    return _ranked(runners, slot)[:LINEUP_SIZE]

def greeter_for(runners, slot):
    """The slot's dedicated gate greeter: the first persona PAST the
    lineup in the same ranking — never a lineup member, so the regular
    five are untouched. None only if the pool shrinks to lineup size."""
    ranked = _ranked(runners, slot)
    return ranked[LINEUP_SIZE] if len(ranked) > LINEUP_SIZE else None

def carry_set(runners, slot, lineup_prev, lineup_cur):
    """Runner ids designated to carry their handle INTO `slot`: 1-2
    members of the intersection of consecutive lineups, hash-ranked."""
    if not lineup_prev:
        return set()
    prev_ids = {r["id"] for r in lineup_prev}
    inter = [r for r in lineup_cur if r["id"] in prev_ids]
    if not inter:
        return set()
    count = 1 + H("carrycount", slot) % 2
    scores = scores_for(("carry", slot), len(runners))
    by_id = {r["id"]: scores[i] for i, r in enumerate(runners)}
    ranked = sorted(inter, key=lambda r: by_id[r["id"]])
    return {r["id"] for r in ranked[:count]}

def _slot_pick(runner, slot, seq, carry):
    """One persona's handle for one appearance, given its own pick
    history `seq` (handles from its previous appearances only)."""
    handles = runner["handles"]
    scores = scores_for((runner["id"], slot, "order"), len(handles))
    order = [h for _, h in sorted(zip(scores, handles), key=lambda p: p[0])]
    legal = [h for h in order
             if not (len(seq) >= 3 and seq[-1] == seq[-2] == seq[-3] == h)]
    prev = seq[-1] if seq else None
    choice = None
    if prev is not None and runner["id"] in carry:
        run = 0
        for h in reversed(seq):
            if h == prev:
                run += 1
            else:
                break
        if run <= 2:
            choice = prev  # cross-over: same handle continues
    if choice is None:
        choice = next((h for h in legal if h != prev), legal[0])
    return choice

def _walk_to(runners, slot):
    """Joint deterministic walk from the week anchor to `slot`.
    Returns (lineups, picks, greeters, greeter_picks) for every slot
    in the window. A persona's law sequence runs over ALL its
    appearances — regular lineup slots AND greeter slots alike — so a
    handle's in-a-row count never resets by switching roles. Within a
    slot the regular picks land first, then the greeter pick;
    greeters never carry a handle across slots."""
    anchor = (slot // SLOTS_PER_WEEK) * SLOTS_PER_WEEK
    seqs = {r["id"]: [] for r in runners}
    lineups, picks, greeters, greeter_picks = {}, {}, {}, {}
    prev_lineup = None
    for t in range(anchor, slot + 1):
        lineup = lineup_for(runners, t)
        carry = carry_set(runners, t, prev_lineup, lineup)
        p = {}
        for r in lineup:
            choice = _slot_pick(r, t, seqs[r["id"]], carry)
            seqs[r["id"]].append(choice)
            p[r["id"]] = choice
        lineups[t] = lineup
        picks[t] = p
        g = greeter_for(runners, t)
        greeters[t] = g
        greeter_picks[t] = {}
        if g is not None:
            gc = _slot_pick(g, t, seqs[g["id"]], NO_CARRY)
            seqs[g["id"]].append(gc)
            greeter_picks[t] = {g["id"]: gc}
        prev_lineup = lineup
    return lineups, picks, greeters, greeter_picks

def pick_handle(runner, runners, slot):
    """Deterministic handle for (runner, slot), or None when the
    runner is not in that slot's lineup."""
    _, picks, _, _ = _walk_to(runners, slot)
    return picks[slot].get(runner["id"])

def _district_order(runner, slot):
    return sorted(DISTRICTS, key=lambda d: H(runner["id"], "district", slot, d))

def _assign_batch(runners, wave, batch, lineup, handles, carry, prev):
    """District map for one batch (lineup members only): spread (<=2
    per district), carried runners pinned to their previous chat
    district, post-pass keeping >=2 distinct chat districts per batch."""
    slot = wave * BATCHES + batch
    counts, assign, pinned = {}, {}, set()
    if prev is not None:
        for r in lineup:
            rid = r["id"]
            if rid not in carry:
                continue
            if rid not in prev[0] or handles[rid] != prev[0][rid]:
                continue  # carry must have landed
            pd = prev[1][rid]
            if pd not in CHAT_DISTRICTS:
                continue
            if counts.get(pd, 0) >= 2:
                continue
            assign[rid] = pd
            counts[pd] = counts.get(pd, 0) + 1
            pinned.add(rid)
    for r in lineup:
        rid = r["id"]
        if rid in assign:
            continue
        order = _district_order(r, slot)
        d = next((d for d in order if counts.get(d, 0) < 2), order[0])
        assign[rid] = d
        counts[d] = counts.get(d, 0) + 1
    def chat_distinct():
        return len({d for d in assign.values() if d in CHAT_DISTRICTS})
    if chat_distinct() < 2:
        movers = sorted((r for r in lineup
                         if r["id"] not in pinned
                         and assign[r["id"]] not in CHAT_DISTRICTS),
                        key=lambda r: H("chatfix", slot, r["id"]))
        for r in movers:
            if chat_distinct() >= 2:
                break
            for d in _district_order(r, slot):
                if d not in CHAT_DISTRICTS:
                    continue
                if d in assign.values():
                    continue
                if counts.get(d, 0) >= 2:
                    continue
                counts[assign[r["id"]]] -= 1
                assign[r["id"]] = d
                counts[d] = counts.get(d, 0) + 1
                break
    return assign

def cycle_assignment(runners, wave):
    """Full cycle plan: [{lineup, handles, districts, carry, greeter}]
    per batch. Only lineup members appear in a batch's handles/
    districts maps; `greeter` is {"runner", "handle"} for the batch's
    dedicated gate runner (Outsider Entrance + MCP Teleporter) or
    None."""
    first = wave * BATCHES
    lineups, picks, greeters, greeter_picks = _walk_to(runners, first + BATCHES - 1)
    out, prev = [], None
    for b in range(BATCHES):
        slot = first + b
        lineup = lineups[slot]
        carry = carry_set(runners, slot,
                          lineups.get(slot - 1) if slot > 0 else None, lineup)
        districts = _assign_batch(runners, wave, b, lineup, picks[slot],
                                  carry, prev)
        g = greeters[slot]
        out.append({"lineup": lineup, "handles": picks[slot],
                    "districts": districts, "carry": carry,
                    "greeter": ({"runner": g,
                                 "handle": greeter_picks[slot][g["id"]]}
                                if g is not None else None)})
        prev = (picks[slot], districts)
    return out

def wave_assignment(runners, wave):
    """{batch: {runner_id: (handle, district)}} for a whole cycle —
    lineup members only per batch."""
    return {b: {rid: (bt["handles"][rid], bt["districts"][rid])
                for rid in bt["handles"]}
            for b, bt in enumerate(cycle_assignment(runners, wave))}

def pick_line(bank, handle, district, slot_idx, line_no):
    """Deterministic line pick with a 3-slot no-repeat window."""
    if not bank:
        return None
    used = {H(handle, district, s, line_no) % len(bank)
            for s in (slot_idx - 1, slot_idx - 2, slot_idx - 3)}
    idx = H(handle, district, slot_idx, line_no) % len(bank)
    while idx in used and len(used) < len(bank):
        idx = (idx + 1) % len(bank)
    return bank[idx]

def current_wave(platform, ts=None):
    ts = ts or time.time()
    phase = 22 * 60 if platform == "github" else 0
    return int((ts - phase - EPOCH0) // WAVE_LEN)

# ---------------------------------------------------------------- MCP client
def call(tool, args, key=None):
    _id[0] += 1
    payload = {"jsonrpc": "2.0", "id": _id[0], "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    cmd = ["curl", "-sS", "--max-time", "30", "-X", "POST", MCP,
           "-H", "Content-Type: application/json",
           "-H", "Accept: application/json, text/event-stream"]
    if key:
        cmd += ["-H", f"Authorization: Bearer {key}"]
    cmd += ["--data-binary", json.dumps(payload)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    resp = None
    try:
        resp = json.loads(out.stdout.strip())
    except Exception:
        for ln in out.stdout.splitlines():
            c = ln.strip()
            if c.startswith("data:"):
                c = c[5:].strip()
            if c.startswith("{"):
                try: resp = json.loads(c)
                except Exception: pass
    if resp is None:
        raise RuntimeError(f"no response for {tool}")
    r = resp.get("result", {})
    txt = ""
    try: txt = r["content"][0]["text"]
    except Exception: pass
    if r.get("isError"):
        raise RuntimeError(f"{tool}: {txt[:160]}")
    try: return json.loads(txt)
    except Exception: return {"raw": txt}

def fetch_json(url):
    out = subprocess.run(["curl", "-sS", "--max-time", "20", url],
                         capture_output=True, text=True)
    return json.loads(out.stdout)

# ---------------------------------------------------------------- game play
def plan_moves(state, n=20):
    """Asteroid-dodge planner (mirrors the city-resident runner's)."""
    try:
        px, py = state["player"]
    except Exception:
        return ["STAY"] * n
    asteroids = [list(a) for a in state.get("asteroids", [])]
    orbs = [list(o) for o in state.get("orbs", [])]
    gw, gh = state.get("grid_w", 24), state.get("grid_h", 16)
    dirs = {"STAY": (0, 0), "UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)}
    moves = []
    for _ in range(n):
        ast_set = {(x, y) for x, y in asteroids}
        ast_next = {(x - 1, y) for x, y in asteroids}
        best, best_score = "STAY", -1e18
        for name, (dx, dy) in dirs.items():
            nx, ny = px + dx, py + dy
            if not (0 <= nx < gw and 0 <= ny < gh):
                continue
            s = 0
            if (nx, ny) in ast_next: s -= 1000
            if (nx + 1, ny) in ast_set: s -= 120
            if (nx + 2, ny) in ast_set: s -= 60
            if orbs:
                d = min(abs(nx - ox) + abs(ny - oy) for ox, oy in orbs)
                s += max(0, 30 - d * 3)
                if (nx, ny) in {(ox, oy) for ox, oy in orbs}: s += 200
            s += nx
            if s > best_score:
                best_score, best = s, name
        dx, dy = dirs[best]
        px, py = px + dx, py + dy
        moves.append(best)
        asteroids = [[x - 1, y] for x, y in asteroids if x - 1 >= 0]
        orbs = [o for o in orbs if not (o[0] == px and o[1] == py)]
    return moves

ROASTS = []
def vocab_move(s, used, rng):
    """Pick the next move from the state's exposed vocabulary
    (legal_moves or legal). Templates like ROAST:<text> get a real line;
    other <...> templates are skipped. Least-used first (some games pay
    less for repeated structures)."""
    vocab = s.get("legal_moves") or s.get("legal") or []
    out = []
    for v in vocab:
        if not isinstance(v, str):
            continue
        if "<" in v:
            if v.startswith("ROAST:") and ROASTS:
                out.append("ROAST:" + rng.choice(ROASTS))
            continue
        out.append(v)
    if not out:
        return None
    out.sort(key=lambda m: used.get(m.split(":")[0], 0))
    mv = out[0]
    used[mv.split(":")[0]] = used.get(mv.split(":")[0], 0) + 1
    return mv

def play_match(key, game, max_acts=40):
    """Join and play out a match. Versus/heat games seat asynchronously:
    poll arena_get_state and tolerate SEATING errors until the heat
    partner is seated (fleet-proven pattern from arena_player.py)."""
    join = call("arena_join", {"key": key, "game": game})
    mid = join.get("match_id") or join.get("id")
    if not mid:
        # already seated in a match for this game? adopt it; else one retry
        try:
            mine = call("arena_list_matches", {"key": key}, key=key)
            lst = mine.get("matches") or mine.get("items") or []
            for m in lst if isinstance(lst, list) else []:
                if isinstance(m, dict) and m.get("game") == game and \
                        str(m.get("status") or "active").lower() not in ("finished", "ended"):
                    mid = m.get("match_id") or m.get("id")
                    join = {"state": m.get("state") or {}}
                    break
        except Exception:
            pass
    if not mid:
        time.sleep(8)
        join = call("arena_join", {"key": key, "game": game})
        mid = join.get("match_id") or join.get("id")
    if not mid:
        raise RuntimeError("join returned no match_id")
    s = join.get("state") or {}
    # wait for seating: state becomes actionable (or the match ends)
    for _ in range(15):
        if s.get("over") or s.get("legal_moves") or s.get("legal") or "player" in s:
            break
        try:
            st = call("arena_get_state", {"key": key, "match_id": mid})
            s = st.get("state") or st or s
        except Exception:
            pass
        if s.get("status") in ("live", "started", "playing", "active"):
            break
        time.sleep(3)
    acts = 0
    seating_retries = 0
    used_moves = {}
    rng = random.Random(H(game, mid))
    while acts < max_acts:
        if s.get("over") or s.get("tick", 0) >= s.get("max_ticks", 300) or s.get("shields", 1) <= 0:
            break
        if game == "asteroid_dodge" and "player" in s:
            mv = plan_moves(s, 20)
        else:
            one = vocab_move(s, used_moves, rng)
            if one:
                mv = [one]
            elif acts == 0 and seating_retries == 0:
                mv = ["STAY"]
            else:
                break
        try:
            res = call("arena_act", {"key": key, "match_id": mid, "moves": mv})
        except Exception as e:
            if "SEATING" in str(e) and seating_retries < 8:
                seating_retries += 1
                time.sleep(4)
                try:
                    st = call("arena_get_state", {"key": key, "match_id": mid})
                    s = st.get("state") or st or s
                except Exception:
                    pass
                continue
            if "MATCH_OVER" in str(e):
                try:
                    st = call("arena_get_state", {"key": key, "match_id": mid})
                    s = st.get("state") or st or s
                except Exception:
                    pass
                break
            raise
        s = res.get("state") or res or s
        acts += 1
        time.sleep(0.25)
    sc = s.get("score")
    if sc is None and isinstance(s.get("you"), dict):
        sc = s["you"].get("score")
    return {"game": game, "score": sc, "tick": s.get("tick"), "over": s.get("over")}

# ---------------------------------------------------------------- session
class Session:
    def __init__(self, platform, runner, wave, handle, district, recovery_code,
                 lines, duration_s, jitter_s, rot_slot=None):
        self.platform, self.runner, self.wave = platform, runner, wave
        self.rot_slot = wave * BATCHES if rot_slot is None else rot_slot
        self.handle, self.district = handle, district
        self.greeter = district in GREETER_DISTRICTS
        # Gate sessions start on the Commons fallback area and pin the
        # district's real presence area by probing (see do_gate).
        self.area = GREETER_FALLBACK_AREA if self.greeter else DISTRICT_AREA[district]
        self.recovery_code = recovery_code
        self.lines = lines
        self.duration_s, self.jitter_s = duration_s, jitter_s
        self.key = None
        self.actions = []
        self.chat_count = 0
        self.line_no = 0
        self.stop_beacon = threading.Event()
        self.rng = random.Random(H(runner["id"], wave, "rng"))

    def act(self, label):
        self.actions.append(label)

    def say(self, text):
        # GH2 patch 2026-11-17: arena enforces 1 lobby msg / 5s / handle and
        # transient key errors - never let a chat failure kill the session.
        for attempt in range(2):
            try:
                call("arena_lobby_say", {"key": self.key, "text": text}, key=self.key)
                self.chat_count += 1
                self.act(f"chat: {text[:60]}")
                return
            except Exception as e:
                if attempt == 0 and "Rate limit" in str(e):
                    time.sleep(6)
                    continue
                self.act(f"chat failed ({str(e)[:60]})")
                return

    def line(self, bank, **slots):
        self.line_no += 1
        raw = pick_line(bank, self.handle, self.district, self.rot_slot, self.line_no)
        if raw is None:
            return None
        defaults = {"game": self.rng.choice(self.runner["fav_games"]),
                    "peer": "neighbour", "finding": "", "item": "a listing",
                    "score": "", "area": self.district}
        defaults.update(slots)
        try:
            return raw.format(**defaults)
        except Exception:
            return raw

    def beacon(self, status="online"):
        call("arena_presence_beacon",
             {"key": self.key, "status": status, "area": self.area}, key=self.key)

    def beacon_loop(self):
        while not self.stop_beacon.wait(50):
            try: self.beacon()
            except Exception: pass

    def recover_key(self):
        # GH2 patch 2026-11-17: arena_recover is rate-limited per handle;
        # back off and retry instead of dying at session start.
        last = None
        for attempt in range(4):
            try:
                rec = call("arena_recover", {"handle": self.handle,
                                             "recovery_code": self.recovery_code})
                self.key = rec.get("key") or rec.get("new_key")
                if self.key:
                    self.act("key recovered for session")
                    return
                last = RuntimeError("arena_recover returned no key")
            except Exception as e:
                last = e
            if attempt < 3:
                time.sleep(20 * (attempt + 1))
        raise last

    # ---- room awareness: replies, mentions, intros, DMs
    def check_inbox(self):
        try:
            inbox = call("get_agent_inbox", {"key": self.key, "limit": 10}, key=self.key)
        except Exception as e:
            self.act(f"inbox check failed ({str(e)[:60]})")
            return
        intros, mentions, dms = [], [], []
        def scan(o):
            if isinstance(o, dict):
                if o.get("intro_id"): intros.append(o)
                txt = str(o.get("text") or o.get("message") or "")
                frm = o.get("from") or o.get("sender") or o.get("handle")
                if self.handle in txt and frm and frm != self.handle:
                    mentions.append((frm, txt))
                if o.get("dm_id") or (o.get("unread") and frm and txt):
                    dms.append((frm, txt))
                for v in o.values(): scan(v)
            elif isinstance(o, list):
                for v in o: scan(v)
        scan(inbox)
        for intro in intros[:1]:
            try:
                call("arena_intro_respond",
                     {"key": self.key, "intro_id": intro["intro_id"], "action": "accept"},
                     key=self.key)
                self.act(f"accepted intro from {intro.get('from') or intro.get('handle') or '?'}")
            except Exception as e:
                self.act(f"intro accept failed ({str(e)[:60]})")
        for frm, txt in mentions[:1]:
            bank = (self.lines["defense_gruff"] if self.runner["attitude"] == "gruff"
                    and any(w in txt.lower() for w in ROAST_WORDS)
                    else self.lines["defense"] if any(w in txt.lower() for w in ROAST_WORDS)
                    else self.lines["reply_general"])
            ln = self.line(bank, peer=frm)
            if ln: self.say(ln)
        for frm, txt in dms[:1]:
            try:
                reply = self.line(self.lines["reply_general"], peer=frm) or \
                    "hey — got your message, I'm around this wave. what's up?"
                call("arena_dm_send", {"key": self.key, "to": frm, "text": reply}, key=self.key)
                self.act(f"DM reply to {frm}")
            except Exception as e:
                self.act(f"DM reply failed ({str(e)[:60]})")

    def read_room(self):
        try:
            feed = call("arena_lobby_read", {"limit": 15})
        except Exception:
            return []
        msgs = feed.get("messages") or feed.get("lobby") or []
        out = []
        for m in msgs if isinstance(msgs, list) else []:
            if isinstance(m, dict):
                frm = m.get("handle") or m.get("from") or m.get("author")
                txt = m.get("text") or m.get("message") or ""
                if frm and frm != self.handle and txt:
                    out.append((frm, txt))
        return out

    # ---- district behaviours
    def do_games(self):
        game = self.rng.choice(self.runner["fav_games"])
        try:
            live = fetch_json(LIVE_URL)
            playable = [g for g, v in (live.get("games") or {}).items()
                        if isinstance(v, dict) and v.get("arena_playable")]
            if playable and game not in playable:
                game = self.rng.choice(playable)
        except Exception:
            pass
        res = play_match(self.key, game)
        self.act(f"played {res['game']} score={res['score']} over={res['over']}")
        ln = self.line(self.lines["game_after"], game=res["game"], score=res["score"])
        if ln: self.say(ln)

    def do_bazaar(self):
        try: call("bazaar_claim_daily", {"key": self.key}, key=self.key)
        except Exception: pass
        item_name = "a listing"
        try:
            shop = call("bazaar_shop_browse", {"key": self.key}, key=self.key)
            items = shop.get("items") or shop.get("catalog") or []
            named = [i for i in items if isinstance(i, dict)]
            if named:
                item_name = str(named[0].get("name") or named[0].get("title") or item_name)
            self.act(f"bazaar browsed {len(named)} shop items")
            wallet = call("bazaar_wallet", {"key": self.key}, key=self.key)
            bal = wallet.get("balance") or wallet.get("credits") or 0
            cheap = [i for i in named if isinstance(i.get("price"), (int, float))
                     and 0 < i["price"] <= 25]
            if cheap and isinstance(bal, (int, float)) and bal > 150 and \
                    self.rng.random() < 0.35:
                it = self.rng.choice(cheap)
                cid = it.get("id") or it.get("catalog_id")
                call("bazaar_shop_buy", {"key": self.key, "catalog_id": cid}, key=self.key)
                self.act(f"bazaar bought {cid} for {it.get('price')}")
                item_name = str(it.get("name") or item_name)
        except Exception as e:
            self.act(f"bazaar partial ({str(e)[:60]})")
        try:
            feed = call("booth_feed", {"key": self.key, "limit": 10}, key=self.key)
            self.act("booth tape read")
        except Exception:
            pass
        ln = self.line(self.lines["district"]["bazaar"], item=item_name)
        if ln: self.say(ln)

    def do_library(self):
        finding = None
        try:
            live = fetch_json(LIVE_URL)
            finding = (f"live feed right now: {live.get('bots_online', '?')} bots online, "
                       f"{len(live.get('games') or {})} games on the catalog")
            self.act("study: read live.json")
        except Exception:
            pass
        try:
            cat = call("arena_catalog", {})
            n = len(cat.get("games") or cat.get("catalog") or []) if isinstance(cat, dict) else 0
            if n: self.act(f"study: arena_catalog ({n} entries)")
        except Exception:
            pass
        if not finding:
            finding = pick_line(self.lines["study_findings_intro"], self.handle,
                                self.district, self.rot_slot, 99)
        self.study_note = finding
        self.act(f"study note: {finding[:80]}")
        ln = self.line(self.lines["district"]["library"], finding=finding)
        if ln: self.say(ln)

    def do_tower(self):
        try:
            boards = call("colosseum_boards", {"key": self.key}, key=self.key)
            self.act("tower: colosseum boards read")
        except Exception as e:
            self.act(f"tower boards failed ({str(e)[:50]})")
        try:
            live = call("arena_live_matches", {"limit": 10})
            ms = live.get("matches") or live.get("live") or []
            if isinstance(ms, list) and ms:
                m = ms[0]
                mid = m.get("match_id") or m.get("id") if isinstance(m, dict) else None
                if mid:
                    call("arena_spectate", {"match_id": mid})
                    self.act(f"tower: spectated match {mid}")
        except Exception:
            pass
        ln = self.line(self.lines["district"]["tower"])
        if ln: self.say(ln)

    def do_plaza(self):
        try:
            polls = call("arena_poll_list", {"limit": 5})
            plist = polls.get("polls") or polls.get("items") or []
            open_polls = [p for p in plist if isinstance(p, dict)
                          and str(p.get("status") or "open").lower()
                          not in ("closed", "ended", "expired")] \
                if isinstance(plist, list) else []
            voted = False
            for p in self.rng.sample(open_polls, len(open_polls)):
                pid = p.get("poll_id") or p.get("id")
                opts = p.get("options") or []
                if not (pid and opts):
                    continue
                try:
                    call("arena_poll_vote", {"key": self.key, "poll_id": pid,
                                             "option": self.rng.randrange(len(opts))},
                         key=self.key)
                    self.act(f"plaza: voted in poll {pid}")
                    voted = True
                    break
                except Exception:
                    continue  # closed between list and vote — try the next
            if not voted and self.rng.random() < 0.5:
                q, opts = self.rng.choice(POLLS)
                call("arena_poll_create", {"key": self.key, "question": q,
                                           "options": opts, "ttl_s": 3600}, key=self.key)
                self.act(f"plaza: created poll '{q[:40]}'")
        except Exception as e:
            self.act(f"plaza poll partial ({str(e)[:50]})")
        ln = self.line(self.rng.choice([self.lines["district"]["plaza"],
                                        self.lines["question_hooks"]]))
        if ln: self.say(ln)

    def do_gate(self, district, room):
        """One gate stop (Outsider Entrance / MCP Teleporter): probe
        the district's candidate presence areas and pin the first
        that answers; if none answers (district not live yet), hold
        the current area, log the skip, and greet anyway — the run
        never hard-fails on a missing district."""
        pinned = None
        for cand in GREETER_AREA_CANDIDATES[district]:
            if cand == self.area:
                pinned = cand
                break
            keep = self.area
            self.area = cand
            try:
                self.beacon()
                pinned = cand
                break
            except Exception:
                self.area = keep
        if pinned:
            self.act(f"gate stop: {GREETER_NAMES[district]} @ {pinned}")
        else:
            self.act(f"gate skip: {GREETER_NAMES[district]} not live yet — holding @ {self.area}")
        newcomers = [f for f, _ in room if f not in
                     {h for r in ALL_RUNNERS for h in r["handles"]}]
        ln = None
        if district == "outsider-entrance" and newcomers:
            ln = self.line(self.lines["greet_newcomer"], peer=self.rng.choice(newcomers))
        if not ln:
            ln = self.line(self.lines["district"].get(district)
                           or self.lines["question_hooks"])
        if ln: self.say(ln)

    def do_lounge(self):
        try:
            pet = call("pet_presence", {"key": self.key}, key=self.key)
            if isinstance(pet, dict) and pet.get("found"):
                call(self.rng.choice(["pet_play", "pet_feed"]), {"key": self.key}, key=self.key)
                self.act("lounge: pet care")
        except Exception:
            pass
        ln = self.line(self.lines["district"]["lounge"])
        if ln: self.say(ln)

    def do_office(self, room):
        newcomers = [f for f, _ in room if f not in
                     {h for r in ALL_RUNNERS for h in r["handles"]}]
        if newcomers and self.rng.random() < 0.6:
            ln = self.line(self.lines["greet_newcomer"], peer=self.rng.choice(newcomers))
            if ln: self.say(ln)
        else:
            ln = self.line(self.lines["district"]["office-lobby"])
            if ln: self.say(ln)

    # ---- main
    def run(self):
        t0 = time.time()
        rec = {"ts_start": datetime.now(timezone.utc).isoformat(),
               "platform": self.platform, "wave": self.wave,
               "slot": self.rot_slot, "batch": self.rot_slot % BATCHES,
               "runner": self.runner["id"], "persona": self.runner["persona"],
               "handle": self.handle, "district": self.district, "area": self.area}
        try:
            if self.jitter_s > 0:
                time.sleep(self.jitter_s)
            self.recover_key()
            self.beacon()
            self.act(f"beacon online @ {self.area}")
            th = threading.Thread(target=self.beacon_loop, daemon=True)
            th.start()
            self.check_inbox()
            room = self.read_room()
            # opening line (district voice, or a reply when the room is talking)
            if room and self.rng.random() < 0.45:
                frm, _txt = self.rng.choice(room)
                ln = self.line(self.lines["reply_general"], peer=frm)
                if ln: self.say(ln)
            deadline = t0 + self.jitter_s + self.duration_s
            if self.greeter:
                # Gate beat: Entrance stop, a beat, Teleporter stop.
                for gd in GREETER_DISTRICTS:
                    if time.time() > deadline - 15:
                        break
                    self.district = gd
                    try:
                        self.do_gate(gd, room)
                    except Exception as e:
                        self.act(f"{gd} failed ({str(e)[:80]})")
                    room = []
                    wait = min(self.rng.uniform(45, 75),
                               max(5, deadline - time.time() - 15))
                    if wait > 0: time.sleep(wait)
            else:
                plan = [self.district]
                if self.rng.random() < 0.55:
                    plan.append("games")
                steps = {"office-lobby": lambda: self.do_office(room),
                         "plaza": self.do_plaza, "lounge": self.do_lounge,
                         "library": self.do_library, "tower": self.do_tower,
                         "bazaar": self.do_bazaar, "games": self.do_games}
                for step in plan:
                    if time.time() > deadline - 20:
                        break
                    try:
                        steps[step]()
                    except Exception as e:
                        self.act(f"{step} failed ({str(e)[:80]})")
                    wait = min(self.rng.uniform(30, 90), max(5, deadline - time.time() - 15))
                    if wait > 0: time.sleep(wait)
            if self.chat_count < 2 and time.time() < deadline - 10:
                ln = self.line(self.rng.choice([self.lines["district"][self.district],
                                                self.lines["question_hooks"]]))
                if ln: self.say(ln)
            ln = self.line(self.lines["signoff"])
            if ln and self.rng.random() < 0.7: self.say(ln)
        except Exception as e:
            rec["error"] = str(e)[:200]
        finally:
            self.stop_beacon.set()
            if self.key:
                try: self.beacon("away")
                except Exception: pass
                try: call("arena_presence_offline", {"key": self.key}, key=self.key)
                except Exception: pass
            rec["actions"] = self.actions
            rec["area"] = self.area
            rec["district"] = self.district
            rec["role"] = "greeter" if self.greeter else "runner"
            rec["ts_end"] = datetime.now(timezone.utc).isoformat()
            rec["summary"] = (f"{self.handle} ({self.runner['persona']}) in {self.district}: "
                              f"{len(self.actions)} actions, {self.chat_count} chat lines")
        return rec

ALL_RUNNERS = []

def load_config():
    personas = json.load(open(os.path.join(ROOT, "shared", "personas.json")))
    lines = json.load(open(os.path.join(ROOT, "shared", "lines.json")))
    return personas["runners"], lines

def recovery_codes(platform, runner_id):
    """Recovery codes for one runner: env WAVE_CODES_JSON
    ({runner_id: {handle: code}}) wins; else the VM custody file."""
    raw = os.environ.get("WAVE_CODES_JSON")
    if raw:
        return json.loads(raw).get(runner_id, {})
    cust = json.load(open(os.path.expanduser("~/.config/arena/wave-custody.json")))
    return {h: v["recovery_code"] for h, v in cust.items()
            if v.get("slice") == runner_id}

def main():
    global ALL_RUNNERS, ROASTS
    platform = os.environ.get("WAVE_PLATFORM", "local-smoke")
    runner_id = os.environ.get("WAVE_RUNNER")
    runners, lines = load_config()
    ROASTS = lines.get("roasts", [])
    ALL_RUNNERS = runners
    mine = [r for r in runners if r["id"] == runner_id]
    if not mine:
        print(f"unknown runner {runner_id}; set WAVE_RUNNER", file=sys.stderr)
        return 2
    runner = mine[0]
    plat_runners = [r for r in runners if r["platform"] ==
                    ("github" if platform.startswith("github") else
                     "cloudflare" if platform.startswith("cloudflare") else runner["platform"])]
    wave = int(os.environ.get("WAVE_N") or current_wave(runner["platform"]))
    batch = int(os.environ.get("WAVE_BATCH") or 0)
    slot = wave * BATCHES + batch
    plan = cycle_assignment(plat_runners, wave)[batch]
    mine = {rid: (plan["handles"][rid], plan["districts"][rid])
            for rid in plan["handles"]}
    g = plan["greeter"]
    is_greeter = g is not None and g["runner"]["id"] == runner_id \
        and runner_id not in mine
    if runner_id not in mine and not is_greeter:
        print(f"{runner_id} is not in the lineup for wave {wave} batch {batch}; nothing to run")
        return 0
    if is_greeter:
        handle, district = g["handle"], GREETER_DISTRICTS[0]
    else:
        handle, district = mine[runner_id]
    codes = recovery_codes(platform, runner_id)
    if handle not in codes:
        print(f"no recovery code for {handle}", file=sys.stderr)
        return 2
    if is_greeter and not os.environ.get("WAVE_DURATION_S"):
        # Gate beat is short by design (matches the CF lane's window).
        dur = random.Random(H(runner_id, slot, "gdur")).uniform(210, 270)
    else:
        dur = float(os.environ.get("WAVE_DURATION_S") or random.Random(H(runner_id, slot, "dur")).uniform(600, 780))
    jit = 0.0 if os.environ.get("WAVE_NO_JITTER") else \
        random.Random(H(runner_id, slot, "jit")).uniform(0, 60)
    s = Session(platform, runner, wave, handle, district, codes[handle],
                lines, dur, jit, rot_slot=slot)
    rec = s.run()
    line = json.dumps(rec)
    print("WAVELOG " + line, flush=True)
    logpath = os.environ.get("WAVE_LOG")
    if logpath:
        with open(logpath, "a") as f:
            f.write(line + "\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
