# Claude in the planner (#89)

Claude helps plan; it never decides. On the trip planner page, the **Claude**
card has a **Why this way?** button. Claude reads the current plan and checks the
proposed stops' notes and the weather where it matters. It explains the day in a
few sentences, and if a clearly better choice exists among your POIs, it
**suggests** a route. Suggestions appear as cards with **Accept & plan** (fills
the form and plans it in slow or quick mode) and **Dismiss**. Nothing changes
until you accept.

## Set up

1. Create an API key at console.anthropic.com. **Set a monthly spend limit there
   too.** That's the real backstop.
2. On the Pi, put it in `deploy/.env`: `ANTHROPIC_API_KEY=…`. Optionally change
   `CLAUDE_MONTHLY_CAP_USD` (default 25).
3. Restart the planner: `sudo docker compose -f deploy/docker-compose.yml up -d planner`.

Without a key, the card says "not set up" and everything else works.

## What Claude can see and do

| Tool | What it does |
|---|---|
| `where_is_the_van` | GPS position |
| `find_pois`, `poi_details` | your POI notes (fields and note text) |
| `last_plan` | the plan on the page: route, day budget, POIs and scores, proposed stops |
| `weather_at` | Open-Meteo forecast at a point and time |
| `propose_route` | records a suggestion; the only tool that "writes" |

It never sees your personal planning notes. The travel rules (300 mi a day as a
ceiling, 10:00 to 15:00, no driving after dark, 3 nights, no interstates,
70–80°F) come from bigair's settings, in the system prompt. Fuel and range always
come from the van's tested model, never from Claude.

## Cost and limits

- Model: Claude Opus 5.5 ($4 / $20 per million tokens in/out), effort medium.
  Prompt caching is on. A "Why this way?" usually costs a few cents.
- bigair estimates each call's cost from its token counts and keeps the
  month's total durably in `data/bigair/claude-spend.json`. It won't start a new
  question with less than $0.50 left under the cap. The card shows "$x of $25
  this month".
- At most 8 Claude calls per question, one question at a time, and
  5 × `PLANNER_DAILY_LIMIT` questions a day.

## Offline

No internet: the card says Claude is offline, and the planner carries on.

## Claude Code on the Pi

`CLAUDE_CODE_ENABLE=true` in `deploy/.env` makes the bootstrap install Claude Code
with Anthropic's native installer (`~/.local/bin/claude`). SSH in over Tailscale
and run `claude` in `~/BigAirHomeAssistant`. Logging in is yours to do, once.
