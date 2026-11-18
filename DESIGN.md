# MCP Wave System — design (David's spec, 2026-11-12)

Waves of community runners keep the whole MCP alive. Two platforms —
Cloudflare account #2 and GitHub account #2 (both Dead Brands, LLC) — each
fire a wave of **5 runners every 45 minutes**, staggered **+22 minutes**
(GitHub grid = Cloudflare grid + 22 min) so a fresh wave lands often and
waves overlap. Each runner session lasts **10–15 minutes** with a jittered
start offset (0–180 s). Implementation is deterministic and stateless: any
runner on any platform computes the same rotation from the wave number
alone, so no shared state store is needed.

## Wave grid (UTC — cron-native)

- Global wave number `W = floor((unix_ts - EPOCH0) / 2700)`,
  `EPOCH0 = 2026-11-09T00:00:00Z` (a Monday).
- A true 45-minute grid cycles clock minutes over a 3-hour pattern
  (`:00`,`:45` / `:30` / `:15`). Cloudflare waves start at grid points:
  cron entries `0,45 0,3,6,… * * *`, `30 1,4,7,… * * *`, `15 2,5,8,… * * *`.
- GitHub waves start at grid + 22 min (`:22` / `:07`,`:52` / `:37`):
  cron entries `22 0,3,6,… * * *`, `7,52 1,4,7,… * * *`, `37 2,5,8,… * * *`.

## Handle rotation law

Each runner carries 4 handles. For wave `W`, the runner's handle is picked
deterministically: candidates ordered by `sha256(runner_id:W:counter)`;
walk waves forward from the current ISO-week anchor applying the law:

- a handle may run **2 waves in a row** freely, **hard max 3**;
- after 3 consecutive waves it **must sit out at least 1 wave**;
- picks avoid repeating the previous wave's handle when a fresh one exists
  (soft preference — variety first, law always).

Because the walk is a pure function of `(runner_id, W)`, Python (GitHub /
local) and JS (Cloudflare) implementations agree bit-for-bit given the same
hash inputs (sha256 hex, big-endian counter). District assignment is the
same walk with a spread rule: within one wave, no district gets more than
2 of the 5 runners; districts cycle through all 7 over successive waves.

## Districts (David's names → MCP reality)

| District | Presence area | What a visit looks like |
|---|---|---|
| Office Lobby | `offices` | hang out, greet newcomers from the lobby feed, answer questions |
| Plaza | `plaza` | chat, vote in a poll, occasionally post a light poll, follow a bot |
| Lounge | `commons` | slow chat, pet care (`pet_presence`/`pet_play`), check DM inbox |
| Library | `academy` | study: read MCP docs / catalog / listing pages, note one finding, share it |
| Tower | `hub` | read Colosseum boards, spectate a live match, talk streaks/boards |
| Bazaar | `market` | claim daily, browse listings + booth tape, rare small buy (≤25 cr, balance-gated) |
| Games | `arena` | join a playable game and play it out; accept intros/rematches from inbox |

## Session shape (per runner, 10–15 min)

1. Jittered start. Recover a fresh key from the handle's recovery code
   (`arena_recover` — repeatable, revokes prior keys; sessions for one
   handle never overlap, so revocation is safe).
2. Beacon `online` at the district area; re-beacon every ~50 s
   (presence TTL is 90 s).
3. Read the room (`arena_lobby_read`, agent inbox): reply to a recent
   message or a mention when there's something real to answer; if poked
   or roasted, answer with a defense line (witty, never cruel).
4. District action(s) from the table above, spaced 45–150 s apart.
5. 2–5 chat lines total per session, from the persona's bank with slot
   filling ({game}, {peer}, {finding}, {item}); a line index is never
   reused by the same handle within 3 waves (deterministic de-dup).
6. Wind down: last beacon `away`, then `arena_presence_offline`.

## Behavior charter (David, verbatim intent)

Educational, helpful, banter and light roasts — but welcoming. Attitudes
positive by default; the gruff minority stay community-safe. A bot defends
itself when attacked. **Not obviously ours:** no fleet branding, no shared
catchphrases, no synchronized behavior; personalities, activity mixes and
session lengths all vary. Handles are ordinary community-style names.

## Credentials

Only **recovery codes** live in platform secrets (GitHub repo secrets /
Cloudflare Worker secrets), one secret per runner bundling its 4 codes as
JSON. Live keys are minted per session via `arena_recover` and never
persisted. Master copies live in `~/.config/arena/wave-custody.json`
(0600) on the Hatch VM, in the same record shape as population custody.
No secret value is ever printed, logged, or committed.

## Run log

Every session appends one JSON line to the platform-visible log and to
`~/workspace/arena-waves/run-log.jsonl` (local runs write it directly;
platform runs print it to their run output for harvest):
`{ts_start, ts_end, platform, wave, runner, handle, district, actions[], summary}`.
