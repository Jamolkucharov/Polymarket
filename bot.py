import os, json, re, asyncio, logging, time
from datetime import datetime, timezone, time as dtime
import requests
from dotenv import load_dotenv
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("polybot")

# ---------- Sozlamalar ----------
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ["TELEGRAM_ADMIN_ID"])
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

LIVE = os.getenv("LIVE_TRADING", "false").lower() == "true"   # false = paper
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.10"))               # 10% farq
MIN_CONF = float(os.getenv("MIN_CONFIDENCE", "0.6"))
MAX_TRADE_USD = float(os.getenv("MAX_TRADE_USD", "5"))
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "20"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))
SCAN_MINUTES = int(os.getenv("SCAN_MINUTES", "30"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME_24H", "5000"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.50"))   # +50% da sotish
STOP_LOSS = float(os.getenv("STOP_LOSS", "0.40"))       # -40% da sotish
CHECK_MINUTES = int(os.getenv("CHECK_MINUTES", "5"))
REPORT_HOUR_UTC = int(os.getenv("REPORT_HOUR_UTC", "17"))

KEYWORDS = ["bitcoin", "btc", "ethereum", "eth ", "solana", "crypto", "gold", "xau"]
STATE_FILE = "state.json"
claude = Anthropic(api_key=ANTHROPIC_KEY)

# ---------- Holat ----------
state = {"paused": False, "positions": [], "closed": [], "spent_today": 0.0, "day": ""}

def load_state():
    global state
    if os.path.exists(STATE_FILE):
        state.update(json.load(open(STATE_FILE)))

def save_state():
    json.dump(state, open(STATE_FILE, "w"), indent=2)

def reset_day():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state["day"] != today:
        state["day"], state["spent_today"] = today, 0.0

# ---------- Ma'lumot ----------
def jl(v):
    return json.loads(v) if isinstance(v, str) else v

def get_markets():
    r = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"active": "true", "closed": "false", "limit": 200,
                "order": "volume24hr", "ascending": "false"}, timeout=20)
    r.raise_for_status()
    out = []
    for m in r.json():
        q = (m.get("question") or "").lower() + " "
        if not any(k in q for k in KEYWORDS):
            continue
        if float(m.get("volume24hr") or 0) < MIN_VOLUME:
            continue
        try:
            outs, prices, toks = jl(m["outcomes"]), jl(m["outcomePrices"]), jl(m["clobTokenIds"])
        except Exception:
            continue
        if [o.lower() for o in outs] != ["yes", "no"]:
            continue
        out.append({"id": m["id"], "question": m["question"], "end": m.get("endDate"),
                    "yes": float(prices[0]), "no": float(prices[1]),
                    "yes_token": toks[0], "no_token": toks[1],
                    "vol": float(m.get("volume24hr") or 0)})
    return out

def get_spot():
    """BTC/ETH/SOL va oltin (PAXG ~ 1 troy oz oltin) narxlari."""
    r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                     params={"ids": "bitcoin,ethereum,solana,pax-gold",
                             "vs_currencies": "usd", "include_24hr_change": "true"}, timeout=20)
    return r.json()

# ---------- Analiz (Claude) ----------
PROMPT = """Sen prediction market analitikisan. Bozor savoli, joriy narxlar va spot ma'lumot berilgan.
Savolning YES bo'lish ehtimolini baho ber. Ehtiyotkor bo'l: aniq asosing bo'lmasa confidence past bo'lsin.
Faqat JSON qaytar: {{"prob": 0..1, "confidence": 0..1, "reason": "1 jumla"}}

Bugun: {now}
Savol: {q}
Tugash sanasi: {end}
Bozor YES narxi: {yes:.3f}
Spot narxlar (USD, 24s o'zgarish): {spot}"""

def analyze(m, spot):
    msg = claude.messages.create(
        model=MODEL, max_tokens=300,
        messages=[{"role": "user", "content": PROMPT.format(
            now=datetime.now(timezone.utc).isoformat(timespec="minutes"),
            q=m["question"], end=m["end"], yes=m["yes"], spot=json.dumps(spot))}])
    text = msg.content[0].text
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))

# ---------- Savdo ----------
_clob = None
def clob():
    global _clob
    if _clob is None:
        from py_clob_client.client import ClobClient
        _clob = ClobClient("https://clob.polymarket.com", key=os.environ["PRIVATE_KEY"],
                           chain_id=137, signature_type=int(os.getenv("SIGNATURE_TYPE", "0")),
                           funder=os.getenv("FUNDER_ADDRESS") or None)
        _clob.set_api_creds(_clob.create_or_derive_api_creds())
    return _clob

