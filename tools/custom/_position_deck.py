"""Build a rich multi-slide 'position deck' for one holding + a concise narration.

Used by get_position_brief. Gathers (READ-ONLY) the dossier, the desk valuation,
and recent news, then lays the story out as `create_presentation` slides in a
fixed narrative arc:

    snapshot → price trend → valuation → thesis → trades → news

and returns ``(presentation_payload, narration_script)``. The narration is what
Mocha SPEAKS — the frontend advances slides proportionally as she talks, so the
script carries ~one short beat per slide (the deck carries the dense numbers, so
TTS stays light). Every section is defensive: a sub-fetch that fails just drops
its slide + beat, never the whole deck. Module name starts with `_` so the
custom-tool loader skips it.
"""

from __future__ import annotations

import asyncio
import json
import re

from tools.custom._opus_raw import call_opus_raw, summarize_valuation, round_decimals


# ── small parse helpers ──────────────────────────────────────────────────────
def _num(x):
    try:
        return float(str(x).replace(",", "").replace("$", "").replace("%", "").strip())
    except Exception:
        return None


def _money(v, dp=2):
    if v is None:
        return None
    v = round(float(v), dp)
    s = f"{v:,.{dp}f}".rstrip("0").rstrip(".")
    return f"${s}"


def _signed(v, dp=2):
    if v is None:
        return None
    return ("+" if v >= 0 else "") + _money(v, dp)


def _company_name(valuation_text: str, symbol: str) -> str:
    m = re.search(r"#\s*" + re.escape(symbol) + r"\s*[—–-]\s*(.+)", valuation_text or "")
    if m:
        return re.sub(r"\s*\(.*\)\s*$", "", m.group(1).strip()).strip()
    return symbol


def _current_price(valuation_text: str):
    for pat in (r"Price:\s*\$([\d,]+\.?\d*)",
                r"Market close:\s*\$([\d,]+\.?\d*)",
                r"live price\)\s*[\s\S]{0,40}?\$([\d,]+\.?\d*)",
                r"to\s*\$([\d,]+\.?\d*)\s*live price",
                r"current(?:ly)?\s*(?:price\s*)?(?:is\s*)?\$([\d,]+\.?\d*)"):
        m = re.search(pat, valuation_text or "", re.IGNORECASE)
        if m:
            return _num(m.group(1))
    return None


def _parse_scenarios(valuation_text: str):
    """Pull (label, iv_per_share) for Bear/Base/Bull/Prob-weighted out of the
    'Scenario Summary' markdown table. Returns [] if the report has no such table."""
    out = []
    for raw_label, short in (("Bear", "Bear"), ("Base", "Base"), ("Bull", "Bull"),
                             ("Probability-weighted", "Prob-wtd"), ("Prob-weighted", "Prob-wtd")):
        # | <label>(...) | <weight> | $<iv> | ... |  — label/iv may be **bold**
        m = re.search(r"\|\s*\*{0,2}" + raw_label + r"\b[^|]*\|[^|]*\|\s*\*{0,2}\$?([\d,]+\.?\d*)",
                      valuation_text or "", re.IGNORECASE)
        if m:
            v = _num(m.group(1))
            if v is not None and not any(s == short for s, _ in out):
                out.append((short, v))
    return out


# ── data gathering ───────────────────────────────────────────────────────────
async def _live_quote(symbol: str):
    """(last_close, prev_close) from yfinance — same source as the candlestick
    slide, so the snapshot price agrees with the chart. (None, None) on failure.
    The valuation report's reference price can be days stale, so we never use it
    as the live price."""
    def _fetch():
        import yfinance as yf
        h = yf.Ticker(symbol).history(period="5d")
        if h is None or h.empty:
            return (None, None)
        closes = [float(c) for c in h["Close"].tolist() if c == c]  # drop NaN
        last = closes[-1] if closes else None
        prev = closes[-2] if len(closes) >= 2 else None
        return (last, prev)
    try:
        return await asyncio.get_event_loop().run_in_executor(None, _fetch)
    except Exception:
        return (None, None)


async def _get_news(query: str, n: int = 4):
    """Return [{title, snippet, source, date, url, thumbnail}] via get_news (Brave)."""
    try:
        from tools.custom import news_fetch
        raw = await news_fetch.execute({"topic": query, "max_results": n})
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict):
            data = data.get("articles") or data.get("results") or []
        arts = []
        for a in (data or [])[:n]:
            if not isinstance(a, dict):
                continue
            arts.append({
                "title": a.get("title", ""),
                "snippet": (a.get("snippet") or a.get("description") or "")[:200],
                "source": a.get("source") or a.get("publisher") or "",
                "date": a.get("date") or a.get("published") or "",
                "url": a.get("url", ""),
                "thumbnail": a.get("thumbnail") or a.get("image") or "",
            })
        return [a for a in arts if a["title"]]
    except Exception:
        return []


