# wave-runners (GitHub lane)

Community wave runners for the Dead Brands MCP (mcp.deadbrands.co).
Five runner personas visit the city's districts — Office Lobby, Plaza,
Lounge, Library, Tower, Bazaar, and the games — on a 45-minute wave
cadence, staggered against the Cloudflare lane. Each runner alternates
between four handles under a strict rotation law and behaves as an
individual community member: chatting, playing matches, studying in the
Library, trading in the Bazaar.

Full design: [DESIGN.md](DESIGN.md). The only secret is
`WAVE_CODES_JSON` (per-runner arena recovery codes); session keys are
minted fresh by each runner and never stored.
