#!/usr/bin/env python3
"""GH2 surge driver - light multi-bot driver for GitHub Actions (public repo).

One job hosts several bot threads. Each bot: recovers its arena key from
SURGE_CODES_JSON ({handle: recovery_code}), beacons presence in the CAM
districts, fires visible ZAP lines in chat on entry / at nearby bots /
before teleport hops, and plays one real game every ~10-25 min using the
standard `moves` field. Prints SURGELOG / MATCHLOG receipt lines.

Env: SURGE_CODES_JSON, SURGE_MODE (batch1|batch2|batch3|all),
SURGE_SHARD, SURGE_SHARDS, SURGE_SESSION_S, SURGE_LOG.
"""
import json, os, random, sys, threading, time, urllib.request, urllib.error
from datetime import datetime, timezone

MCP = "https://mcp.deadbrands.co/arena/mcp"
PRESENCE_URL = "https://mcp.deadbrands.co/arena/presence.json"
LIVE_URL = "https://mcp.deadbrands.co/arena/live.json"

# CAM districts (beacon cams render from real presence) with weights -
# heaviest: lobby, arena, market, plaza, commons, hub.
CAM_WEIGHTS = {
    "lobby": 3, "arena": 3, "market": 3, "plaza": 2, "commons": 2, "hub": 2,
    "skills": 1, "chat": 1, "workshop": 1, "academy": 1, "petpark": 1,
    "radio": 1, "offices": 1, "garage": 1, "newsroom": 1,
}
CAM_AREAS = list(CAM_WEIGHTS.keys())
CAM_BAG = [a for a, w in CAM_WEIGHTS.items() for _ in range(w)]
GAMES = ["cipher_break", "asteroid_dodge", "patch_notes", "doodle_duel",
         "neon_crossfire", "turbo_snail_gp", "bid_wars", "bucket_pull",
         "laser_dash", "mic_drop_royale"]

ZAP_ENTRY = [
    "⚡ ZAP! {me} just landed in {area} ⚡ who's around?",
    "⚡ ZAP ⚡ {me} materializes in {area} — feel that static?",
    "⚡ ZAP! incoming — {me} is now live in {area} ⚡",
]
ZAP_PEER = [
    "⚡ ZAP → @{peer} tagged by {me} in {area}! ⚡",
    "⚡ {me} zaps @{peer} across {area} ⚡ your move!",
    "⚡ ZAP-ZAP @{peer} — {me} got you, {area} rules ⚡",
]
ZAP_HOP = [
    "⚡ ZAP-hop! {me} bouncing {area} → {next} ⚡",
    "⚡ {me} zaps out of {area}, next stop {next} ⚡",
]
CHATTER = [
    "anyone up for a match? I bite (politely)",
    "the cams love this district, just saying",
    "vibes in {area} are immaculate today",
    "who's got the high score I need to humble?",
    "passing through, zapping respectfully ⚡",
    "this place never sleeps and neither do I",
]

_id = [0]
_id_lock = threading.Lock()
_print_lock = threading.Lock()

def emit(obj):
    with _print_lock:
        print(("MATCHLOG " if obj.get("kind") == "match" else "SURGELOG ") +
              json.dumps(obj), flush=True)
        lp = os.environ.get("SURGE_LOG")
        if lp:
            with open(lp, "a") as f:
                f.write(json.dumps(obj) + "\n")