# ── deck assembly ────────────────────────────────────────────────────────────
async def build_position_deck(symbol: str):
    """Return ``(presentation_payload | None, narration_script)`` for one holding."""
    symbol = symbol.strip().upper()

    (dok, draw), (vok, vraw), news, (live_last, live_prev) = await asyncio.gather(
        call_opus_raw("get_position_dossier", {"symbol": symbol}),
        call_opus_raw("get_ticker_valuation", {"symbol": symbol}),
        _get_news(f"{symbol} stock", 4),
        _live_quote(symbol),
    )

    dossier = {}
    if dok:
        try:
            dossier = json.loads(draw)
        except Exception:
            dossier = {}
    valuation_text = vraw if vok else ""
    has_val = vok and "no damodaran-style valuation on file" not in valuation_text.lower()

    company = _company_name(valuation_text, symbol) if has_val else symbol
    # Live price from yfinance; the report's reference price is only a fallback
    # (it can be days stale, which would misstate cheap/rich vs fair value).
    report_price = _current_price(valuation_text) if has_val else None
    price = live_last if live_last is not None else report_price
    day_pct = None
    if live_last is not None and live_prev:
        day_pct = round((live_last - live_prev) / live_prev * 100, 2)
    asof = ""
    m_asof = re.search(r"generated_at:\s*(\d{4}-\d{2}-\d{2})", valuation_text)
    if m_asof:
        asof = m_asof.group(1)
    scenarios = _parse_scenarios(valuation_text) if has_val else []
    base_iv = next((v for s, v in scenarios if s == "Base"), None)
    pw_iv = next((v for s, v in scenarios if s == "Prob-wtd"), None)
    # The desk's headline "fair value" is the probability-weighted estimate, not
    # the base case alone (base can read as cheap/rich while the blend is fair).
    fair = pw_iv if pw_iv is not None else base_iv

    convs = dossier.get("active_convictions") or []
    conv = convs[0] if convs else {}
    direction = (conv.get("direction") or "").lower()
    conviction = _num(conv.get("conviction"))
    exp_ret = _num(conv.get("expected_return_pct"))
    ttt = conv.get("time_to_target_days")
    rationale = (conv.get("rationale") or "").strip()
    agent = conv.get("agent_name") or ""
    unreal = _num(dossier.get("unrealized_pnl"))
    total_pnl = _num(dossier.get("total_pnl"))
    net_qty = None
    try:
        net_qty = sum(_num(p.get("net_qty_change")) or 0 for p in (dossier.get("pnl_by_agent") or []))
    except Exception:
        net_qty = None
    fills = dossier.get("recent_fills_30d") or []

    slides: list[dict] = []
    beats: list[str] = []
    stance = {"long": "LONG", "short": "SHORT"}.get(direction, direction.upper() or "—")

    # 1 ── Snapshot (stat_row)
    stats = []
    if price is not None:
        ps = {"label": "Price", "value": _money(price)}
        if day_pct is not None:
            ps["delta"] = f"{day_pct:+}% today"
        stats.append(ps)
    if direction:
        cv = f"{stance}"
        if conviction is not None:
            cv += f" · conv {round(conviction, 2)}"
        stats.append({"label": "Our stance", "value": cv})
    if fair is not None:
        delta = None
        if price:
            delta = f"{round((fair - price) / price * 100, 1):+}% vs price"
        lbl = "Desk fair value" + (f" · as of {asof}" if asof else "")
        stats.append({"label": lbl, "value": _money(fair), **({"delta": delta} if delta else {})})
    if unreal is not None:
        stats.append({"label": "Unrealized P&L", "value": _signed(unreal), "delta": _signed(unreal)})
    if net_qty:
        stats.append({"label": "Net shares", "value": f"{round(net_qty, 2):g}"})
    if stats:
        slides.append({"type": "stat_row", "title": f"{company} ({symbol})", "stats": stats,
                       "narration": ""})
        # Narration is deliberately NUMBER-FREE — the slides carry every figure,
        # and handing the model raw prices makes it fabricate num: handles around
        # them (they leak as "num:"). Speak qualitatively; show quantitatively.
        clauses = []
        if direction:
            clauses.append(f"we're {stance.lower()}")
        if unreal is not None:
            clauses.append("in the green" if unreal >= 0 else "underwater")
        snap = f"Here's where we stand on {company}"
        if clauses:
            snap += " — " + " and ".join(clauses)
        beats.append(snap + ".")

    # 2 ── Price trend (candlestick — frontend auto-fetches /api/stock-chart)
    slides.append({"type": "candlestick", "title": f"{symbol} · 3-month trend",
                   "symbol": symbol, "default_period": "3mo", "narration": ""})
    beats.append("Here's the three-month price trend.")

    # 3 ── Valuation (scenario bar chart, else markdown verdict)
    if scenarios:
        labels = [s for s, _ in scenarios]
        data = [round(v, 2) for _, v in scenarios]
        if price is not None:
            labels.append("Current")
            data.append(round(price, 2))
        vtitle = "Desk valuation — intrinsic value by scenario"
        if asof:
            vtitle = f"Desk valuation (as of {asof}) — intrinsic value by scenario"
        slides.append({"type": "chart", "chart_type": "bar",
                       "title": vtitle,
                       "labels": labels,
                       "datasets": [{"label": "$ / share", "data": data}],
                       "narration": ""})
        beat = "On valuation"
        if fair and price:
            gap = (price - fair) / fair * 100
            if gap >= 7:
                beat += ", it's run rich of the desk's fair value"
            elif gap <= -7:
                beat += ", it's trading below the desk's fair value — there's room"
            else:
                beat += ", it's right about the desk's fair value"
        else:
            beat += ", here's the desk's scenario range"
        beats.append(beat + ".")
    elif has_val:
        slides.append({"type": "markdown", "title": "Desk valuation",
                       "content": round_decimals(summarize_valuation(symbol, valuation_text)),
                       "narration": ""})
        beats.append("Here's the desk's valuation verdict.")

    # 4 ── Thesis / why we hold (markdown: conviction + OCAP rationale + verdict)
    if conv or has_val:
        lines = []
        if agent:
            head = f"**{agent}** — {stance.lower()} conviction"
            if conviction is not None:
                head += f" {round(conviction, 2)}"
            if exp_ret is not None:
                head += f", targeting {round(exp_ret, 2)}%"
                if ttt:
                    head += f" in {ttt}d"
            lines.append(head)
        if rationale:
            lines.append("")
            lines.append(rationale)
        if has_val:
            verdict = summarize_valuation(symbol, valuation_text)
            # keep only the Verdict/Stance/Risk body, drop the "Desk valuation for…" header line
            verdict = re.sub(r"^Desk valuation for [^\n]*\n+", "", verdict).strip()
            if verdict:
                lines.append("")
                lines.append("---")
                lines.append(verdict)
        if lines:
            slides.append({"type": "markdown", "title": "Why we hold it",
                           "content": round_decimals("\n".join(lines)), "narration": ""})
            if agent:
                tb = f"We hold it on {agent}'s {stance.lower()} conviction, a near-term call"
            else:
                tb = "Here's the desk's read on why we're in it"
            beats.append(tb + ".")

    # 5 ── Trade record (table of recent fills)
    if fills:
        rows = []
        for f in fills[:8]:
            when = (str(f.get("filled_at") or "")[:10])
            act = "BUY" if str(f.get("action", "")).upper() in ("BOT", "BUY") else "SELL"
            qty = _num(f.get("quantity"))
            px = _num(f.get("fill_price"))
            pnl = _num(f.get("realized_pnl"))
            rows.append([when, f.get("agent_name", ""), act,
                         f"{round(qty, 2):g}" if qty is not None else "",
                         _money(px) or "", _signed(pnl) if pnl else "—"])
        slides.append({"type": "table", "title": "Recent fills (30d)",
                       "headers": ["Date", "Agent", "Side", "Qty", "Price", "Realized"],
                       "rows": rows, "narration": ""})
        last = fills[0]
        ag = last.get("agent_name", "the desk")
        beats.append(f"We got in through {ag}'s recent fills.")

    # 6 ── News (news_feed with thumbnails)
    if news:
        slides.append({"type": "news_feed", "title": f"Latest on {symbol}",
                       "articles": news, "narration": ""})
        beats.append("And here are the latest headlines.")

    if not slides:
        return None, ""

    payload = {
        "id": f"pres_position_{symbol.lower()}",
        "title": f"{company} ({symbol}) — position briefing",
        "slides": slides,
    }
    # mirror each beat onto its slide's narration (forward-compat; harmless today)
    for sl, bt in zip(slides, beats):
        sl["narration"] = bt
    narration = " ".join(beats)
    return payload, narration
