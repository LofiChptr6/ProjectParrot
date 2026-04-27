# Personal-Trading Question Surface

## The Gap

`get_trading_briefing` covers "what happened today" — P&L, top positions, conviction at a glance. But the moment a user goes one level deeper, there is no protocol:

> "Who long'ed LMT today? What's his rationale?"  
> "Is the thesis still good?"  
> "Why did agent X close TSLA?"

Mocha currently has no tool that answers these, so she promises a lookup and silently does nothing — the user is left hanging.

This document enumerates the questions a user will *actually ask* about their strategies, groups them by intent, and proposes tools the opus_trading team should expose. Each tool follows the `__panel__` envelope from `docs/tool-protocol.md`.

## Design principles

1. **One tool per question class, not one tool per question.** A user asking "why did agent X buy LMT?" and "why did the conviction change on LMT?" both want a position dossier — same tool, different focus.
2. **Each tool returns a panel-ready envelope.** No string blobs that Mocha has to parse. The opus team owns the analysis; ProjectParrot owns the rendering.
3. **Panels carry insight, not raw fields.** Every tool should include a 1–3 sentence `analyst_note` field that Nori can use as the basis of her narration. This way the opus team — who knows the strategy best — gets to influence the framing, not just hand over rows.
4. **Be okay returning "no data."** A graceful `markdown` slide that says "this position has no recorded thesis — likely a manual override on Mar 12" is far better than fabricating one.

## Question categories

Each section lists the user's natural phrasing, then the proposed tool. All tools take 0–2 small parameters and return the standard envelope.

---

### 1. Position-level dossier — "Tell me about LMT"

The single most-asked deep question. Should answer all of: who owns it, why, what's the plan, is it still on track.

**User says:**
- "Who long'ed LMT today?" / "Who put this on?"
- "What's his rationale?" / "Why are we long LMT?"
- "What's the thesis?" / "What's the plan on LMT?"
- "Is the LMT thesis still good?"
- "What would make us exit?"
- "Why did we close TSLA yesterday?"

**Proposed tool:** `get_position_dossier(symbol, status="open")`

**Envelope:** 4–6 slide presentation
- Slide 1 — `stat_row`: symbol, side, size, entry price, current price, P&L, holder agent(s), entry date
- Slide 2 — `markdown`: the agent's original thesis (verbatim quote if available, paraphrased if reconstructed)
- Slide 3 — `bullets`: the plan — entry trigger, stop level, target(s), time horizon, what invalidates
- Slide 4 — `markdown`: thesis health — what's still on-track, what's broken, what's neutral. Include the agent's most recent comment if any.
- Slide 5 (optional) — `chart` or `candlestick`: price action since entry, with annotations on key events (add, trim, news that mattered)
- Slide 6 (optional) — `news_feed`: news on the symbol since entry, tagged by relevance to thesis

`analyst_note`: 1–3 sentences leading the headline — "Aerospace agent went long LMT 4 days ago on the F-35 Block IV order anticipation. Thesis is still on track but the recent dip has us 80 bps offside."

---

### 2. Agent-level overview — "How is the macro agent doing?"

**User says:**
- "How is the macro agent doing?"
- "What's agent X running right now?"
- "Show me the semis specialist's book."
- "Who's my best/worst agent?"
- "Has agent X been right lately?"

**Proposed tool:** `get_agent_overview(agent_id, window="7d")`

**Envelope:** 3–4 slide presentation
- Slide 1 — `stat_row`: agent name + class, P&L (today, 7d, 30d, YTD), hit rate, sharpe, current allocation
- Slide 2 — `multi_chart` or `table`: all open positions for this agent
- Slide 3 — `markdown`: current playbook / what the agent is "leaning into" right now — sector view, macro thesis, factor tilt
- Slide 4 (optional) — `bullets`: recent wins and losses (last 5 closed trades)

`analyst_note`: "Macro agent is +2.1% on the week, mostly from the long-bond convexity trade. They're still defensive on equities — net short SPY."