def place_buy(token_id, price, usd):
    size = round(usd / price, 2)
    if not LIVE:
        return {"paper": True, "size": size}
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    order = clob().create_order(OrderArgs(token_id=token_id, price=round(price, 2), size=size, side=BUY))
    return clob().post_order(order, OrderType.GTC)

def place_sell(token_id, price, shares):
    if not LIVE:
        return {"paper": True}
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL
    p = max(0.01, round(price - 0.01, 2))   # tezroq bajarilishi uchun biroz past
    order = clob().create_order(OrderArgs(token_id=token_id, price=p, size=shares, side=SELL))
    return clob().post_order(order, OrderType.GTC)

def get_market_now(market_id):
    r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=20)
    r.raise_for_status()
    return r.json()

# ---------- Asosiy sikl ----------
async def scan(app: Application, manual=False):
    if state["paused"] and not manual:
        return
    reset_day()
    notify = lambda t: app.bot.send_message(ADMIN_ID, t)
    try:
        markets = await asyncio.to_thread(get_markets)
        spot = await asyncio.to_thread(get_spot)
    except Exception as e:
        await notify(f"⚠️ Ma'lumot xatosi: {e}")
        return
    held = {p["market_id"] for p in state["positions"]}
    checked = 0
    for m in markets:
        if m["id"] in held or len(state["positions"]) >= MAX_POSITIONS:
            continue
        if state["spent_today"] + MAX_TRADE_USD > DAILY_BUDGET_USD:
            await notify("Kunlik limitga yetildi.")
            break
        try:
            a = await asyncio.to_thread(analyze, m, spot)
        except Exception as e:
            log.warning("analiz xato %s", e)
            continue
        checked += 1
        edge = a["prob"] - m["yes"]
        if a["confidence"] < MIN_CONF or abs(edge) < MIN_EDGE:
            continue
        side, price, token = ("YES", m["yes"], m["yes_token"]) if edge > 0 else ("NO", m["no"], m["no_token"])
        if not 0.05 < price < 0.95:
            continue
        try:
            res = await asyncio.to_thread(place_buy, token, price, MAX_TRADE_USD)
        except Exception as e:
            await notify(f"❌ Buyurtma xatosi: {e}")
            continue
        state["spent_today"] += MAX_TRADE_USD
        state["positions"].append({"market_id": m["id"], "question": m["question"], "side": side,
                                   "token": token, "shares": round(MAX_TRADE_USD / price, 2),
                                   "price": price, "usd": MAX_TRADE_USD, "live": LIVE,
                                   "ts": int(time.time())})
        save_state()
        await notify(f"{'🟢 REAL' if LIVE else '📝 PAPER'} {side} @ {price:.2f} (${MAX_TRADE_USD})\n"
                     f"{m['question']}\nClaude: {a['prob']:.0%} vs bozor {m['yes']:.0%}, "
                     f"ishonch {a['confidence']:.0%}\n{a['reason']}")
    if manual:
        await notify(f"Skan tugadi. Bozor: {len(markets)}, tahlil: {checked}.")

def pnl_totals():
    realized = sum(c["pnl"] for c in state["closed"])
    wins = sum(1 for c in state["closed"] if c["pnl"] > 0)
    return realized, wins, len(state["closed"])

async def close_position(app, p, cur, reason):
    try:
        await asyncio.to_thread(place_sell, p["token"], cur, p["shares"])
    except Exception as e:
        await app.bot.send_message(ADMIN_ID, f"❌ Sotish xatosi: {e}")
        return False
    pnl = round(p["shares"] * cur - p["usd"], 2)
    state["positions"].remove(p)
    state["closed"].append({**p, "exit": cur, "pnl": pnl, "reason": reason,
                            "closed_ts": int(time.time())})
    save_state()
    await app.bot.send_message(
        ADMIN_ID, f"{'✅' if pnl >= 0 else '🔻'} {reason}: {p['side']} {p['price']:.2f} → {cur:.2f}\n"
                  f"PnL: ${pnl:+.2f}\n{p['question']}")
    return True