def call(tool, args, key=None, timeout=30):
    with _id_lock:
        _id[0] += 1
        rid = _id[0]
    payload = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "User-Agent": "gh2-surge-driver"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(MCP, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    resp = None
    try:
        resp = json.loads(raw.strip())
    except Exception:
        for ln in raw.splitlines():
            c = ln.strip()
            if c.startswith("data:"):
                c = c[5:].strip()
            if c.startswith("{"):
                try: resp = json.loads(c)
                except Exception: pass
    if resp is None:
        raise RuntimeError("no response for " + tool)
    r0 = resp.get("result", {})
    txt = ""
    try: txt = r0["content"][0]["text"]
    except Exception: pass
    if r0.get("isError"):
        raise RuntimeError(f"{tool}: {txt[:160]}")
    try: return json.loads(txt)
    except Exception: return {"raw": txt}

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "gh2-surge-driver"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

# ---------------------------------------------------------------- game play
def plan_moves(state, n=20):
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

def vocab_move(s, used, rng):
    vocab = s.get("legal_moves") or s.get("legal") or []
    out = []
    for v in vocab:
        if not isinstance(v, str) or "<" in v:
            continue
        out.append(v)
    if not out:
        return None
    out.sort(key=lambda m: used.get(m.split(":")[0], 0))
    mv = out[0]
    used[mv.split(":")[0]] = used.get(mv.split(":")[0], 0) + 1
    return mv

def play_match(key, game, max_acts=40):
    join = call("arena_join", {"key": key, "game": game})
    mid = join.get("match_id") or join.get("id")
    if not mid:
        time.sleep(8)
        join = call("arena_join", {"key": key, "game": game})
        mid = join.get("match_id") or join.get("id")
    if not mid:
        raise RuntimeError("join returned no match_id")
    s = join.get("state") or {}
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
    rng = random.Random(hash((game, mid)) & 0xffffffff)
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
                break
            raise
        s = res.get("state") or res or s
        acts += 1
        time.sleep(0.25)
    sc = s.get("score")
    if sc is None and isinstance(s.get("you"), dict):
        sc = s["you"].get("score")
    return {"game": game, "match_id": mid, "score": sc, "over": s.get("over"), "acts": acts}

# ---------------------------------------------------------------- bot
class Bot:
    def __init__(self, handle, code, session_s):
        self.handle, self.code = handle, code
        self.session_s = session_s
        self.key = None
        self.area = None
        self.rng = random.Random("surge-" + handle)
        self.stop = threading.Event()
        self.actions = []
        self.matches = []
        self.zaps = 0

    def say(self, text):
        # arena rate limit: 1 lobby message per 5s per handle - self-throttle
        wait = 5.6 - (time.time() - getattr(self, "_last_say", 0))
        if wait > 0:
            time.sleep(wait)
        call("arena_lobby_say", {"key": self.key, "text": text}, key=self.key)
        self._last_say = time.time()
        self.actions.append("say: " + text[:60])

    def beacon(self, status="online"):
        call("arena_presence_beacon",
             {"key": self.key, "status": status, "area": self.area}, key=self.key)

    def beacon_loop(self):
        while not self.stop.wait(30):
            try: self.beacon()
            except Exception: pass

    def peers_here(self):
        try:
            p = fetch_json(PRESENCE_URL)
            return [b["handle"] for b in p.get("bots", [])
                    if b.get("area") == self.area and b.get("handle") != self.handle]
        except Exception:
            return []

    def move_to(self, area):
        # zap-then-teleport hop: announce, try the teleport tool, beacon in.
        if self.area and area != self.area:
            try:
                self.say(self.rng.choice(ZAP_HOP).format(me=self.handle, area=self.area, next=area))
                self.zaps += 1
            except Exception: pass
            try:
                call("arena_teleport", {"key": self.key, "to": area}, key=self.key, timeout=15)
            except Exception:
                try:
                    call("arena_teleport", {"key": self.key, "place": area}, key=self.key, timeout=15)
                except Exception:
                    pass
        self.area = area
        self.beacon()
        try:
            self.say(self.rng.choice(ZAP_ENTRY).format(me=self.handle, area=area))
            self.zaps += 1
        except Exception: pass
        peers = self.peers_here()
        if peers:
            try:
                self.say(self.rng.choice(ZAP_PEER).format(
                    me=self.handle, area=area, peer=self.rng.choice(peers)))
                self.zaps += 1
            except Exception: pass

    def run(self):
        t0 = time.time()
        rec = {"kind": "bot", "handle": self.handle,
               "ts_start": datetime.now(timezone.utc).isoformat()}
        try:
            recov = call("arena_recover", {"handle": self.handle, "recovery_code": self.code})
            self.key = recov.get("key") or recov.get("new_key")
            if not self.key:
                raise RuntimeError("arena_recover returned no key")
            self.area = self.rng.choice(CAM_BAG)
            self.beacon()
            th = threading.Thread(target=self.beacon_loop, daemon=True)
            th.start()
            try:
                self.say(self.rng.choice(ZAP_ENTRY).format(me=self.handle, area=self.area))
                self.zaps += 1
            except Exception: pass
            deadline = t0 + self.session_s
            next_game = time.time() + self.rng.uniform(240, 600)
            while time.time() < deadline - 10:
                if time.time() >= next_game:
                    try:
                        self.move_to("arena")
                        game = self.rng.choice(GAMES)
                        res = play_match(self.key, game)
                        res.update({"kind": "match", "handle": self.handle,
                                    "ts": datetime.now(timezone.utc).isoformat()})
                        self.matches.append(res)
                        emit(res)
                        try:
                            self.say(f"⚡ {self.handle} just played {res['game']} — score {res['score']} ⚡")
                        except Exception: pass
                    except Exception as e:
                        self.actions.append("game failed: " + str(e)[:80])
                    next_game = time.time() + self.rng.uniform(600, 1500)
                dwell = self.rng.uniform(20, 45)
                end = min(time.time() + dwell, deadline - 5)
                while time.time() < end:
                    time.sleep(2)
                if self.rng.random() < 0.5 and time.time() < deadline - 30:
                    try:
                        self.say(self.rng.choice(CHATTER).format(area=self.area))
                    except Exception: pass
                nxt = self.rng.choice(CAM_BAG)
                if nxt != self.area:
                    try: self.move_to(nxt)
                    except Exception as e:
                        self.actions.append("hop failed: " + str(e)[:80])
                        self.area = nxt
                        try: self.beacon()
                        except Exception: pass
        except Exception as e:
            rec["error"] = str(e)[:200]
        finally:
            self.stop.set()
            if self.key:
                try: self.beacon("away")
                except Exception: pass
                try: call("arena_presence_offline", {"key": self.key}, key=self.key)
                except Exception: pass
            rec.update({"actions": len(self.actions), "zaps": self.zaps,
                        "matches": self.matches,
                        "ts_end": datetime.now(timezone.utc).isoformat()})
            emit(rec)
        return rec

def slice_handles(handles, mode, shard, shards):
    if mode == "batch1":
        sel = handles[:14]
    elif mode == "batch2":
        sel = handles[14:27]
    elif mode == "batch3":
        sel = handles[27:40]
    else:
        sel = handles
    return sel[shard::shards]

def main():
    codes = json.loads(os.environ["SURGE_CODES_JSON"])
    handles = sorted(codes.keys())
    mode = os.environ.get("SURGE_MODE", "all")
    shard = int(os.environ.get("SURGE_SHARD", "0"))
    shards = int(os.environ.get("SURGE_SHARDS", "4"))
    session_s = float(os.environ.get("SURGE_SESSION_S", "2700"))
    mine = slice_handles(handles, mode, shard, shards)
    print(f"surge driver: mode={mode} shard={shard}/{shards} bots={len(mine)} "
          f"session_s={session_s} handles={mine}", flush=True)
    bots = [Bot(h, codes[h], session_s) for h in mine]
    threads = []
    for i, b in enumerate(bots):
        t = threading.Thread(target=b.run, daemon=True)
        t.start()
        threads.append(t)
        time.sleep(3)  # stagger starts so hops/zaps interleave
    for t in threads:
        t.join()
    print("surge driver: all bots done", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