---

### 3. P&L attribution — "Why did I make/lose money today?"

**User says:**
- "Why did I make money today?"
- "What drove the loss?"
- "Which agent crushed it?"
- "Where did the alpha come from this week?"

**Proposed tool:** `get_pnl_attribution(window="1d")`

**Envelope:** 2–3 slide presentation
- Slide 1 — `chart` (bar): P&L by agent (top contributors and detractors)
- Slide 2 — `table`: top 5 contributing positions and top 5 detracting positions, with thesis status one-liners
- Slide 3 (optional) — `markdown`: narrative — "today was a sector rotation: defensives got bid, semis sold off, our long-LMT/short-NVDA pair worked"

`analyst_note`: "Aerospace agent did 70% of today's gain on LMT and RTX. Semis agent gave half of it back on NVDA."

---

### 4. Trade activity — "What did I trade today?"

**User says:**
- "What did we trade today?"
- "Show me today's tickets."
- "Did anything close?"
- "Any new positions?"

**Proposed tool:** `get_trade_activity(window="1d")`

**Envelope:** single slide if quiet day, multi-slide if busy
- Slide 1 — `table`: time, symbol, side, qty, price, agent, action (open/add/trim/close), brief reason
- Slide 2 (optional) — `bullets`: highlights — biggest add, biggest cut, most controversial close

`analyst_note`: "Six tickets today, all from the macro and aerospace agents. The big one was the LMT add at 11:42 — that's now your largest position."

---

### 5. Risk & exposure — "Where am I exposed?"

**User says:**
- "What's my biggest risk right now?"
- "How concentrated am I in semis?"
- "What's my net beta?"
- "What if the market drops 5% tomorrow?"
- "What's my factor exposure?"

**Proposed tool:** `get_risk_overview(focus=null)`

`focus` can be `null` (general), `"sector"`, `"factor"`, `"single_name"`, or `"scenario"`.

**Envelope:** 3–4 slide presentation
- Slide 1 — `stat_row`: gross, net, leverage, beta, VaR, max drawdown contributors
- Slide 2 — `chart` (bar): exposure by sector OR by factor OR by agent (depending on focus)
- Slide 3 — `table`: scenario P&L for {-5%, -2%, +2%, +5%} market moves
- Slide 4 (optional) — `markdown`: what would actually hurt — the concentration, the correlation, the basis risk

`analyst_note`: "Your top 3 names are 47% of the book and all defense — that's a single-factor bet, not three positions."

---

### 6. Strategy / agent disagreement — "What are we conflicted about?"

**User says:**
- "Are any of my agents fighting each other?"
- "What's controversial in the book right now?"
- "Where do strategies disagree?"

**Proposed tool:** `get_agent_disagreement()`

**Envelope:** single slide
- Slide 1 — `bullets` or `table`: pairs of (agent_A, agent_B, symbol, A's view, B's view, current net position)

`analyst_note`: "Macro is short equities, but the semis agent just put on a big NVDA long. You're net long NVDA but the macro overlay says you shouldn't be — call it."

---

### 7. Position history — "Has this trade worked before?"

**User says:**
- "Has the macro agent traded TLT before? How did it go?"
- "What's our track record on LMT?"
- "How often does this setup work?"

**Proposed tool:** `get_position_history(symbol=null, agent_id=null, setup_type=null)`

At least one of the three params must be set.

**Envelope:** 2–3 slide presentation
- Slide 1 — `stat_row`: number of trades, win rate, avg holding, avg P&L, best/worst
- Slide 2 — `table`: chronological list — entry date, exit date, P&L, thesis one-liner
- Slide 3 (optional) — `chart`: P&L of each trade, sized by gross

`analyst_note`: "Macro agent has been long TLT 4 times in the past year. Hit rate 75%, but the one miss was the 2025-Q4 inflation surprise — and the macro setup today rhymes with that."

---