async def manage_positions(app: Application, force=False):
    for p in list(state["positions"]):
        try:
            m = await asyncio.to_thread(get_market_now, p["market_id"])
            prices = jl(m["outcomePrices"])
            cur = float(prices[0] if p["side"] == "YES" else prices[1])
        except Exception as e:
            log.warning("narx xato %s", e)
            continue
        change = cur / p["price"] - 1
        if m.get("closed") and (cur >= 0.99 or cur <= 0.01):
            # bozor yopilgan; LIVE da yutuqni Polymarket saytida 'Redeem' qiling
            p2 = {**p}
            pnl = round(p["shares"] * cur - p["usd"], 2)
            state["positions"].remove(p)
            state["closed"].append({**p2, "exit": cur, "pnl": pnl, "reason": "resolved",
                                    "closed_ts": int(time.time())})
            save_state()
            await app.bot.send_message(ADMIN_ID, f"🏁 Bozor yopildi: {'yutdi' if cur > 0.5 else 'yutqazdi'} "
                                       f"${pnl:+.2f}\n{p['question']}"
                                       + ("\n(Real: yutuqni saytda Redeem qiling)" if p["live"] and cur > 0.5 else ""))
        elif force:
            await close_position(app, p, cur, "qo'lda yopildi")
        elif change >= TAKE_PROFIT:
            await close_position(app, p, cur, "Take-profit")
        elif change <= -STOP_LOSS:
            await close_position(app, p, cur, "Stop-loss")

async def build_report(app: Application):
    realized, wins, n = pnl_totals()
    unreal, lines = 0.0, []
    for p in state["positions"]:
        try:
            m = await asyncio.to_thread(get_market_now, p["market_id"])
            pr = jl(m["outcomePrices"])
            cur = float(pr[0] if p["side"] == "YES" else pr[1])
        except Exception:
            continue
        u = p["shares"] * cur - p["usd"]
        unreal += u
        lines.append(f"{p['side']} {p['price']:.2f}→{cur:.2f} ({u:+.2f}$) {p['question'][:50]}")
    wr = f"{wins/n:.0%}" if n else "-"
    return (f"📊 PnL hisobot ({'REAL' if LIVE else 'PAPER'})\n"
            f"Yopilgan: {n} ta, yutuq foizi: {wr}\n"
            f"Realized: ${realized:+.2f}\nUnrealized: ${unreal:+.2f}\n"
            f"Jami: ${realized + unreal:+.2f}\n\nOchiq pozitsiyalar:\n"
            + ("\n".join(lines) if lines else "yo'q"))

def admin_only(fn):
    async def wrap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return
        return await fn(update, ctx)
    return wrap

@admin_only
async def cmd_start(u, c):
    await u.message.reply_text("Buyruqlar: /status /positions /pnl /scan /closeall /stop /resume")

@admin_only
async def cmd_status(u, c):
    reset_day()
    await u.message.reply_text(
        f"Rejim: {'REAL' if LIVE else 'PAPER'}\nHolat: {'⏸ to‘xtatilgan' if state['paused'] else '▶️ ishlayapti'}\n"
        f"Pozitsiyalar: {len(state['positions'])}/{MAX_POSITIONS}\n"
        f"Bugun sarflandi: ${state['spent_today']:.2f}/${DAILY_BUDGET_USD}")

@admin_only
async def cmd_positions(u, c):
    if not state["positions"]:
        return await u.message.reply_text("Pozitsiya yo'q.")
    await u.message.reply_text("\n\n".join(
        f"{p['side']} @ {p['price']:.2f} ${p['usd']}\n{p['question']}" for p in state["positions"]))

@admin_only
async def cmd_stop(u, c):
    state["paused"] = True; save_state()
    await u.message.reply_text("⏸ Savdo to'xtatildi (kill switch).")

@admin_only
async def cmd_resume(u, c):
    state["paused"] = False; save_state()
    await u.message.reply_text("▶️ Davom etadi.")

@admin_only
async def cmd_scan(u, c):
    await u.message.reply_text("Skan boshlandi...")
    await scan(c.application, manual=True)

@admin_only
async def cmd_pnl(u, c):
    await u.message.reply_text(await build_report(c.application))

@admin_only
async def cmd_closeall(u, c):
    await u.message.reply_text("Hamma pozitsiyalar yopilmoqda...")
    await manage_positions(c.application, force=True)

async def daily_report(ctx):
    await ctx.application.bot.send_message(ADMIN_ID, await build_report(ctx.application))

def main():
    load_state()
    app = Application.builder().token(TG_TOKEN).build()
    for name, fn in [("start", cmd_start), ("status", cmd_status), ("positions", cmd_positions),
                     ("stop", cmd_stop), ("resume", cmd_resume), ("scan", cmd_scan),
                     ("pnl", cmd_pnl), ("closeall", cmd_closeall)]:
        app.add_handler(CommandHandler(name, fn))
    app.job_queue.run_repeating(lambda ctx: scan(ctx.application), interval=SCAN_MINUTES * 60, first=10)
    app.job_queue.run_repeating(lambda ctx: manage_positions(ctx.application),
                                interval=CHECK_MINUTES * 60, first=60)
    app.job_queue.run_daily(daily_report, time=dtime(hour=REPORT_HOUR_UTC, tzinfo=timezone.utc))
    app.run_polling()

if __name__ == "__main__":
    main()