### 8. What-changed digest — "What's new since I last checked?"

**User says:**
- "Anything change overnight?"
- "What's new this morning?"
- "Catch me up — I've been away."

**Proposed tool:** `get_changes_since(timestamp_iso=null)`

If `timestamp_iso` is null, defaults to the user's last interaction.

**Envelope:** 2–3 slide presentation
- Slide 1 — `bullets`: top 3–5 things that changed (new positions, closed positions, large moves, broken theses, news that matters)
- Slide 2 — `table`: full position deltas (NAV, sizes, P&L since last check)
- Slide 3 (optional) — `news_feed`: news the agents have flagged as relevant

`analyst_note`: "Two things while you were out: aerospace added 30% to LMT into the dip, and the semis agent flipped to net short NVDA on a borrow-rate signal."

---

### 9. Forward-looking — "What are we watching?"

**User says:**
- "Anything coming up I should care about?"
- "What's on the docket today?"
- "What's the next catalyst?"

**Proposed tool:** `get_upcoming_catalysts(window="7d")`

**Envelope:** 1–2 slide presentation
- Slide 1 — `table`: date, event, affected positions, agent's prep / planned action
- Slide 2 (optional) — `markdown`: the agent's notes on the most important upcoming event

`analyst_note`: "Three things: NVDA earnings Thursday after close, FOMC next Wednesday, and the LMT lockheed-martin investor day Friday. The semis agent has already trimmed NVDA into earnings."

---

### 10. Override / manual — "Did I do anything dumb?"

Important for any system where the user can override agents.

**User says:**
- "Did I override anything recently?"
- "What did I touch manually?"
- "Where did my manual trades work or hurt?"

**Proposed tool:** `get_manual_overrides(window="30d")`

**Envelope:** 2 slide presentation
- Slide 1 — `table`: timestamp, symbol, agent's call, your override, P&L impact
- Slide 2 — `markdown`: honest assessment — was the override profitable on average

`analyst_note`: "You overrode the semis agent 3 times this month. Two were right, one was wrong — net +$420. The one that hurt was selling NVDA early on the 9th."

---

## Suggested build order

If the opus team can only ship a few of these soon, I'd prioritize as:

1. **`get_position_dossier`** — answers the most-asked deep question. Without this, every "why are we long X?" is a dead end.
2. **`get_pnl_attribution`** — natural follow-up to the daily briefing. "Why did I make money?" is asked daily.
3. **`get_trade_activity`** — cheap, useful, often asked.
4. **`get_agent_overview`** — once users start trusting the agents they want to know each one.
5. **`get_changes_since`** — closes the "what did I miss" loop.
6. **`get_risk_overview`** — for power users.
7. **Everything else** — as time permits.

---

## ProjectParrot side: what we still need

Each new tool above will Just Work via the existing `__panel__` envelope detection in `tools/executor.py`. Two small things to add on this side, both already trivial:

1. **Teach Nori about each new tool.** One line in `_NORI_TOOL_NAMES` (`nori/agent.py`) and one paragraph in the Data Tools section of `nori/soul.md` — same pattern as `get_trading_briefing`.

2. **Maybe** introduce a new slide type for richly-structured position dossiers if the existing `stat_row` + `bullets` + `markdown` combination feels too plain. Let's ship a few real envelopes first and decide.

## Questions for the opus team

1. Is there a stable `agent_id` namespace? (e.g. `aerospace`, `macro`, `semis`) Or are agents identified by class name + instance?
2. Do agents persist their thesis as text at entry time, or do we have to reconstruct from logs?
3. Is there a "thesis health" check the agents run, or is that a new capability we'd ask for?
4. What's the canonical timestamp for "since I last checked" — is this stored per-user on opus side, or do we pass it in from ProjectParrot?
5. Are sector/factor exposures already computed somewhere, or would `get_risk_overview` need to compute on-the-fly?

Answers to these shape which tools are 1-day builds vs 1-week builds.
