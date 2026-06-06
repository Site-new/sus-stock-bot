import json
import os
import time
import secrets
import requests
from flask import Flask, jsonify, render_template_string, redirect, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

DATA_FILE = os.environ.get("DATA_FILE", "/data/data.json" if os.path.isdir("/data") else "data.json")
CHAT_FILE = DATA_FILE.replace("data.json", "chat.json")
STARTING_BALANCE = 1000.0
TRADE_FEE = 0.01  # 1% fee on SUS buys and sells (slows progression, drains money)

# Newest entries first. Keep these short and player-friendly.
CHANGELOG = [
    {"date": "Jun 6", "items": [
        "📱 The site is now mobile-friendly — the top buttons collapse into a ☰ menu and tap targets are bigger.",
        "🔴 While the market is closed (12am–12pm CST) the SUS price is frozen, no news happens, and you can't buy/sell/short. Companies, the lottery, the store, and server unlocks still work.",
        "📈 The buy/sell average-cost line now also shows when you trade as a company.",
        "🔍 Insider Rings can now upgrade their lead time (2–5 min before the public). Faster tiers cost upkeep from the treasury every 20 min.",
        "📈 A Max button was added next to the Buy / Sell / Short boxes.",
        "📉 Open short positions now update their profit/loss live with the price.",
        "🐷 CEO deposits into a Savings Bank now count as the bank's own capital, not money owed to depositors.",
    ]},
    {"date": "Jun 5", "items": [
        "📋 Update log added (you're reading it!) — check here for what's new.",
        "📰 Market news now happens twice as often.",
        "🐷 Savings Banks show owners a live profit split (bank value vs. owed to depositors).",
        "📈 The chart now draws a yellow dashed line at your average buy price.",
        "💸 Trading fee lowered from 3% to 1%.",
    ]},
    {"date": "Jun 4", "items": [
        "🔁 New Copy Trading company — subscribe and auto-mirror the company's SUS trades.",
        "📋 New Analyst Firm company — sell Buy/Hold/Sell ratings to subscribers.",
        "🕯️ Candlestick chart toggle added next to the zoom buttons.",
        "🎰 Casino now has 5 games (Coin Flip, High Card, Dice, Roulette, Slots).",
        "🔓 Server Unlocks — pool money to unlock the Nether ($100k) and End ($10M).",
    ]},
]


@app.route("/api/changelog")
def api_changelog():
    return jsonify(CHANGELOG)

DISCORD_CLIENT_ID     = os.environ.get("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI  = os.environ.get("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_AUTH_URL  = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL  = "https://discord.com/api/users/@me"

# In-memory username cache so we don't hammer Discord API
_username_cache = {}

def get_discord_username(user_id):
    if user_id in _username_cache:
        return _username_cache[user_id]
    if not DISCORD_BOT_TOKEN:
        return None
    try:
        res = requests.get(
            f"https://discord.com/api/v10/users/{user_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            timeout=3,
        )
        if res.status_code == 200:
            data = res.json()
            name = data.get("global_name") or data.get("username") or None
            _username_cache[user_id] = name
            return name
    except Exception:
        pass
    return None


def load_data():
    for f_path in [DATA_FILE, DATA_FILE + ".tmp"]:
        if os.path.exists(f_path):
            try:
                with open(f_path, "r") as f:
                    return json.load(f)
            except Exception:
                continue
    return {"users": {}, "stock_price": 50.0, "price_history": [50.0]}


def save_data(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)


def get_user(data, user_id):
    uid = str(user_id)
    if uid not in data["users"]:
        data["users"][uid] = {"balance": STARTING_BALANCE, "shares": 0}
    return data["users"][uid]


# ── Credit score ────────────────────────────────────────────────────────────────

def get_credit(u):
    return u.get("credit", 500)

def adjust_credit(u, delta):
    u["credit"] = max(300, min(850, get_credit(u) + delta))
    return u["credit"]

def credit_tier(score):
    if score >= 800:
        return {"name": "Exceptional", "emoji": "💎", "color": "#7ad7ff"}
    if score >= 740:
        return {"name": "Very Good", "emoji": "🟢", "color": "#57f287"}
    if score >= 670:
        return {"name": "Good", "emoji": "🥇", "color": "#ffd700"}
    if score >= 580:
        return {"name": "Fair", "emoji": "🟡", "color": "#fee75c"}
    return {"name": "Poor", "emoji": "🔴", "color": "#ed4245"}

def send_limit(score):
    """Max single transfer by credit tier. None = unlimited."""
    if score >= 800:
        return None
    if score >= 740:
        return 100000
    if score >= 670:
        return 25000
    if score >= 580:
        return 5000
    return 1000


def is_banned(u):
    """True if user is permanently or temporarily banned."""
    b = u.get("banned_until", 0)
    if b == -1:
        return True
    return b and time.time() < b


@app.before_request
def block_banned_users():
    """Banned users can view but not perform any state-changing action."""
    if request.method == "POST" and request.path.startswith("/api/") and "user_id" in session:
        if request.path == "/api/admin/ban":
            return  # admin ban endpoint handles its own auth
        try:
            data = load_data()
            u = data.get("users", {}).get(session["user_id"])
            if u and is_banned(u):
                until = u.get("banned_until", 0)
                msg = "Your account is permanently banned." if until == -1 else "Your account is temporarily banned."
                return jsonify({"error": msg}), 403
        except Exception:
            pass


def fmt(amount):
    return f"${amount:,.2f}"


def is_market_open():
    """Market is open noon–midnight CST. While closed, the price is frozen and no trading."""
    import datetime as dt
    cst = dt.timezone(dt.timedelta(hours=-6))
    return dt.datetime.now(cst).hour >= 12


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.route("/login")
def login():
    state = secrets.token_hex(16)
    session["oauth_state"] = state
    session["login_next"] = request.args.get("next", "/")
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    url = DISCORD_AUTH_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return redirect(url)


@app.route("/callback")
def callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        return "Invalid state", 400

    code = request.args.get("code")
    token_res = requests.post(DISCORD_TOKEN_URL, data={
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})

    token_data = token_res.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return "Auth failed", 400

    user_res = requests.get(DISCORD_USER_URL, headers={"Authorization": f"Bearer {access_token}"})
    user_data = user_res.json()

    session["user_id"] = user_data["id"]
    session["username"] = user_data.get("username", "Unknown")
    session["avatar"] = user_data.get("avatar")

    # Register user in data.json if first time
    data = load_data()
    get_user(data, user_data["id"])
    save_data(data)

    next_url = session.pop("login_next", "/")
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


ADMIN_USERNAME = "slasher_asher"

def is_admin():
    return session.get("username") == ADMIN_USERNAME


# ── Admin API ──────────────────────────────────────────────────────────────────

@app.route("/api/admin/users")
def admin_users():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    data = load_data()
    price = data["stock_price"]
    users = []
    for uid, u in data["users"].items():
        username = get_discord_username(uid)
        invested = round(u["shares"] * price, 2)
        net_worth = round(u["balance"] + invested, 2)
        users.append({"id": uid, "username": username or f"User #{uid[-4:]}", "shares": u["shares"],
                      "cash": u["balance"], "net_worth": net_worth, "verified": u.get("verified", False),
                      "banned": is_banned(u)})
    users.sort(key=lambda x: x["net_worth"], reverse=True)
    return jsonify(users)


@app.route("/api/admin/verify", methods=["POST"])
def admin_verify():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    uid = str(request.json.get("user_id", ""))
    verified = bool(request.json.get("verified", True))
    data = load_data()
    u = get_user(data, uid)
    u["verified"] = verified
    save_data(data)
    return jsonify({"ok": True, "verified": verified})


@app.route("/api/admin/reset_market", methods=["POST"])
def admin_reset_market():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    data = load_data()
    for uid, u in list(data.get("users", {}).items()):
        data["users"][uid] = {
            "balance": STARTING_BALANCE, "shares": 0, "credit": 500,
            "verified": u.get("verified", False), "banned_until": u.get("banned_until", 0),
        }
    data["stock_price"] = 50.0
    data["price_history"] = [50.0]
    data["price_timestamps"] = []
    data["news_feed"] = []
    data["shorts"] = {}
    data["limit_orders"] = []
    data["pending_earnings"] = []
    data["pending_bull_bear"] = None
    data["notifications"] = {}
    data["history"] = {}
    data["unlocks"] = {}
    data["lottery"] = {"open": False, "ends_at": 0, "next_open": 0, "pot": 0, "tickets": {}}

    # Delete every store-created Discord role
    gid = get_guild_id()
    deleted = 0
    if gid and DISCORD_BOT_TOKEN:
        for role_id in data.get("store_roles", []):
            try:
                r = requests.delete(f"{DISCORD_API}/guilds/{gid}/roles/{role_id}", headers=_dh(), timeout=8)
                if r.status_code in (200, 204):
                    deleted += 1
            except Exception:
                pass
    data["store_roles"] = []

    save_data(data)
    try:
        save_companies({})  # wipe all companies
    except Exception:
        pass
    return jsonify({"ok": True, "roles_deleted": deleted})


@app.route("/api/admin/fire_news", methods=["POST"])
def admin_fire_news():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    import random as _r
    from headlines import get_headline
    positive = _r.random() > 0.45
    impact = round((_r.uniform(5.5, 17.5) if positive else _r.uniform(-15.5, -5.5)), 2)
    headline = get_headline(positive)
    data = load_data()
    now = int(time.time())
    events = data.get("news_feed", [])
    events.append({"headline": f"📰 {headline}", "positive": positive, "impact": impact,
                   "ts": now, "public_at": now + 300})
    data["news_feed"] = events[-50:]
    data.setdefault("pending_earnings", []).append({"impact_pct": impact, "apply_at": now + 300})
    save_data(data)
    return jsonify({"ok": True, "headline": headline, "impact": impact})


@app.route("/api/admin/lottery", methods=["POST"])
def admin_lottery():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    import random as _r
    action = request.json.get("action", "")
    data = load_data()
    lot = data.get("lottery") or {"open": False, "ends_at": 0, "next_open": 0, "pot": 0, "tickets": {}}
    now = int(time.time())
    news = data.setdefault("news_feed", [])
    if action == "start":
        lot = {"open": True, "ends_at": now + 600, "next_open": 0, "pot": 0, "tickets": {}}
        news.append({"headline": "🎟️ LOTTERY OPEN for 10 minutes! Buy tickets on the website.",
                     "positive": True, "impact": 0, "ts": now, "public_at": now, "kind": "announcement"})
    elif action == "draw":
        tickets = lot.get("tickets", {})
        pot = round(lot.get("pot", 0), 2)
        pool = [uid for uid, c in tickets.items() for _ in range(int(c))]
        if pool and pot > 0:
            winner = _r.choice(pool)
            wu = get_user(data, winner)
            wu["balance"] = round(wu["balance"] + pot, 2)
            wname = get_discord_username(winner) or "A player"
            news.append({"headline": f"🎟️ LOTTERY: {wname} won {fmt(pot)} with {tickets.get(winner,0)} tickets!",
                         "positive": True, "impact": 0, "ts": now, "public_at": now, "kind": "announcement"})
            add_notification(data, winner, f"🎟️ You WON the lottery — {fmt(pot)}!", True)
        else:
            news.append({"headline": "🎟️ LOTTERY: No tickets sold — no winner.", "positive": False,
                         "impact": 0, "ts": now, "public_at": now, "kind": "announcement"})
        lot = {"open": False, "ends_at": 0, "next_open": now + 7200, "pot": 0, "tickets": {}}
    elif action == "cancel":
        for uid, c in lot.get("tickets", {}).items():
            if uid in data["users"]:
                data["users"][uid]["balance"] = round(data["users"][uid]["balance"] + c * LOTTERY_TICKET_PRICE, 2)
        lot = {"open": False, "ends_at": 0, "next_open": now + 7200, "pot": 0, "tickets": {}}
        news.append({"headline": "🎟️ Lottery round cancelled — tickets refunded.", "positive": False,
                     "impact": 0, "ts": now, "public_at": now, "kind": "announcement"})
    else:
        return jsonify({"error": "unknown action"}), 400
    data["news_feed"] = news[-50:]
    data["lottery"] = lot
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/admin/ban", methods=["POST"])
def admin_ban():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    uid = str(request.json.get("user_id", ""))
    minutes = request.json.get("minutes", 0)  # 0 = unban, -1 = permanent, >0 = temp
    data = load_data()
    u = get_user(data, uid)
    if minutes == 0:
        u["banned_until"] = 0
        result = "unbanned"
    elif minutes == -1:
        u["banned_until"] = -1
        result = "permanently banned"
    else:
        u["banned_until"] = int(time.time() + float(minutes) * 60)
        result = f"banned for {minutes} min"
    save_data(data)
    return jsonify({"ok": True, "result": result})


@app.route("/api/admin/set_price", methods=["POST"])
def admin_set_price():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    price = float(request.json.get("price", 0))
    if price <= 0:
        return jsonify({"error": "invalid price"}), 400
    data = load_data()
    data["stock_price"] = round(price, 2)
    data.setdefault("price_history", []).append(round(price, 2))
    save_data(data)
    return jsonify({"ok": True, "price": price})


@app.route("/api/admin/give_shares", methods=["POST"])
def admin_give_shares():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    uid = str(request.json.get("user_id", ""))
    shares = int(request.json.get("shares", 0))
    data = load_data()
    u = get_user(data, uid)
    u["shares"] = max(0, u["shares"] + shares)
    save_data(data)
    return jsonify({"ok": True, "shares": u["shares"]})


@app.route("/api/admin/give_cash", methods=["POST"])
def admin_give_cash():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    uid = str(request.json.get("user_id", ""))
    amount = float(request.json.get("amount", 0))
    data = load_data()
    u = get_user(data, uid)
    u["balance"] = round(max(0, u["balance"] + amount), 2)
    save_data(data)
    return jsonify({"ok": True, "balance": u["balance"]})


@app.route("/api/admin/reset_user", methods=["POST"])
def admin_reset_user():
    if not is_admin():
        return jsonify({"error": "forbidden"}), 403
    uid = str(request.json.get("user_id", ""))
    data = load_data()
    if uid in data["users"]:
        data["users"][uid] = {"balance": STARTING_BALANCE, "shares": 0}
        save_data(data)
    return jsonify({"ok": True})


# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/api/stock")
def api_stock():
    data = load_data()
    history = data.get("price_history", [data["stock_price"]])
    price = data["stock_price"]
    prev = history[-2] if len(history) >= 2 else price
    change = round(price - prev, 2)
    pct = round((change / prev * 100) if prev else 0, 2)
    timestamps = data.get("price_timestamps", [])[-100:]
    market_open = is_market_open()
    cycle = data.get("bull_bear", "neutral")
    sentiment = data.get("sentiment", 50)

    # Insider rings see news early; higher early-access levels see it sooner.
    level = user_insider_level(session.get("user_id"))
    is_insider = level >= 0
    now = int(time.time())
    all_news = data.get("news_feed", [])
    news = [n for n in all_news if insider_reveal_time(n, level) <= now][-10:]

    # Merge in this user's personal notifications (e.g. money received)
    if session.get("user_id"):
        personal = data.get("notifications", {}).get(session["user_id"], [])
        news = (news + personal)
        news.sort(key=lambda n: n.get("ts", 0))
        news = news[-15:]

    return jsonify({
        "price": price, "change": change, "change_pct": pct,
        "history": history[-100:], "timestamps": timestamps,
        "market_open": market_open, "bull_bear": cycle,
        "sentiment": sentiment, "news": news, "is_insider": is_insider,
        "analyst_rating": user_analyst_rating(session.get("user_id")),
    })


@app.route("/api/leaderboard")
def api_leaderboard():
    data = load_data()
    price = data["stock_price"]
    rows = []
    for uid, u in data["users"].items():
        invested = round(u["shares"] * price, 2)
        net_worth = round(u["balance"] + invested, 2)
        username = get_discord_username(uid)
        rows.append({"id": uid, "username": username, "shares": u["shares"], "cash": u["balance"],
                     "invested": invested, "net_worth": net_worth,
                     "pnl": round(net_worth - STARTING_BALANCE, 2), "is_company": False,
                     "verified": u.get("verified", False),
                     "credit_emoji": credit_tier(get_credit(u))["emoji"]})
    # Include companies, ranked by total value, showing their CEO
    try:
        from companies import load_companies as _lc, company_value as _cv
        for c in _lc().values():
            ceo_name = get_discord_username(c.get("ceo")) or "Unknown"
            rows.append({
                "id": c["id"], "username": f"{c['name']} ({c['ticker']})",
                "shares": c.get("sus_shares", 0), "cash": c.get("treasury", 0),
                "invested": round(c.get("sus_shares", 0) * price, 2),
                "net_worth": _cv(c, price), "pnl": 0,
                "is_company": True, "ceo": ceo_name,
            })
    except Exception:
        pass
    rows.sort(key=lambda x: x["net_worth"], reverse=True)
    return jsonify(rows)


@app.route("/api/act_as", methods=["POST"])
def api_act_as():
    """CEO toggles trading on behalf of a company (or '' for personal)."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    cid = str(request.json.get("company_id", "") or "")
    if not cid:
        session.pop("acting_as", None)
        return jsonify({"ok": True, "acting_as": None})
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]):
        return jsonify({"error": "you must be the CEO"}), 403
    session["acting_as"] = cid
    return jsonify({"ok": True, "acting_as": cid})


@app.route("/api/me")
def api_me():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    data = load_data()
    u = get_user(data, session["user_id"])
    save_data(data)
    price = data["stock_price"]
    uid = session["user_id"]

    # Companies this user is CEO of (for the "trade as" selector)
    companies = load_companies()
    my_companies = [{"id": c["id"], "name": c["name"], "ticker": c["ticker"]}
                    for c in companies.values() if is_ceo(c, uid)]

    # Validate acting_as
    acting_id = session.get("acting_as")
    acting = companies.get(acting_id) if acting_id else None
    if acting and not is_ceo(acting, uid):
        acting = None
        session.pop("acting_as", None)

    if acting:
        # Report the company's position instead of the user's
        invested = round(acting.get("sus_shares", 0) * price, 2)
        cash = round(acting.get("treasury", 0), 2)
        net_worth = round(cash + invested, 2)
        cshort = acting.get("short")
        cshort_pnl = round((cshort["entry_price"] - price) * cshort["shares"], 2) if cshort else None
        return jsonify({
            "username": session["username"], "avatar": session.get("avatar"), "user_id": uid,
            "shares": acting.get("sus_shares", 0), "cash": cash, "invested": invested,
            "net_worth": net_worth, "pnl": 0, "price": price,
            "short": cshort, "short_pnl": cshort_pnl, "limit_orders": [],
            "verified": u.get("verified", False), "is_admin": is_admin(),
            "acting_as": {"id": acting["id"], "name": acting["name"], "ticker": acting["ticker"]},
            "my_companies": my_companies,
            "avg_cost": acting.get("avg_cost", 0),
        })

    invested = round(u["shares"] * price, 2)
    net_worth = round(u["balance"] + invested, 2)
    short = data.get("shorts", {}).get(uid)
    short_pnl = round((short["entry_price"] - price) * short["shares"], 2) if short else None
    limit_orders = [o for o in data.get("limit_orders", []) if o["user_id"] == uid]
    return jsonify({
        "username": session["username"],
        "avatar": session.get("avatar"),
        "user_id": uid,
        "shares": u["shares"],
        "cash": u["balance"],
        "invested": invested,
        "net_worth": net_worth,
        "pnl": round(net_worth - STARTING_BALANCE, 2),
        "price": price,
        "short": short,
        "short_pnl": short_pnl,
        "limit_orders": limit_orders,
        "verified": u.get("verified", False),
        "is_admin": is_admin(),
        "acting_as": None,
        "my_companies": my_companies,
        "credit": get_credit(u),
        "credit_tier": credit_tier(get_credit(u)),
        "send_limit": send_limit(get_credit(u)),
        "mc_linked": next((l.get("username") for l in data.get("mc_links", {}).values()
                           if l.get("discord_id") == uid), None),
        "avg_cost": u.get("avg_cost", 0),
    })


@app.route("/api/buy", methods=["POST"])
def api_buy():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    if not is_market_open():
        return jsonify({"error": "Market is closed (open 12pm–12am CST). You can't trade SUS right now."}), 400
    shares = int(request.json.get("shares", 0))
    if shares <= 0:
        return jsonify({"error": "invalid amount"}), 400
    data = load_data()
    price = data["stock_price"]
    cost = round(price * shares * (1 + TRADE_FEE), 2)  # 3% trading fee
    companies = load_companies()
    acting, acting_id = get_acting_company(companies)
    if acting:
        if acting["treasury"] < cost:
            return jsonify({"error": f"Company needs {fmt(cost)} (incl. 1% fee), has {fmt(acting['treasury'])}"}), 400
        acting["treasury"] = round(acting["treasury"] - cost, 2)
        old_cshares = acting.get("sus_shares", 0)
        old_cavg = acting.get("avg_cost", 0)
        acting["avg_cost"] = round((old_cavg * old_cshares + price * shares) / (old_cshares + shares), 4) if (old_cshares + shares) > 0 else 0
        acting["sus_shares"] = old_cshares + shares
        mirror_copy_trades(data, acting, "buy", shares, price)
        save_companies(companies)
        save_data(data)
        return jsonify({"ok": True, "bought": shares, "cost": cost, "balance": acting["treasury"], "shares": acting["sus_shares"]})
    u = get_user(data, session["user_id"])
    if u["balance"] < cost:
        return jsonify({"error": f"Not enough cash. Need {fmt(cost)} (incl. 1% fee), have {fmt(u['balance'])}"}), 400
    u["balance"] = round(u["balance"] - cost, 2)
    # Update average cost basis (price paid per share, excl. fee)
    old_shares = u["shares"]
    old_avg = u.get("avg_cost", 0)
    u["avg_cost"] = round((old_avg * old_shares + price * shares) / (old_shares + shares), 4) if (old_shares + shares) > 0 else 0
    u["shares"] += shares
    log_transaction(data, session["user_id"], "buy", f"Bought {shares} SUS @ {fmt(price)} (1% fee)", -cost)
    save_data(data)
    return jsonify({"ok": True, "bought": shares, "cost": cost, "balance": u["balance"], "shares": u["shares"]})


@app.route("/api/sell", methods=["POST"])
def api_sell():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    if not is_market_open():
        return jsonify({"error": "Market is closed (open 12pm–12am CST). You can't trade SUS right now."}), 400
    shares = int(request.json.get("shares", 0))
    if shares <= 0:
        return jsonify({"error": "invalid amount"}), 400
    data = load_data()
    price = data["stock_price"]
    earnings = round(price * shares * (1 - TRADE_FEE), 2)  # 3% trading fee
    companies = load_companies()
    acting, acting_id = get_acting_company(companies)
    if acting:
        if acting.get("sus_shares", 0) < shares:
            return jsonify({"error": f"Company only has {acting.get('sus_shares', 0)} shares"}), 400
        acting["sus_shares"] -= shares
        acting["treasury"] = round(acting["treasury"] + earnings, 2)
        if acting["sus_shares"] <= 0:
            acting["avg_cost"] = 0
        mirror_copy_trades(data, acting, "sell", shares, price)
        save_companies(companies)
        save_data(data)
        return jsonify({"ok": True, "sold": shares, "earnings": earnings, "balance": acting["treasury"], "shares": acting["sus_shares"]})
    u = get_user(data, session["user_id"])
    if u["shares"] < shares:
        return jsonify({"error": f"You only have {u['shares']} shares"}), 400
    u["shares"] -= shares
    u["balance"] = round(u["balance"] + earnings, 2)
    if u["shares"] <= 0:
        u["avg_cost"] = 0  # reset cost basis when fully sold out
    log_transaction(data, session["user_id"], "sell", f"Sold {shares} SUS @ {fmt(price)} (1% fee)", earnings)
    save_data(data)
    return jsonify({"ok": True, "sold": shares, "earnings": earnings, "balance": u["balance"], "shares": u["shares"]})


@app.route("/api/short", methods=["POST"])
def api_short():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    if not is_market_open():
        return jsonify({"error": "Market is closed (open 12pm–12am CST). You can't trade SUS right now."}), 400
    shares = int(request.json.get("shares", 0))
    if shares <= 0:
        return jsonify({"error": "invalid amount"}), 400
    data = load_data()
    uid = session["user_id"]
    price = data["stock_price"]
    companies = load_companies()
    acting, acting_id = get_acting_company(companies)
    if acting:
        if acting.get("short"):
            return jsonify({"error": "Company already has an open short. Cover it first."}), 400
        acting["short"] = {"shares": shares, "entry_price": price}
        save_companies(companies)
        return jsonify({"ok": True, "shares": shares, "entry_price": price})
    if uid in data.get("shorts", {}):
        return jsonify({"error": "You already have an open short. Cover it first."}), 400
    get_user(data, uid)
    data.setdefault("shorts", {})[uid] = {"shares": shares, "entry_price": price}
    log_transaction(data, uid, "short", f"Shorted {shares} SUS @ {fmt(price)}", 0)
    save_data(data)
    return jsonify({"ok": True, "shares": shares, "entry_price": price})


@app.route("/api/cover", methods=["POST"])
def api_cover():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    if not is_market_open():
        return jsonify({"error": "Market is closed (open 12pm–12am CST). You can't trade SUS right now."}), 400
    data = load_data()
    uid = session["user_id"]
    price = data["stock_price"]
    companies = load_companies()
    acting, acting_id = get_acting_company(companies)
    if acting:
        if not acting.get("short"):
            return jsonify({"error": "Company has no open short."}), 400
        cshort = acting.pop("short")
        cpnl = round((cshort["entry_price"] - price) * cshort["shares"], 2)
        acting["treasury"] = round(max(0, acting["treasury"] + cpnl), 2)
        save_companies(companies)
        return jsonify({"ok": True, "pnl": cpnl, "bonus": 0, "balance": acting["treasury"]})
    shorts = data.get("shorts", {})
    if uid not in shorts:
        return jsonify({"error": "No open short position."}), 400
    short = shorts.pop(uid)
    pnl = round((short["entry_price"] - price) * short["shares"], 2)
    u = get_user(data, uid)

    # Short Selling Cartel: members get a 2× bonus on profitable covers,
    # the bonus paid out of the cartel's treasury.
    bonus = 0
    if pnl > 0:
        companies = load_companies()
        for c in companies.values():
            if c.get("type") == "short_cartel" and uid in c.get("members", {}):
                payable = min(pnl, c.get("treasury", 0))  # bonus capped by treasury
                if payable > 0:
                    bonus = round(payable, 2)
                    c["treasury"] = round(c["treasury"] - bonus, 2)
                    save_companies(companies)
                break

    total = round(pnl + bonus, 2)
    u["balance"] = round(max(0, u["balance"] + total), 2)
    data["shorts"] = shorts
    detail = f"Covered short @ {fmt(price)}" + (f" (+{fmt(bonus)} cartel bonus)" if bonus else "")
    log_transaction(data, uid, "cover", detail, total)
    save_data(data)
    return jsonify({"ok": True, "pnl": total, "bonus": bonus, "balance": u["balance"]})


@app.route("/api/limitbuy", methods=["POST"])
def api_limitbuy():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    shares = int(request.json.get("shares", 0))
    price = float(request.json.get("price", 0))
    if shares <= 0 or price <= 0:
        return jsonify({"error": "invalid values"}), 400
    data = load_data()
    u = get_user(data, session["user_id"])
    cost = round(shares * price, 2)
    if u["balance"] < cost:
        return jsonify({"error": f"Not enough cash. Need {fmt(cost)}"}), 400
    u["balance"] = round(u["balance"] - cost, 2)
    data.setdefault("limit_orders", []).append({
        "user_id": session["user_id"], "type": "buy", "shares": shares, "price": round(price, 2)
    })
    save_data(data)
    return jsonify({"ok": True, "shares": shares, "price": price, "reserved": cost})


@app.route("/api/limitsell", methods=["POST"])
def api_limitsell():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    shares = int(request.json.get("shares", 0))
    price = float(request.json.get("price", 0))
    if shares <= 0 or price <= 0:
        return jsonify({"error": "invalid values"}), 400
    data = load_data()
    u = get_user(data, session["user_id"])
    if u["shares"] < shares:
        return jsonify({"error": f"You only have {u['shares']} shares"}), 400
    u["shares"] -= shares
    data.setdefault("limit_orders", []).append({
        "user_id": session["user_id"], "type": "sell", "shares": shares, "price": round(price, 2)
    })
    save_data(data)
    return jsonify({"ok": True, "shares": shares, "price": price})


@app.route("/api/cancel_order", methods=["POST"])
def api_cancel_order():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    idx = int(request.json.get("index", -1))
    data = load_data()
    uid = session["user_id"]
    user_orders = [(i, o) for i, o in enumerate(data.get("limit_orders", [])) if o["user_id"] == uid]
    if idx < 0 or idx >= len(user_orders):
        return jsonify({"error": "invalid order"}), 400
    global_idx, order = user_orders[idx]
    # Refund reserved funds/shares
    u = get_user(data, uid)
    if order["type"] == "buy":
        u["balance"] = round(u["balance"] + order["shares"] * order["price"], 2)
    else:
        u["shares"] += order["shares"]
    data["limit_orders"].pop(global_idx)
    save_data(data)
    return jsonify({"ok": True})


# ── Transaction history & money transfers ──────────────────────────────────────

def log_transaction(data, user_id, kind, detail, amount):
    """Append a transaction to the user's history (stored in data.json)."""
    hist = data.setdefault("history", {})
    user_hist = hist.setdefault(str(user_id), [])
    user_hist.append({
        "kind": kind,          # buy, sell, short, cover, send, receive, dividend, company_buy, etc.
        "detail": detail,
        "amount": round(amount, 2),
        "ts": int(time.time()),
    })
    hist[str(user_id)] = user_hist[-100:]  # keep last 100 per user


def add_notification(data, user_id, headline, positive=True):
    """Add a personal notification shown in that user's news feed only."""
    notifs = data.setdefault("notifications", {})
    user_notifs = notifs.setdefault(str(user_id), [])
    user_notifs.append({"headline": headline, "positive": positive,
                        "impact": 0, "ts": int(time.time()), "kind": "personal"})
    notifs[str(user_id)] = user_notifs[-20:]


@app.route("/api/history")
def api_history():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    data = load_data()
    hist = data.get("history", {}).get(session["user_id"], [])
    return jsonify(list(reversed(hist)))


@app.route("/api/all_users")
def api_all_users():
    """List all known users by display name (for pickers)."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    data = load_data()
    out = [{"id": uid, "username": get_discord_username(uid) or f"User #{uid[-4:]}"}
           for uid in data["users"]]
    out.sort(key=lambda x: x["username"].lower())
    return jsonify(out)


@app.route("/api/verified_users")
def api_verified_users():
    """List verified users (for the send-money recipient picker). Verified only."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    data = load_data()
    me = get_user(data, session["user_id"])
    if not me.get("verified"):
        return jsonify({"error": "not verified"}), 403
    out = []
    for uid, u in data["users"].items():
        if u.get("verified") and uid != session["user_id"]:
            out.append({"id": uid, "username": get_discord_username(uid) or f"User #{uid[-4:]}"})
    return jsonify(out)


@app.route("/api/send_money", methods=["POST"])
def api_send_money():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    data = load_data()
    sender = get_user(data, session["user_id"])
    if not sender.get("verified"):
        return jsonify({"error": "Only verified accounts can send money"}), 403
    now = int(time.time())
    last_send = sender.get("last_send", 0)
    if now - last_send < 120:
        remain = 120 - (now - last_send)
        return jsonify({"error": f"You can send again in {remain}s"}), 429
    recipient_id = str(request.json.get("recipient_id", ""))
    amount = float(request.json.get("amount", 0))
    if amount <= 0:
        return jsonify({"error": "invalid amount"}), 400
    lim = send_limit(get_credit(sender))
    if lim is not None and amount > lim:
        tier = credit_tier(get_credit(sender))
        return jsonify({"error": f"Your {tier['name']} credit limits sends to {fmt(lim)}. Repay loans to raise it."}), 400
    if recipient_id == session["user_id"]:
        return jsonify({"error": "can't send to yourself"}), 400
    if recipient_id not in data["users"]:
        return jsonify({"error": "recipient not found"}), 400
    recipient = data["users"][recipient_id]
    if not recipient.get("verified"):
        return jsonify({"error": "Recipient must be verified to receive money"}), 400
    if sender["balance"] < amount:
        return jsonify({"error": "not enough cash"}), 400
    sender["balance"] = round(sender["balance"] - amount, 2)
    recipient["balance"] = round(recipient["balance"] + amount, 2)
    sender["last_send"] = now
    recip_name = get_discord_username(recipient_id) or f"User #{recipient_id[-4:]}"
    sender_name = session.get("username", "Someone")
    log_transaction(data, session["user_id"], "send", f"Sent to {recip_name}", -amount)
    log_transaction(data, recipient_id, "receive", f"Received from {sender_name}", amount)
    add_notification(data, recipient_id, f"💸 You received {fmt(amount)} from {sender_name}!", True)
    save_data(data)
    return jsonify({"ok": True, "balance": sender["balance"]})


# ── Chat ──────────────────────────────────────────────────────────────────────

CHAT_META_FILE = DATA_FILE.replace("data.json", "chat_meta.json")

def load_chat_meta():
    if not os.path.exists(CHAT_META_FILE):
        return {}
    with open(CHAT_META_FILE, "r") as f:
        return json.load(f)

def load_chat():
    if not os.path.exists(CHAT_FILE):
        return []
    with open(CHAT_FILE, "r") as f:
        return json.load(f)

def save_chat(messages):
    tmp = CHAT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(messages[-200:], f)
    os.replace(tmp, CHAT_FILE)


@app.route("/api/chat")
def api_chat():
    after = int(request.args.get("after", 0))
    messages = load_chat()
    return jsonify([m for m in messages if m["id"] > after])


@app.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    text = request.json.get("text", "").strip()
    if not text or len(text) > 300:
        return jsonify({"error": "invalid message"}), 400
    messages = load_chat()
    msg_id = (messages[-1]["id"] + 1) if messages else 1
    import time
    msg = {
        "id": msg_id,
        "user_id": session["user_id"],
        "username": session["username"],
        "avatar": session.get("avatar"),
        "text": text,
        "ts": int(time.time()),
    }
    messages.append(msg)
    save_chat(messages)
    # Mirror to Discord #susstock-chat
    if DISCORD_BOT_TOKEN:
        try:
            channel_id = load_chat_meta().get("discord_channel_id")
            if channel_id:
                requests.post(
                    f"https://discord.com/api/v10/channels/{channel_id}/messages",
                    headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                    json={"content": f"**{msg['username']}** (web): {text}"},
                    timeout=3
                )
        except Exception:
            pass
    return jsonify({"ok": True, "message": msg})


# ── Companies ─────────────────────────────────────────────────────────────────

from companies import (load_companies, save_companies, company_value,
                       company_stock_price, create_company, is_ceo,
                       get_member, COMPANY_TYPES, COMPANY_COST, SHARES_ISSUED)

SUB_TYPES = ("insider_ring", "analyst_firm", "copy_trading")


def user_in_insider_ring(user_id):
    """True if the user runs (CEO) or is a paying subscriber of any Insider Ring."""
    if not user_id:
        return False
    uid = str(user_id)
    try:
        companies = load_companies()
        for c in companies.values():
            if c.get("type") != "insider_ring":
                continue
            if c.get("ceo") == uid:
                return True
            if uid in c.get("subscribers", {}):
                return True
    except Exception:
        pass
    return False


def user_insider_level(user_id):
    """Best insider early-access level for a user across all rings they run/subscribe to.
    Returns -1 if not in any ring (public), else 0-3. Lead = (2 + level) minutes before price moves."""
    if not user_id:
        return -1
    uid = str(user_id)
    best = -1
    try:
        for c in load_companies().values():
            if c.get("type") != "insider_ring":
                continue
            if c.get("ceo") == uid or uid in c.get("subscribers", {}):
                best = max(best, int(c.get("early", 0)))
    except Exception:
        pass
    return best


def insider_reveal_time(news_item, level):
    """When a viewer with the given insider level (-1=public) gets to see this news item."""
    ts = news_item.get("ts", 0)
    public_at = news_item.get("public_at", ts)
    if level < 0:
        return public_at
    # public_at = ts + 300. level 0 -> see at ts+180 (120s/2min lead); level 3 -> ts (300s/5min lead).
    return ts + max(0, 180 - 60 * level)


def user_analyst_rating(user_id):
    """Return the rating set by the Analyst Firm this user runs/subscribes to (or None)."""
    if not user_id:
        return None
    uid = str(user_id)
    try:
        for c in load_companies().values():
            if c.get("type") != "analyst_firm":
                continue
            if c.get("ceo") == uid or uid in c.get("subscribers", {}):
                r = c.get("rating", "Offline")
                if r and r != "Offline":
                    return f"{r} - {c.get('name', 'Analyst Firm')}"
    except Exception:
        pass
    return None


def mirror_copy_trades(data, company, action, shares, price):
    """Replicate a copy-trading company's SUS trade onto subscribers who have copy on."""
    if company.get("type") != "copy_trading":
        return
    ceo = company.get("ceo")
    for uid, sub in company.get("subscribers", {}).items():
        if uid == ceo or not sub.get("copy"):
            continue
        u = data["users"].get(uid)
        if not u:
            continue
        if action == "buy":
            affordable = int(u["balance"] // (price * (1 + TRADE_FEE))) if price else 0
            n = min(shares, affordable)
            if n > 0:
                cost = round(price * n * (1 + TRADE_FEE), 2)
                u["balance"] = round(u["balance"] - cost, 2)
                u["shares"] = u.get("shares", 0) + n
                log_transaction(data, uid, "buy", f"🔁 Copied {company['ticker']}: bought {n} SUS", -cost)
        else:
            n = min(shares, u.get("shares", 0))
            if n > 0:
                earn = round(price * n * (1 - TRADE_FEE), 2)
                u["shares"] -= n
                u["balance"] = round(u["balance"] + earn, 2)
                log_transaction(data, uid, "sell", f"🔁 Copied {company['ticker']}: sold {n} SUS", earn)


def get_acting_company(companies):
    """Return (company_dict, company_id) the CEO is currently trading as, or (None, None)."""
    acting_id = session.get("acting_as")
    if not acting_id:
        return None, None
    c = companies.get(acting_id)
    if c and is_ceo(c, session.get("user_id")):
        return c, acting_id
    return None, None


def enrich_company(c, sus_price):
    """Add computed fields for API responses."""
    price = company_stock_price(c, sus_price)
    val = company_value(c, sus_price)
    c["_stock_price"] = price
    c["_value"] = val
    return c


@app.route("/api/companies")
def api_companies():
    data = load_data()
    sus_price = data["stock_price"]
    companies = load_companies()
    result = []
    for c in companies.values():
        result.append({
            "id": c["id"], "name": c["name"], "ticker": c["ticker"],
            "type": c["type"], "ceo": c["ceo"],
            "member_count": len(c.get("members", {})),
            "treasury": c["treasury"], "sus_shares": c.get("sus_shares", 0),
            "value": company_value(c, sus_price),
            "stock_price": company_stock_price(c, sus_price),
            "shares_issued": c.get("shares_issued", SHARES_ISSUED),
            "description": c.get("description", ""),
            "marketing": bool(c.get("upgrades", {}).get("marketing", 0)),
        })
    # Marketing-upgraded companies float to the top, then by value
    result.sort(key=lambda x: (x.get("marketing", False), x["value"]), reverse=True)
    return jsonify(result)


@app.route("/api/companies/<cid>")
def api_company(cid):
    data = load_data()
    sus_price = data["stock_price"]
    companies = load_companies()
    c = companies.get(cid)
    if not c:
        return jsonify({"error": "not found"}), 404
    uid = session.get("user_id")
    c["_stock_price"] = company_stock_price(c, sus_price)
    c["_value"] = company_value(c, sus_price)
    c["_is_member"] = uid in c.get("members", {})
    c["_is_ceo"] = is_ceo(c, uid) if uid else False
    c["_my_shares"] = c.get("shareholders", {}).get(uid, 0)
    c["_my_deposit"] = c.get("deposits", {}).get(uid, 0)
    c["_my_loan"] = c.get("loans", {}).get(uid)
    c["_my_policy"] = c.get("policies", {}).get(uid)

    # Investors (shareholders) with names
    investors = []
    for sh_uid, sh in c.get("shareholders", {}).items():
        if sh > 0:
            investors.append({"name": get_discord_username(sh_uid) or f"User #{sh_uid[-4:]}", "shares": sh})
    investors.sort(key=lambda x: x["shares"], reverse=True)
    c["_investors"] = investors

    # Subscribers (insider ring) with next due times
    subs = []
    for s_uid, s in c.get("subscribers", {}).items():
        subs.append({"id": s_uid, "name": get_discord_username(s_uid) or f"User #{s_uid[-4:]}",
                     "next_due": s.get("next_due", 0), "free": bool(s.get("free"))})
    subs.sort(key=lambda x: x["next_due"])
    c["_subscribers_list"] = subs

    # Free-access list for non-insider services
    c["_free_access_list"] = [{"id": fid, "name": get_discord_username(fid) or f"User #{fid[-4:]}"}
                              for fid in c.get("free_access", [])]

    # Members with names + salary (for payroll UI)
    emps = c.get("employees", {})
    c["_members_list"] = [{"id": mid, "name": get_discord_username(mid) or f"User #{mid[-4:]}",
                           "salary": emps.get(mid, 0), "is_ceo": mid == c.get("ceo")}
                          for mid in c.get("members", {})]
    c["_my_salary"] = emps.get(uid, 0) if uid else 0

    return jsonify(c)


@app.route("/api/companies/create", methods=["POST"])
def api_company_create():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    body = request.json
    name = body.get("name", "").strip()[:40]
    ticker = body.get("ticker", "").strip().upper()[:4]
    ctype = body.get("type", "")
    desc = body.get("description", "").strip()[:200]
    sub_price = float(body.get("sub_price", 0) or 0)
    if not name or not ticker or ctype not in COMPANY_TYPES:
        return jsonify({"error": "invalid fields"}), 400
    if not ticker.isalpha():
        return jsonify({"error": "ticker must be letters only"}), 400
    data = load_data()
    u = get_user(data, session["user_id"])
    if u["balance"] < COMPANY_COST:
        return jsonify({"error": f"Need ${COMPANY_COST:,.0f} to found a company"}), 400
    companies = load_companies()
    if any(c["ticker"] == ticker for c in companies.values()):
        return jsonify({"error": "ticker already taken"}), 400
    u["balance"] = round(u["balance"] - COMPANY_COST, 2)
    save_data(data)
    company = create_company(session["user_id"], name, ticker, ctype, desc, sub_price=sub_price)
    companies[company["id"]] = company
    save_companies(companies)
    return jsonify({"ok": True, "company": company})


@app.route("/api/companies/<cid>/join", methods=["POST"])
def api_company_join(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c:
        return jsonify({"error": "not found"}), 404
    uid = session["user_id"]
    if uid in c.get("members", {}):
        return jsonify({"error": "already a member"}), 400
    c.setdefault("members", {})[uid] = {"role": "member", "deposit": 0, "joined_at": int(time.time())}
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/subscribe", methods=["POST"])
def api_company_subscribe(cid):
    """Subscribe to an Insider Ring's news feed (pays hourly)."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or c["type"] not in SUB_TYPES:
        return jsonify({"error": "not a subscription company"}), 400
    uid = session["user_id"]
    if uid in c.get("subscribers", {}):
        return jsonify({"error": "already subscribed"}), 400
    sub_price = c.get("sub_price", 0)
    # Charge the first hour up front
    data = load_data()
    u = get_user(data, uid)
    if u["balance"] < sub_price:
        return jsonify({"error": f"Need {fmt(sub_price)} for the first hour"}), 400
    u["balance"] = round(u["balance"] - sub_price, 2)
    c["treasury"] = round(c["treasury"] + sub_price, 2)
    now = int(time.time())
    c.setdefault("subscribers", {})[uid] = {"since": now, "next_due": now + 3600}
    log_transaction(data, uid, "send", f"Subscribed to {c['ticker']} insider ring", -sub_price)
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/unsubscribe", methods=["POST"])
def api_company_unsubscribe(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    uid = session["user_id"]
    if not c or uid not in c.get("subscribers", {}):
        return jsonify({"error": "not subscribed"}), 400
    del c["subscribers"][uid]
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/set_sub_price", methods=["POST"])
def api_company_set_sub_price(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]) or c["type"] not in SUB_TYPES:
        return jsonify({"error": "CEO of a subscription company only"}), 403
    c["sub_price"] = round(float(request.json.get("sub_price", 0)), 2)
    save_companies(companies)
    return jsonify({"ok": True, "sub_price": c["sub_price"]})


@app.route("/api/companies/<cid>/distribute", methods=["POST"])
def api_company_distribute(cid):
    """Hedge Fund CEO splits an amount of treasury among members by deposit share."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]):
        return jsonify({"error": "CEO only"}), 403
    amount = float(request.json.get("amount", 0))
    if amount <= 0 or amount > c["treasury"]:
        return jsonify({"error": "invalid amount"}), 400
    members = c.get("members", {})
    total_deposit = sum(m.get("deposit", 0) for m in members.values())
    data = load_data()
    if total_deposit <= 0:
        # Split equally if nobody has deposits
        share = round(amount / max(1, len(members)), 2)
        for uid in members:
            u = get_user(data, uid)
            u["balance"] = round(u["balance"] + share, 2)
            log_transaction(data, uid, "receive", f"{c['ticker']} profit share", share)
    else:
        for uid, m in members.items():
            portion = round(amount * (m.get("deposit", 0) / total_deposit), 2)
            if portion > 0:
                u = get_user(data, uid)
                u["balance"] = round(u["balance"] + portion, 2)
                log_transaction(data, uid, "receive", f"{c['ticker']} profit share", portion)
    c["treasury"] = round(c["treasury"] - amount, 2)
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True, "treasury": c["treasury"]})


@app.route("/api/companies/<cid>/post_news", methods=["POST"])
def api_company_post_news(cid):
    """CEO posts a public announcement to the market news feed (once per hour)."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]):
        return jsonify({"error": "CEO only"}), 403
    now = int(time.time())
    last = c.get("last_news_post", 0)
    if now - last < 3600:
        remain = (3600 - (now - last)) // 60 + 1
        return jsonify({"error": f"You can post again in {remain} min"}), 429
    headline = request.json.get("headline", "").strip()[:140]
    if not headline:
        return jsonify({"error": "empty headline"}), 400
    positive = bool(request.json.get("positive", True))
    c["last_news_post"] = now
    save_companies(companies)
    data = load_data()
    events = data.get("news_feed", [])
    events.append({
        "headline": f"📢 {c['name']} ({c['ticker']}): {headline}",
        "positive": positive, "impact": 0, "ts": now, "public_at": now,
        "kind": "announcement",
    })
    data["news_feed"] = events[-50:]
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/grant_free", methods=["POST"])
def api_company_grant_free(cid):
    """CEO grants a user free access to the company's service (e.g. insider ring)."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]):
        return jsonify({"error": "CEO only"}), 403
    target = str(request.json.get("user_id", "")).strip()
    if not target:
        return jsonify({"error": "enter a user ID"}), 400
    now = int(time.time())
    if c["type"] in SUB_TYPES:
        c.setdefault("subscribers", {})[target] = {"since": now, "next_due": now + 10**12, "free": True}
    else:
        c.setdefault("free_access", [])
        if target not in c["free_access"]:
            c["free_access"].append(target)
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/revoke_free", methods=["POST"])
def api_company_revoke_free(cid):
    """CEO revokes a user's free access."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]):
        return jsonify({"error": "CEO only"}), 403
    target = str(request.json.get("user_id", "")).strip()
    if c["type"] in SUB_TYPES:
        sub = c.get("subscribers", {}).get(target)
        if sub and sub.get("free"):
            del c["subscribers"][target]
    else:
        if target in c.get("free_access", []):
            c["free_access"].remove(target)
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/copy_toggle", methods=["POST"])
def api_company_copy_toggle(cid):
    """Subscriber toggles auto-copy on/off for a copy-trading company."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    uid = session["user_id"]
    if not c or c["type"] != "copy_trading" or uid not in c.get("subscribers", {}):
        return jsonify({"error": "subscribe first"}), 400
    sub = c["subscribers"][uid]
    sub["copy"] = not sub.get("copy", False)
    save_companies(companies)
    return jsonify({"ok": True, "copy": sub["copy"]})


@app.route("/api/companies/<cid>/set_rating", methods=["POST"])
def api_company_set_rating(cid):
    """Analyst Firm CEO sets the Buy/Hold/Sell/Offline rating."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]) or c["type"] != "analyst_firm":
        return jsonify({"error": "CEO of analyst firm only"}), 403
    rating = request.json.get("rating", "")
    if rating not in ("Offline", "Buy", "Hold", "Sell"):
        return jsonify({"error": "invalid rating"}), 400
    c["rating"] = rating
    save_companies(companies)
    return jsonify({"ok": True, "rating": rating})


# Upkeep cost per cycle (20 min) for each early-access level above 0.
EARLY_UPKEEP_PER_LEVEL = 1500


@app.route("/api/companies/<cid>/set_early", methods=["POST"])
def api_company_set_early(cid):
    """Insider Ring CEO sets the early-access level (0-3). Higher = more lead time but pricier upkeep."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]) or c["type"] != "insider_ring":
        return jsonify({"error": "CEO of insider ring only"}), 403
    try:
        level = int(request.json.get("level", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid level"}), 400
    if level < 0 or level > 3:
        return jsonify({"error": "level must be 0-3"}), 400
    c["early"] = level
    save_companies(companies)
    return jsonify({"ok": True, "early": level})


def _mkt_level(c):
    m = c.get("upgrades", {}).get("marketing", 0)
    return 1 if m is True else int(m or 0)  # migrate legacy bool


@app.route("/api/companies/<cid>/buy_upgrade", methods=["POST"])
def api_company_buy_upgrade(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]):
        return jsonify({"error": "CEO only"}), 403
    now_u = int(time.time())
    if now_u - c.get("last_upgrade", 0) < 10:
        return jsonify({"error": f"Slow down — wait {10 - (now_u - c.get('last_upgrade', 0))}s"}), 429
    up = request.json.get("upgrade", "")
    ups = c.setdefault("upgrades", {"vault": 0, "marketing": 0})
    if up == "vault":
        lvl = ups.get("vault", 0)
        if lvl >= 5:
            return jsonify({"error": "max level"}), 400
        cost = 10000 * (lvl + 1)
        if c["treasury"] < cost:
            return jsonify({"error": f"Treasury needs {fmt(cost)}"}), 400
        c["treasury"] = round(c["treasury"] - cost, 2)
        ups["vault"] = lvl + 1
    elif up == "marketing":
        lvl = _mkt_level(c)
        if lvl >= 5:
            return jsonify({"error": "max level"}), 400
        cost = 15000 * (lvl + 1)
        if c["treasury"] < cost:
            return jsonify({"error": f"Treasury needs {fmt(cost)}"}), 400
        c["treasury"] = round(c["treasury"] - cost, 2)
        ups["marketing"] = lvl + 1
    else:
        return jsonify({"error": "unknown upgrade"}), 400
    c["last_upgrade"] = now_u
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/set_ad", methods=["POST"])
def api_company_set_ad(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]):
        return jsonify({"error": "CEO only"}), 403
    c["ad_text"] = request.json.get("ad_text", "").strip()[:140]
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/set_description", methods=["POST"])
def api_company_set_description(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]):
        return jsonify({"error": "CEO only"}), 403
    c["description"] = request.json.get("description", "").strip()[:200]
    save_companies(companies)
    return jsonify({"ok": True, "description": c["description"]})


@app.route("/api/companies/<cid>/deposit", methods=["POST"])
def api_company_deposit(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    amount = float(request.json.get("amount", 0))
    if amount <= 0:
        return jsonify({"error": "invalid amount"}), 400
    companies = load_companies()
    c = companies.get(cid)
    if not c or session["user_id"] not in c.get("members", {}):
        return jsonify({"error": "not a member"}), 403
    data = load_data()
    u = get_user(data, session["user_id"])
    if u["balance"] < amount:
        return jsonify({"error": "not enough cash"}), 400
    u["balance"] = round(u["balance"] - amount, 2)
    c["treasury"] = round(c["treasury"] + amount, 2)
    c["members"][session["user_id"]]["deposit"] = round(c["members"][session["user_id"]].get("deposit", 0) + amount, 2)
    # In a savings bank, the CEO's deposits are the bank's own capital — not a
    # customer deposit, so they don't count as "owed" and don't earn interest.
    if c["type"] == "savings" and not is_ceo(c, session["user_id"]):
        c.setdefault("deposits", {})[session["user_id"]] = round(c["deposits"].get(session["user_id"], 0) + amount, 2)
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True, "treasury": c["treasury"]})


@app.route("/api/companies/<cid>/withdraw", methods=["POST"])
def api_company_withdraw(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    amount = float(request.json.get("amount", 0))
    uid = session["user_id"]
    companies = load_companies()
    c = companies.get(cid)
    if not c or uid not in c.get("members", {}):
        return jsonify({"error": "not a member"}), 403
    member = c["members"][uid]
    max_withdraw = member.get("deposit", 0) if not is_ceo(c, uid) else c["treasury"]
    if amount > max_withdraw or amount > c["treasury"]:
        return jsonify({"error": f"can only withdraw up to ${max_withdraw:.2f}"}), 400
    data = load_data()
    u = get_user(data, uid)
    c["treasury"] = round(c["treasury"] - amount, 2)
    member["deposit"] = round(max(0, member.get("deposit", 0) - amount), 2)
    u["balance"] = round(u["balance"] + amount, 2)
    if c["type"] == "savings" and not is_ceo(c, uid):
        c.setdefault("deposits", {})[uid] = round(max(0, c["deposits"].get(uid, 0) - amount), 2)
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/buy_stock", methods=["POST"])
def api_company_buy_stock(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    shares = int(request.json.get("shares", 0))
    if shares <= 0:
        return jsonify({"error": "invalid shares"}), 400
    data = load_data()
    sus_price = data["stock_price"]
    companies = load_companies()
    c = companies.get(cid)
    if not c:
        return jsonify({"error": "not found"}), 404
    price = company_stock_price(c, sus_price)
    cost = round(price * shares, 2)
    uid = session["user_id"]
    u = get_user(data, uid)
    if u["balance"] < cost:
        return jsonify({"error": f"Need {fmt(cost)}"}), 400
    # Investment Bank earns commission on trades
    for oc in companies.values():
        if oc["type"] == "invest_bank" and len(oc.get("members", {})) > 0:
            commission = round(cost * 0.03, 2)
            oc["treasury"] = round(oc["treasury"] + commission, 2)
    u["balance"] = round(u["balance"] - cost, 2)
    c["treasury"] = round(c["treasury"] + cost, 2)
    c.setdefault("shareholders", {})[uid] = c["shareholders"].get(uid, 0) + shares
    c["shares_issued"] = c.get("shares_issued", SHARES_ISSUED) + shares
    new_price = company_stock_price(c, sus_price)
    c["stock_price"] = new_price
    c.setdefault("stock_history", []).append(new_price)
    log_transaction(data, uid, "company_buy", f"Bought {shares} {c['ticker']} @ {fmt(price)}", -cost)
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True, "shares": shares, "cost": cost, "new_price": new_price})


@app.route("/api/companies/<cid>/sell_stock", methods=["POST"])
def api_company_sell_stock(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    shares = int(request.json.get("shares", 0))
    if shares <= 0:
        return jsonify({"error": "invalid shares"}), 400
    data = load_data()
    sus_price = data["stock_price"]
    companies = load_companies()
    c = companies.get(cid)
    if not c:
        return jsonify({"error": "not found"}), 404
    uid = session["user_id"]
    owned = c.get("shareholders", {}).get(uid, 0)
    if owned < shares:
        return jsonify({"error": f"only own {owned} shares"}), 400
    price = company_stock_price(c, sus_price)
    earnings = round(price * shares, 2)
    if c["treasury"] < earnings:
        return jsonify({"error": "company treasury too low to buy back"}), 400
    # Investment Bank commission
    for oc in companies.values():
        if oc["type"] == "invest_bank" and len(oc.get("members", {})) > 0:
            commission = round(earnings * 0.03, 2)
            oc["treasury"] = round(oc["treasury"] + commission, 2)
    data = load_data()
    u = get_user(data, uid)
    c["treasury"] = round(c["treasury"] - earnings, 2)
    c["shareholders"][uid] = owned - shares
    c["shares_issued"] = max(1, c.get("shares_issued", SHARES_ISSUED) - shares)
    u["balance"] = round(u["balance"] + earnings, 2)
    new_price = company_stock_price(c, sus_price)
    c["stock_price"] = new_price
    c.setdefault("stock_history", []).append(new_price)
    log_transaction(data, uid, "company_sell", f"Sold {shares} {c['ticker']} @ {fmt(price)}", earnings)
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True, "earnings": earnings})


@app.route("/api/companies/<cid>/trade_sus", methods=["POST"])
def api_company_trade_sus(cid):
    """CEO-only: buy or sell SUS stock using company treasury."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    action = request.json.get("action")
    shares = int(request.json.get("shares", 0))
    if shares <= 0 or action not in ("buy", "sell"):
        return jsonify({"error": "invalid"}), 400
    companies = load_companies()
    c = companies.get(cid)
    if not c or not is_ceo(c, session["user_id"]):
        return jsonify({"error": "CEO only"}), 403
    data = load_data()
    price = data["stock_price"]
    if action == "buy":
        cost = round(price * shares, 2)
        if c["treasury"] < cost:
            return jsonify({"error": "not enough treasury"}), 400
        c["treasury"] = round(c["treasury"] - cost, 2)
        c["sus_shares"] = c.get("sus_shares", 0) + shares
    else:
        if c.get("sus_shares", 0) < shares:
            return jsonify({"error": "not enough SUS shares"}), 400
        c["sus_shares"] -= shares
        c["treasury"] = round(c["treasury"] + price * shares, 2)
    save_companies(companies)
    return jsonify({"ok": True, "treasury": c["treasury"], "sus_shares": c["sus_shares"]})


@app.route("/api/companies/<cid>/vote", methods=["POST"])
def api_company_vote(cid):
    """Cast or create a vote (day_trading, pump_dump, wolf_pack)."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    uid = session["user_id"]
    if not c or uid not in c.get("members", {}):
        return jsonify({"error": "not a member"}), 403
    vote_type = request.json.get("vote")  # "buy" | "sell" | "hold"
    vote = c.get("vote") or {"buy": [], "sell": [], "hold": [], "expires": int(time.time()) + 3600}
    for v in ["buy", "sell", "hold"]:
        if uid in vote.get(v, []):
            vote[v].remove(uid)
    vote.setdefault(vote_type, []).append(uid)
    c["vote"] = vote
    save_companies(companies)
    return jsonify({"ok": True, "vote": vote})


@app.route("/api/companies/<cid>/loan", methods=["POST"])
def api_company_loan(cid):
    """Request a loan from a Lending Bank."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    amount = float(request.json.get("amount", 0))
    if amount <= 0:
        return jsonify({"error": "invalid amount"}), 400
    companies = load_companies()
    c = companies.get(cid)
    if not c or c["type"] != "lending_bank":
        return jsonify({"error": "not a lending bank"}), 400
    uid = session["user_id"]
    if uid in c.get("loans", {}):
        return jsonify({"error": "already have a loan"}), 400
    if c["treasury"] < amount:
        return jsonify({"error": "bank insufficient funds"}), 400
    data = load_data()
    u = get_user(data, uid)
    rate = 0.04  # interest charged per cycle
    c["treasury"] = round(c["treasury"] - amount, 2)
    c.setdefault("loans", {})[uid] = {"amount": amount, "rate": rate, "due": round(amount * 1.08, 2)}
    u["balance"] = round(u["balance"] + amount, 2)
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True, "amount": amount, "due": c["loans"][uid]["due"]})


@app.route("/api/companies/<cid>/repay", methods=["POST"])
def api_company_repay(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    uid = session["user_id"]
    if not c or uid not in c.get("loans", {}):
        return jsonify({"error": "no loan found"}), 400
    loan = c["loans"][uid]
    data = load_data()
    u = get_user(data, uid)
    if u["balance"] < loan["due"]:
        return jsonify({"error": f"need {fmt(loan['due'])} to repay"}), 400
    u["balance"] = round(u["balance"] - loan["due"], 2)
    c["treasury"] = round(c["treasury"] + loan["due"], 2)
    del c["loans"][uid]
    new_credit = adjust_credit(u, 25)  # reward on-time repayment
    add_notification(data, uid, f"✅ Loan repaid — credit +25 (now {new_credit})", True)
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/insure", methods=["POST"])
def api_company_insure(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or c["type"] != "insurance":
        return jsonify({"error": "not an insurance company"}), 400
    uid = session["user_id"]
    premium = float(request.json.get("premium", 50))
    data = load_data()
    u = get_user(data, uid)
    if u["balance"] < premium:
        return jsonify({"error": "not enough cash"}), 400
    u["balance"] = round(u["balance"] - premium, 2)
    c["treasury"] = round(c["treasury"] + premium, 2)
    net_worth = u["balance"] + u.get("shares", 0) * data["stock_price"]
    c.setdefault("policies", {})[uid] = {"premium": premium, "coverage": round(net_worth * 0.5, 2), "snapshot": round(net_worth, 2)}
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True, "coverage": c["policies"][uid]["coverage"]})


@app.route("/api/companies/<cid>/bounty", methods=["POST"])
def api_company_bounty(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or c["type"] != "bounty_hunter":
        return jsonify({"error": "not a bounty hunter"}), 400
    target_id = str(request.json.get("target_id", ""))
    amount = float(request.json.get("amount", 0))
    if not target_id or amount <= 0:
        return jsonify({"error": "invalid"}), 400
    data = load_data()
    u = get_user(data, session["user_id"])
    if u["balance"] < amount:
        return jsonify({"error": "not enough cash"}), 400
    u["balance"] = round(u["balance"] - amount, 2)
    c["treasury"] = round(c["treasury"] + amount * 0.3, 2)
    c.setdefault("bounties", []).append({
        "target": target_id, "amount": amount, "poster": session["user_id"],
        "expires": int(time.time()) + 86400
    })
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/set_spread", methods=["POST"])
def api_company_set_spread(cid):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    companies = load_companies()
    c = companies.get(cid)
    if not c or c["type"] != "market_maker" or not is_ceo(c, session["user_id"]):
        return jsonify({"error": "CEO of market maker only"}), 403
    c["spread_buy"] = float(request.json.get("buy", 0))
    c["spread_sell"] = float(request.json.get("sell", 0))
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/market_trade", methods=["POST"])
def api_company_market_trade(cid):
    """Trade with Market Maker at their spread price."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    action = request.json.get("action")
    shares = int(request.json.get("shares", 0))
    companies = load_companies()
    c = companies.get(cid)
    if not c or c["type"] != "market_maker":
        return jsonify({"error": "not a market maker"}), 400
    data = load_data()
    uid = session["user_id"]
    u = get_user(data, uid)
    if action == "buy":
        price = c.get("spread_sell", data["stock_price"] * 1.02)
        cost = round(price * shares, 2)
        if u["balance"] < cost:
            return jsonify({"error": "not enough cash"}), 400
        if c.get("sus_shares", 0) < shares:
            return jsonify({"error": "market maker has no shares"}), 400
        u["balance"] = round(u["balance"] - cost, 2)
        u["shares"] = u.get("shares", 0) + shares
        c["sus_shares"] -= shares
        c["treasury"] = round(c["treasury"] + cost, 2)
    else:
        price = c.get("spread_buy", data["stock_price"] * 0.98)
        earnings = round(price * shares, 2)
        if u.get("shares", 0) < shares:
            return jsonify({"error": "not enough SUS shares"}), 400
        if c["treasury"] < earnings:
            return jsonify({"error": "market maker low on cash"}), 400
        u["shares"] = u.get("shares", 0) - shares
        u["balance"] = round(u["balance"] + earnings, 2)
        c["sus_shares"] = c.get("sus_shares", 0) + shares
        c["treasury"] = round(c["treasury"] - earnings, 2)
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True})


@app.route("/api/companies/<cid>/pay_protection", methods=["POST"])
def api_company_pay_protection(cid):
    """Pay protection to Sus Mafia."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    amount = float(request.json.get("amount", 100))
    companies = load_companies()
    c = companies.get(cid)
    if not c or c["type"] != "sus_mafia":
        return jsonify({"error": "not the mafia"}), 400
    data = load_data()
    u = get_user(data, session["user_id"])
    if u["balance"] < amount:
        return jsonify({"error": "not enough cash"}), 400
    u["balance"] = round(u["balance"] - amount, 2)
    c["treasury"] = round(c["treasury"] + amount, 2)
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True})


CASINO_GAMES = {
    "coinflip": {"name": "Coin Flip", "emoji": "🪙", "chance": 0.48, "payout": 2.0,  "desc": "48% to double"},
    "highlow":  {"name": "High Card", "emoji": "🃏", "chance": 0.45, "payout": 2.1,  "desc": "45% to win 2.1×"},
    "dice":     {"name": "Dice Roll", "emoji": "🎲", "chance": 0.33, "payout": 2.8,  "desc": "33% to win 2.8×"},
    "roulette": {"name": "Roulette",  "emoji": "🎡", "chance": 0.25, "payout": 3.6,  "desc": "25% to win 3.6×"},
    "slots":    {"name": "Slots",     "emoji": "🎰", "chance": 0.10, "payout": 8.0,  "desc": "10% to win 8× jackpot"},
}


@app.route("/api/companies/<cid>/gamble", methods=["POST"])
def api_company_gamble(cid):
    """Gamble against a Casino's treasury across several games with different odds."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    bet = float(request.json.get("bet", 0))
    game_key = request.json.get("game", "coinflip")
    game = CASINO_GAMES.get(game_key)
    if not game:
        return jsonify({"error": "unknown game"}), 400
    if bet <= 0:
        return jsonify({"error": "invalid bet"}), 400
    companies = load_companies()
    c = companies.get(cid)
    if not c or c["type"] != "casino":
        return jsonify({"error": "not a casino"}), 400
    data = load_data()
    u = get_user(data, session["user_id"])
    now_g = int(time.time())
    last_g = u.get("last_gamble", 0)
    if now_g - last_g < 20:
        return jsonify({"error": f"Slow down — wait {20 - (now_g - last_g)}s between bets"}), 429
    if u["balance"] < bet:
        return jsonify({"error": "not enough cash"}), 400
    win_profit = round(bet * (game["payout"] - 1), 2)  # net gain on a win
    if c["treasury"] < win_profit:
        return jsonify({"error": "casino can't cover that bet"}), 400
    import random as _r
    win = _r.random() < game["chance"]
    if win:
        u["balance"] = round(u["balance"] + win_profit, 2)
        c["treasury"] = round(c["treasury"] - win_profit, 2)
        log_transaction(data, session["user_id"], "receive", f"🎰 Won {game['name']} at {c['ticker']}", win_profit)
    else:
        u["balance"] = round(u["balance"] - bet, 2)
        c["treasury"] = round(c["treasury"] + bet, 2)
        log_transaction(data, session["user_id"], "send", f"🎰 Lost {game['name']} at {c['ticker']}", -bet)
    u["last_gamble"] = now_g
    save_data(data)
    save_companies(companies)
    return jsonify({"ok": True, "win": win, "bet": bet, "profit": win_profit,
                    "game": game["name"], "balance": u["balance"]})


# ── Minecraft integration ───────────────────────────────────────────────────────

MC_API_KEY = os.environ.get("MC_API_KEY", "")

def _mc_auth():
    key = request.headers.get("X-API-Key") or request.args.get("key", "")
    return MC_API_KEY and key == MC_API_KEY

def mc_get_uid(data, uuid):
    link = data.get("mc_links", {}).get(uuid)
    return link["discord_id"] if link else None


@app.route("/api/mc/generate_code", methods=["POST"])
def mc_generate_code():
    """Website user generates a one-time code to enter in Minecraft."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    import random as _r, string as _s
    code = "".join(_r.choices(_s.ascii_uppercase + _s.digits, k=6))
    data = load_data()
    codes = data.setdefault("mc_link_codes", {})
    for k in list(codes):
        if codes[k]["discord_id"] == session["user_id"] or time.time() > codes[k]["expires"]:
            del codes[k]
    codes[code] = {"discord_id": session["user_id"], "expires": int(time.time()) + 600}
    save_data(data)
    return jsonify({"ok": True, "code": code})


@app.route("/api/mc/link", methods=["POST"])
def mc_link():
    """Called by the Minecraft plugin to link a player's UUID via a code."""
    if not _mc_auth():
        return jsonify({"error": "bad api key"}), 403
    body = request.json or {}
    code = str(body.get("code", "")).strip().upper()
    uuid = str(body.get("uuid", ""))
    username = str(body.get("username", ""))
    if not code or not uuid:
        return jsonify({"error": "missing code or uuid"}), 400
    data = load_data()
    codes = data.get("mc_link_codes", {})
    entry = codes.get(code)
    if not entry or time.time() > entry["expires"]:
        return jsonify({"error": "invalid or expired code"}), 400
    discord_id = entry["discord_id"]
    data.setdefault("mc_links", {})[uuid] = {"discord_id": discord_id, "username": username}
    del codes[code]
    data["mc_link_codes"] = codes
    save_data(data)
    return jsonify({"ok": True, "discord_name": get_discord_username(discord_id) or "your account"})


@app.route("/api/mc/balance")
def mc_balance():
    if not _mc_auth():
        return jsonify({"error": "bad api key"}), 403
    data = load_data()
    uid = mc_get_uid(data, request.args.get("uuid", ""))
    if not uid:
        return jsonify({"error": "not linked"}), 404
    u = get_user(data, uid)
    save_data(data)
    return jsonify({"balance": u["balance"], "discord_name": get_discord_username(uid)})


@app.route("/api/mc/add", methods=["POST"])
def mc_add():
    """Add (or remove, if negative) SUS cash for a linked player."""
    if not _mc_auth():
        return jsonify({"error": "bad api key"}), 403
    body = request.json or {}
    uuid = str(body.get("uuid", ""))
    amount = float(body.get("amount", 0))
    reason = str(body.get("reason", "Minecraft"))[:60]
    data = load_data()
    uid = mc_get_uid(data, uuid)
    if not uid:
        return jsonify({"error": "not linked"}), 404
    u = get_user(data, uid)
    u["balance"] = round(max(0, u["balance"] + amount), 2)
    log_transaction(data, uid, "receive" if amount >= 0 else "send", f"⛏️ {reason}", amount)
    save_data(data)
    return jsonify({"ok": True, "balance": u["balance"]})


# In-game store items → console commands the plugin runs (%player% is replaced)
MC_STORE = {
    "diamonds":  {"name": "💎 32 Diamonds",       "cost": 6000,  "cmds": ["give %player% diamond 32"]},
    "gapples":   {"name": "🍎 16 Golden Apples",   "cost": 1000,  "cmds": ["give %player% golden_apple 16"]},
    "xp":        {"name": "✨ 30 XP Levels",       "cost": 2000,  "cmds": ["xp add %player% 30 levels"]},
    "xpbottles": {"name": "🍾 64 XP Bottles",      "cost": 5000,  "cmds": ["@susitem:EXPERIENCE_BOTTLE:64"]},
    "shulker":   {"name": "📦 Shulker Box",        "cost": 10000, "cmds": ["@susitem:SHULKER_BOX:1"]},
    "totem":     {"name": "🪬 Totem of Undying",   "cost": 4000,  "cmds": ["@susitem:TOTEM_OF_UNDYING:1"]},
    "elytra":    {"name": "🪽 Elytra",             "cost": 25000, "cmds": ["@susitem:ELYTRA:1"]},
    "netherite": {"name": "🪓 Netherite Ingot",    "cost": 40000, "cmds": ["@susitem:NETHERITE_INGOT:1"]},
}

def mc_uuid_for(data, discord_id):
    for uuid, link in data.get("mc_links", {}).items():
        if link.get("discord_id") == discord_id:
            return uuid
    return None


# Enchantments players can buy (applied to the item they're holding). Keys are vanilla.
ENCHANTS = {
    "sharpness":       {"name": "⚔️ Sharpness +1",       "key": "sharpness",       "level": 1, "cost": 8000},
    "protection":      {"name": "🛡️ Protection +1",     "key": "protection",      "level": 1, "cost": 8000},
    "efficiency":      {"name": "⛏️ Efficiency +1",       "key": "efficiency",      "level": 1, "cost": 6000},
    "unbreaking":      {"name": "🔧 Unbreaking +1",     "key": "unbreaking",      "level": 1, "cost": 5000},
    "mending":         {"name": "💚 Mending",            "key": "mending",         "level": 1, "cost": 15000},
    "fortune":         {"name": "💎 Fortune +1",        "key": "fortune",         "level": 1, "cost": 10000},
    "looting":         {"name": "🍖 Looting +1",        "key": "looting",         "level": 1, "cost": 8000},
    "power":           {"name": "🏹 Power +1",            "key": "power",           "level": 1, "cost": 7000},
    "feather_falling": {"name": "🪶 Feather Falling +1", "key": "feather_falling", "level": 1, "cost": 5000},
}


@app.route("/api/store/enchants")
def api_store_enchants():
    return jsonify([{"key": k, "name": v["name"], "cost": v["cost"]} for k, v in ENCHANTS.items()])


@app.route("/api/store/buy_enchant", methods=["POST"])
def api_store_buy_enchant():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    e = ENCHANTS.get(request.json.get("enchant", ""))
    if not e:
        return jsonify({"error": "unknown enchant"}), 400
    data = load_data()
    uuid = mc_uuid_for(data, session["user_id"])
    if not uuid:
        return jsonify({"error": "Link your Minecraft account first"}), 400
    if not charge(data, session["user_id"], e["cost"]):
        return jsonify({"error": f"Need {fmt(e['cost'])}"}), 400
    data.setdefault("mc_pending_ench", {}).setdefault(uuid, []).append(f"{e['key']}:{e['level']}")
    log_transaction(data, session["user_id"], "send", f"✨ Bought {e['name']}", -e["cost"])
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/mc/my_enchants")
def api_mc_my_enchants():
    """Plugin reads a player's owned (un-applied) enchants WITHOUT clearing them."""
    if not _mc_auth():
        return jsonify({"error": "bad api key"}), 403
    uuid = request.args.get("uuid", "")
    data = load_data()
    return jsonify({"enchants": data.get("mc_pending_ench", {}).get(uuid, [])})


@app.route("/api/mc/use_enchant", methods=["POST"])
def api_mc_use_enchant():
    """Consume one enchant token when the player applies it via the menu."""
    if not _mc_auth():
        return jsonify({"error": "bad api key"}), 403
    body = request.json or {}
    uuid = str(body.get("uuid", ""))
    token = str(body.get("token", ""))
    data = load_data()
    lst = data.get("mc_pending_ench", {}).get(uuid, [])
    if token not in lst:
        return jsonify({"error": "not owned"}), 400
    lst.remove(token)
    data["mc_pending_ench"][uuid] = lst
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/store/mc_items")
def api_store_mc_items():
    return jsonify([{"key": k, "name": v["name"], "cost": v["cost"]} for k, v in MC_STORE.items()])


@app.route("/api/store/mc_buy", methods=["POST"])
def api_store_mc_buy():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    item = MC_STORE.get(request.json.get("item", ""))
    if not item:
        return jsonify({"error": "unknown item"}), 400
    data = load_data()
    uuid = mc_uuid_for(data, session["user_id"])
    if not uuid:
        return jsonify({"error": "Link your Minecraft account first"}), 400
    if not charge(data, session["user_id"], item["cost"]):
        return jsonify({"error": f"Need {fmt(item['cost'])}"}), 400
    pending = data.setdefault("mc_pending", {}).setdefault(uuid, [])
    for c in item["cmds"]:
        pending.append(c)
    log_transaction(data, session["user_id"], "send", f"⛏️ Bought {item['name']}", -item["cost"])
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/store/mc_checkout", methods=["POST"])
def api_store_mc_checkout():
    """Buy a cart of in-game items in one go."""
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    cart = request.json.get("cart", {})  # {item_key: qty}
    if not cart:
        return jsonify({"error": "cart is empty"}), 400
    data = load_data()
    uuid = mc_uuid_for(data, session["user_id"])
    if not uuid:
        return jsonify({"error": "Link your Minecraft account first"}), 400
    total = 0
    cmds = []
    for key, qty in cart.items():
        item = MC_STORE.get(key)
        try:
            qty = int(qty)
        except Exception:
            qty = 0
        if not item or qty < 1:
            continue
        total += item["cost"] * qty
        for _ in range(qty):
            cmds.extend(item["cmds"])
    if not cmds:
        return jsonify({"error": "cart is empty"}), 400
    u = get_user(data, session["user_id"])
    if u["balance"] < total:
        return jsonify({"error": f"Need {fmt(total)}, you have {fmt(u['balance'])}"}), 400
    u["balance"] = round(u["balance"] - total, 2)
    pending = data.setdefault("mc_pending", {}).setdefault(uuid, [])
    pending.extend(cmds)
    log_transaction(data, session["user_id"], "send", f"⛏️ Store checkout ({len(cmds)} item(s))", -total)
    save_data(data)
    return jsonify({"ok": True, "total": total, "balance": u["balance"]})


@app.route("/api/mc/pending")
def api_mc_pending():
    """Plugin claims a linked player's pending in-game rewards (and clears them)."""
    if not _mc_auth():
        return jsonify({"error": "bad api key"}), 403
    uuid = request.args.get("uuid", "")
    data = load_data()
    cmds = data.get("mc_pending", {}).get(uuid, [])
    if cmds:
        data["mc_pending"][uuid] = []
        save_data(data)
    return jsonify({"commands": cmds})


DEFAULT_UNLOCKS = {
    "nether": {"name": "The Nether", "emoji": "🔥", "goal": 100000},
    "end":    {"name": "The End",    "emoji": "🐉", "goal": 10000000},
}

def get_unlocks(data):
    u = data.setdefault("unlocks", {})
    for k in DEFAULT_UNLOCKS:
        if k not in u:
            u[k] = {"pooled": 0, "unlocked": False, "contributors": {}}
    return u


@app.route("/api/unlocks")
def api_unlocks():
    data = load_data()
    u = get_unlocks(data)
    save_data(data)
    uid = session.get("user_id")
    out = []
    for k, info in DEFAULT_UNLOCKS.items():
        st = u[k]
        out.append({"key": k, "name": info["name"], "emoji": info["emoji"], "goal": info["goal"],
                    "pooled": round(st["pooled"], 2), "unlocked": st["unlocked"],
                    "mine": round(st["contributors"].get(uid, 0), 2) if uid else 0})
    return jsonify(out)


@app.route("/api/unlocks/contribute", methods=["POST"])
def api_unlocks_contribute():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    key = request.json.get("key", "")
    amount = float(request.json.get("amount", 0))
    if key not in DEFAULT_UNLOCKS or amount <= 0:
        return jsonify({"error": "invalid"}), 400
    data = load_data()
    u = get_unlocks(data)
    st = u[key]
    if st["unlocked"]:
        return jsonify({"error": "already unlocked"}), 400
    user = get_user(data, session["user_id"])
    if user["balance"] < amount:
        return jsonify({"error": "not enough cash"}), 400
    # Don't overshoot the goal
    remaining = DEFAULT_UNLOCKS[key]["goal"] - st["pooled"]
    amount = round(min(amount, remaining), 2)
    user["balance"] = round(user["balance"] - amount, 2)
    st["pooled"] = round(st["pooled"] + amount, 2)
    st["contributors"][session["user_id"]] = round(st["contributors"].get(session["user_id"], 0) + amount, 2)
    log_transaction(data, session["user_id"], "send", f"🔓 Contributed to {DEFAULT_UNLOCKS[key]['name']}", -amount)
    newly = False
    if st["pooled"] >= DEFAULT_UNLOCKS[key]["goal"] and not st["unlocked"]:
        st["unlocked"] = True
        newly = True
        info = DEFAULT_UNLOCKS[key]
        events = data.get("news_feed", [])
        events.append({"headline": f"🔓 {info['emoji']} {info['name']} has been UNLOCKED server-wide!",
                       "positive": True, "impact": 0, "ts": int(time.time()),
                       "public_at": int(time.time()), "kind": "announcement"})
        data["news_feed"] = events[-50:]
    save_data(data)
    return jsonify({"ok": True, "pooled": st["pooled"], "unlocked": st["unlocked"], "newly": newly})


LOTTERY_TICKET_PRICE = 500


@app.route("/api/lottery")
def api_lottery():
    data = load_data()
    lot = data.get("lottery") or {"open": False, "ends_at": 0, "next_open": 0, "pot": 0, "tickets": {}}
    uid = session.get("user_id")
    return jsonify({
        "open": lot.get("open", False),
        "ends_at": lot.get("ends_at", 0),
        "next_open": lot.get("next_open", 0),
        "pot": round(lot.get("pot", 0), 2),
        "ticket_price": LOTTERY_TICKET_PRICE,
        "my_tickets": lot.get("tickets", {}).get(uid, 0) if uid else 0,
        "total_tickets": sum(lot.get("tickets", {}).values()),
    })


@app.route("/api/lottery/buy", methods=["POST"])
def api_lottery_buy():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    count = int(request.json.get("count", 1))
    if count < 1:
        return jsonify({"error": "invalid count"}), 400
    data = load_data()
    lot = data.get("lottery") or {}
    if not lot.get("open") or time.time() >= lot.get("ends_at", 0):
        return jsonify({"error": "lottery is closed right now"}), 400
    owned = lot.get("tickets", {}).get(session["user_id"], 0)
    if owned + count > 3:
        return jsonify({"error": f"Max 3 tickets per round (you have {owned})"}), 400
    cost = count * LOTTERY_TICKET_PRICE
    u = get_user(data, session["user_id"])
    if u["balance"] < cost:
        return jsonify({"error": f"Need {fmt(cost)} for {count} ticket(s)"}), 400
    u["balance"] = round(u["balance"] - cost, 2)
    lot["pot"] = round(lot.get("pot", 0) + cost, 2)
    lot.setdefault("tickets", {})[session["user_id"]] = lot["tickets"].get(session["user_id"], 0) + count
    log_transaction(data, session["user_id"], "send", f"🎟️ Bought {count} lottery ticket(s)", -cost)
    data["lottery"] = lot
    save_data(data)
    return jsonify({"ok": True, "pot": lot["pot"], "my_tickets": lot["tickets"][session["user_id"]]})


@app.route("/api/mc/unlocks")
def api_mc_unlocks():
    if not _mc_auth():
        return jsonify({"error": "bad api key"}), 403
    data = load_data()
    u = get_unlocks(data)
    return jsonify({k: u[k]["unlocked"] for k in DEFAULT_UNLOCKS})


@app.route("/api/mc/news")
def api_mc_news():
    """Recent market news for the plugin to broadcast (with public_at for insider timing)."""
    if not _mc_auth():
        return jsonify({"error": "bad api key"}), 403
    data = load_data()
    feed = data.get("news_feed", [])[-15:]
    def clean(s):
        # Minecraft chat can't render emojis — strip non-ASCII and tidy spaces
        return " ".join("".join(c for c in s if ord(c) < 128).split())
    out = [{"ts": n.get("ts", 0), "public_at": n.get("public_at", n.get("ts", 0)),
            "headline": clean(n.get("headline", "")), "positive": n.get("positive", True),
            "impact": n.get("impact", 0), "kind": n.get("kind", "")} for n in feed]
    return jsonify(out)


@app.route("/api/mc/insiders")
def api_mc_insiders():
    """UUIDs of linked players who currently have insider-ring access."""
    if not _mc_auth():
        return jsonify({"error": "bad api key"}), 403
    data = load_data()
    uuids = []
    for uuid, link in data.get("mc_links", {}).items():
        if user_in_insider_ring(link.get("discord_id")):
            uuids.append(uuid)
    return jsonify({"uuids": uuids})


@app.route("/api/mc/status")
def mc_status():
    if not _mc_auth():
        return jsonify({"error": "bad api key"}), 403
    data = load_data()
    uid = mc_get_uid(data, request.args.get("uuid", ""))
    if not uid:
        return jsonify({"error": "not linked"}), 404
    u = get_user(data, uid)
    price = data["stock_price"]
    nw = round(u["balance"] + u.get("shares", 0) * price, 2)
    return jsonify({
        "verified": u.get("verified", False),
        "credit": get_credit(u),
        "tier": credit_tier(get_credit(u))["name"],
        "net_worth": nw,
        "discord_name": get_discord_username(uid),
    })


# ── Store (Discord-automated perks) ─────────────────────────────────────────────

DISCORD_API = "https://discord.com/api/v10"
_guild_id_cache = None

def _dh():
    return {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

def get_guild_id():
    global _guild_id_cache
    if _guild_id_cache:
        return _guild_id_cache
    if not DISCORD_BOT_TOKEN:
        return None
    try:
        r = requests.get(f"{DISCORD_API}/users/@me/guilds", headers=_dh(), timeout=5)
        g = r.json()
        if isinstance(g, list) and g:
            _guild_id_cache = g[0]["id"]
            return _guild_id_cache
    except Exception:
        pass
    return None

def charge(data, uid, cost):
    u = get_user(data, uid)
    if u["balance"] < cost:
        return False
    u["balance"] = round(u["balance"] - cost, 2)
    return True


@app.route("/api/store/channels")
def api_store_channels():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    gid = get_guild_id()
    if not gid:
        return jsonify([])
    try:
        r = requests.get(f"{DISCORD_API}/guilds/{gid}/channels", headers=_dh(), timeout=5)
        chans = [{"id": c["id"], "name": c["name"]} for c in r.json() if c.get("type") == 0]
        return jsonify(chans)
    except Exception:
        return jsonify([])


@app.route("/api/store/role", methods=["POST"])
def api_store_role():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    name = request.json.get("name", "").strip()[:30]
    color = request.json.get("color", "#5865f2")
    if not name:
        return jsonify({"error": "enter a role name"}), 400
    try:
        color_int = int(str(color).lstrip("#"), 16)
    except Exception:
        color_int = 0
    gid = get_guild_id()
    if not gid:
        return jsonify({"error": "Discord unavailable"}), 500
    data = load_data()
    if not charge(data, session["user_id"], 4000):
        return jsonify({"error": "Need $4,000"}), 400
    try:
        rr = requests.post(f"{DISCORD_API}/guilds/{gid}/roles", headers=_dh(),
                           json={"name": name, "color": color_int, "mentionable": True}, timeout=8)
        if rr.status_code not in (200, 201):
            return jsonify({"error": "Couldn't create role — bot needs Manage Roles"}), 500
        role_id = rr.json()["id"]
        ar = requests.put(f"{DISCORD_API}/guilds/{gid}/members/{session['user_id']}/roles/{role_id}",
                          headers=_dh(), timeout=8)
        if ar.status_code not in (200, 204):
            return jsonify({"error": "Role made but couldn't assign (are you in the server?)"}), 500
    except Exception as e:
        return jsonify({"error": "Discord error"}), 500
    data.setdefault("store_roles", []).append(role_id)  # track for market reset
    log_transaction(data, session["user_id"], "send", f"Bought custom role '{name}'", -4000)
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/store/nickname", methods=["POST"])
def api_store_nickname():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    nick = request.json.get("nick", "").strip()[:32]
    if not nick:
        return jsonify({"error": "enter a nickname"}), 400
    gid = get_guild_id()
    if not gid:
        return jsonify({"error": "Discord unavailable"}), 500
    data = load_data()
    if not charge(data, session["user_id"], 5000):
        return jsonify({"error": "Need $5,000"}), 400
    try:
        r = requests.patch(f"{DISCORD_API}/guilds/{gid}/members/{session['user_id']}",
                           headers=_dh(), json={"nick": nick}, timeout=8)
        if r.status_code not in (200, 204):
            return jsonify({"error": "Couldn't set nickname — bot needs Manage Nicknames"}), 500
    except Exception:
        return jsonify({"error": "Discord error"}), 500
    log_transaction(data, session["user_id"], "send", f"Changed nickname to '{nick}'", -5000)
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/store/timeout", methods=["POST"])
def api_store_timeout():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    target = str(request.json.get("target_id", "")).strip()
    if not target:
        return jsonify({"error": "pick a target"}), 400
    gid = get_guild_id()
    if not gid:
        return jsonify({"error": "Discord unavailable"}), 500
    data = load_data()
    if not charge(data, session["user_id"], 4000):
        return jsonify({"error": "Need $4,000"}), 400
    import datetime as _dt
    until = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=10)).isoformat()
    try:
        r = requests.patch(f"{DISCORD_API}/guilds/{gid}/members/{target}", headers=_dh(),
                           json={"communication_disabled_until": until}, timeout=8)
        if r.status_code not in (200, 204):
            return jsonify({"error": "Couldn't timeout — bot needs Moderate Members & higher role"}), 500
    except Exception:
        return jsonify({"error": "Discord error"}), 500
    tname = get_discord_username(target) or "user"
    log_transaction(data, session["user_id"], "send", f"Timed out {tname} (10m)", -4000)
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/store/pin", methods=["POST"])
def api_store_pin():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    channel_id = str(request.json.get("channel_id", "")).strip()
    message = request.json.get("message", "").strip()[:300]
    if not channel_id or not message:
        return jsonify({"error": "pick a channel and message"}), 400
    data = load_data()
    if not charge(data, session["user_id"], 2000):
        return jsonify({"error": "Need $2,000"}), 400
    sender = session.get("username", "Someone")
    try:
        mr = requests.post(f"{DISCORD_API}/channels/{channel_id}/messages", headers=_dh(),
                           json={"content": f"📌 **{sender}:** {message}"}, timeout=8)
        if mr.status_code not in (200, 201):
            return jsonify({"error": "Couldn't post message"}), 500
        msg_id = mr.json()["id"]
        pr = requests.put(f"{DISCORD_API}/channels/{channel_id}/pins/{msg_id}", headers=_dh(), timeout=8)
        if pr.status_code not in (200, 204):
            return jsonify({"error": "Posted but couldn't pin — bot needs Manage Messages"}), 500
    except Exception:
        return jsonify({"error": "Discord error"}), 500
    log_transaction(data, session["user_id"], "send", "Pinned a message", -2000)
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/store/announce", methods=["POST"])
def api_store_announce():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    channel_id = str(request.json.get("channel_id", "")).strip()
    message = request.json.get("message", "").strip()[:300]
    if not channel_id or not message:
        return jsonify({"error": "pick a channel and message"}), 400
    data = load_data()
    if not charge(data, session["user_id"], 10000):
        return jsonify({"error": "Need $10,000"}), 400
    sender = session.get("username", "Someone")
    try:
        r = requests.post(f"{DISCORD_API}/channels/{channel_id}/messages", headers=_dh(),
                          json={"content": f"@everyone 📢 **{sender}** says: {message}",
                                "allowed_mentions": {"parse": ["everyone"]}}, timeout=8)
        if r.status_code not in (200, 201):
            return jsonify({"error": "Couldn't post — bot needs Send Messages & Mention Everyone"}), 500
    except Exception:
        return jsonify({"error": "Discord error"}), 500
    log_transaction(data, session["user_id"], "send", "Posted @everyone announcement", -10000)
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/store/news", methods=["POST"])
def api_store_news():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    headline = request.json.get("headline", "").strip()[:140]
    positive = bool(request.json.get("positive", True))
    if not headline:
        return jsonify({"error": "enter a headline"}), 400
    data = load_data()
    if not charge(data, session["user_id"], 5000):
        return jsonify({"error": "Need $5,000"}), 400
    sender = session.get("username", "Someone")
    now = int(time.time())
    events = data.get("news_feed", [])
    events.append({"headline": f"📢 {sender}: {headline}", "positive": positive,
                   "impact": 0, "ts": now, "public_at": now, "kind": "announcement"})
    data["news_feed"] = events[-50:]
    log_transaction(data, session["user_id"], "send", "Posted market news", -5000)
    save_data(data)
    return jsonify({"ok": True})


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/companies")
def companies_page():
    return render_template_string(COMPANIES_HTML)


@app.route("/guide")
def guide_page():
    return render_template_string(GUIDE_HTML)


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Sus Stock Market</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root {
    --bg: #1e1f22; --surface: #2b2d31; --surface2: #313338;
    --accent: #5865f2; --green: #57f287; --red: #ed4245;
    --text: #dbdee1; --muted: #949ba4; --border: #3a3c40;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; min-height: 100vh; }

  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 28px; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 20px; font-weight: 700; }
  .tag { background: var(--accent); color: #fff; font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 999px; }
  .live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  .auth-area { margin-left: auto; display: flex; align-items: center; gap: 10px; }
  .btn { padding: 7px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; border: none; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; }
  .btn-discord { background: #5865f2; color: #fff; }
  .btn-discord:hover { background: #4752c4; }
  .btn-logout { background: var(--surface2); color: var(--muted); font-size: 12px; padding: 5px 12px; }
  .btn-logout:hover { color: var(--text); }
  .btn-buy { background: #57f28722; color: var(--green); border: 1px solid #57f28740; }
  .btn-buy:hover { background: #57f28740; }
  .btn-sell { background: #ed424522; color: var(--red); border: 1px solid #ed424540; }
  .btn-sell:hover { background: #ed424540; }
  .avatar { width: 30px; height: 30px; border-radius: 50%; }

  .layout { display: grid; grid-template-columns: 260px 1fr 340px; gap: 20px; padding: 20px 28px; max-width: 1600px; margin: 0 auto; }
  .news-card { height: calc(100vh - 120px); overflow-y: auto; position: sticky; top: 20px; }
  @media(max-width:1100px){
    .layout{ grid-template-columns: 1fr 340px; }
    .news-col{ grid-column: 1 / -1; order: 5; }
    .news-card{ height: auto; max-height: 380px; position: static; }
  }
  @media(max-width:800px){
    .layout{ grid-template-columns:1fr; padding: 12px; }
  }

  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px 22px; margin-bottom: 16px; }
  .card-title { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); margin-bottom: 14px; }

  .price-hero { display: flex; align-items: baseline; gap: 10px; margin-bottom: 4px; }
  .price { font-size: 40px; font-weight: 800; letter-spacing: -1px; }
  .change-badge { font-size: 13px; font-weight: 700; padding: 3px 10px; border-radius: 6px; }
  .change-badge.up { background: #57f28722; color: var(--green); }
  .change-badge.down { background: #ed424522; color: var(--red); }
  .chart-wrap { position: relative; height: 240px; }

  .range-bar-wrap { margin-top: 12px; }
  .range-labels { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-bottom: 4px; }
  .range-bar { height: 6px; border-radius: 3px; background: var(--border); position: relative; }
  .range-fill { height: 100%; border-radius: 3px; background: linear-gradient(90deg, var(--red), var(--green)); }
  .range-marker { position: absolute; top: 50%; transform: translate(-50%,-50%); width: 12px; height: 12px; border-radius: 50%; background: #fff; border: 2px solid var(--accent); }

  .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 14px; }
  .stat { background: var(--surface2); border-radius: 8px; padding: 10px 12px; }
  .stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 3px; }
  .stat-value { font-size: 16px; font-weight: 700; }

  /* Portfolio card */
  .portfolio-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 14px; }
  .p-stat { background: var(--surface2); border-radius: 8px; padding: 10px 12px; }
  .p-stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 3px; }
  .p-stat-value { font-size: 15px; font-weight: 700; }
  .trade-row { display: flex; gap: 8px; margin-top: 4px; }
  .trade-input { background: var(--surface2); border: 1px solid var(--border); color: var(--text); border-radius: 8px; padding: 8px 12px; font-size: 14px; width: 100%; outline: none; }
  .trade-input:focus { border-color: var(--accent); }
  .trade-btns { display: flex; gap: 8px; margin-top: 8px; }

  .login-prompt { text-align: center; padding: 24px 0; color: var(--muted); font-size: 14px; }
  .login-prompt a { color: var(--accent); text-decoration: none; font-weight: 600; }

  /* Leaderboard */
  .lb-row { display: flex; align-items: center; gap: 10px; padding: 9px 0; border-bottom: 1px solid var(--border); }
  .lb-row:last-child { border-bottom: none; }
  .lb-rank { font-size: 16px; width: 26px; text-align: center; flex-shrink: 0; }
  .lb-name { font-weight: 600; font-size: 13px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lb-worth { font-weight: 700; font-size: 13px; color: var(--green); flex-shrink: 0; }
  .lb-pnl { font-size: 11px; }
  .lb-pnl.pos { color: var(--green); }
  .lb-pnl.neg { color: var(--red); }
  .lb-me { background: #5865f215; border-radius: 6px; padding: 0 6px; }

  .updated { font-size: 11px; color: var(--muted); margin-top: 8px; }
  .toast { position: fixed; bottom: 24px; right: 24px; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 18px; font-size: 13px; font-weight: 600; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 99; }
  .toast.show { opacity: 1; }
  .toast.ok { border-color: var(--green); color: var(--green); }
  .toast.err { border-color: var(--red); color: var(--red); }

  /* Zoom buttons */
  .zoom-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--muted); font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 6px; cursor: pointer; }
  .zoom-btn:hover { border-color: var(--accent); color: var(--text); }
  .zoom-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }

  /* Chat */
  .chat-msg { display: flex; gap: 8px; align-items: flex-start; }
  .chat-avatar { width: 28px; height: 28px; border-radius: 50%; flex-shrink: 0; background: var(--border); }
  .chat-bubble { background: var(--surface2); border-radius: 0 8px 8px 8px; padding: 6px 10px; max-width: 220px; }
  .chat-name { font-size: 11px; font-weight: 700; color: var(--accent); margin-bottom: 2px; }
  .chat-text { font-size: 13px; word-break: break-word; line-height: 1.4; }
  .chat-time { font-size: 10px; color: var(--muted); margin-top: 2px; }
  .chat-msg.me { flex-direction: row-reverse; }
  .chat-msg.me .chat-bubble { background: #5865f230; border-radius: 8px 0 8px 8px; }
  .chat-msg.me .chat-name { text-align: right; color: var(--green); }
  #chat-messages::-webkit-scrollbar { width: 4px; }
  #chat-messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  /* Nav buttons (header) */
  .nav-buttons { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .nav-buttons > button, .nav-buttons > a { margin-left: 0 !important; }
  .hamburger { display: none; margin-left: auto; background: var(--surface2); border: 1px solid var(--border); color: var(--text); font-size: 20px; line-height: 1; padding: 8px 12px; border-radius: 8px; cursor: pointer; }

  /* ── Mobile ─────────────────────────────────────────────── */
  @media(max-width:800px){
    header { flex-wrap: wrap; padding: 12px 16px; gap: 8px; }
    header h1 { font-size: 17px; }
    .hamburger { display: inline-flex; }
    .nav-buttons {
      display: none; order: 10; width: 100%;
      flex-direction: column; align-items: stretch; gap: 6px; margin-top: 8px;
    }
    .nav-buttons.open { display: flex; }
    .nav-buttons > button, .nav-buttons > a {
      width: 100%; justify-content: flex-start;
      padding: 13px 16px !important; font-size: 15px !important; border-radius: 10px;
    }
    .auth-area { width: 100%; order: 11; margin-left: 0; margin-top: 6px; }
    .auth-area .btn { width: 100%; justify-content: center; padding: 13px 16px; font-size: 15px; }
    .auth-area #acct-chip { width: 100%; justify-content: space-between; }

    /* Roomier tap targets + readable type on phones */
    .price { font-size: 34px; }
    .card { padding: 16px; }
    .chart-wrap { height: 220px; }
    .zoom-btn { padding: 7px 12px; font-size: 12px; }
    .trade-input { padding: 11px 14px; font-size: 16px; }  /* 16px stops iOS zoom-on-focus */
    .btn-buy, .btn-sell { padding: 12px 16px; font-size: 15px; }
    .btn { padding: 11px 16px; }
    #market-timer { padding: 10px 14px; }
    #timer-countdown { font-size: 22px; }
    /* Modals behave like full-width sheets */
    .modal-sheet { width: 96vw !important; max-height: 92vh !important; padding: 18px !important; }
  }
</style>
</head>
<body>

<header>
  <span style="font-size:22px">📈</span>
  <h1>Sus Stock Market</h1>
  <span class="tag">LIVE</span>
  <div class="live-dot"></div>
  <button class="hamburger" onclick="toggleNav()" aria-label="Menu">☰</button>
  <div class="nav-buttons" id="nav-buttons" onclick="if(window.innerWidth<=800)this.classList.remove('open')">
  <button id="linkmc-btn" onclick="openLinkMC()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:13px;font-weight:700;padding:5px 14px;border-radius:8px;cursor:pointer;margin-left:8px">🟩 Link MC</button>
  <a href="/guide" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:13px;font-weight:700;padding:5px 14px;border-radius:8px;cursor:pointer;margin-left:8px;text-decoration:none">📖 Guide</a>
  <button onclick="openUpdates()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:13px;font-weight:700;padding:5px 14px;border-radius:8px;cursor:pointer;margin-left:8px">🆕 Updates</button>
  <button onclick="openStore()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:13px;font-weight:700;padding:5px 14px;border-radius:8px;cursor:pointer;margin-left:8px">🛒 Store</button>
  <button onclick="openUnlocks()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:13px;font-weight:700;padding:5px 14px;border-radius:8px;cursor:pointer;margin-left:8px">🔓 Server Unlocks</button>
  <button onclick="openLottery()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:13px;font-weight:700;padding:5px 14px;border-radius:8px;cursor:pointer;margin-left:8px">🎟️ Lottery</button>
  <button onclick="toggleHistory()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:13px;font-weight:700;padding:5px 14px;border-radius:8px;cursor:pointer;margin-left:8px">📜 History</button>
  <button onclick="toggleCompanies()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:13px;font-weight:700;padding:5px 14px;border-radius:8px;cursor:pointer;margin-left:8px">🏢 Companies</button>
  </div>
  <div class="auth-area" id="auth-area">
    <a href="/login" class="btn btn-discord">
      <svg width="16" height="12" viewBox="0 0 71 55" fill="white"><path d="M60.1 4.9A58.6 58.6 0 0 0 45.6.4a.2.2 0 0 0-.2.1 40.8 40.8 0 0 0-1.8 3.7 54.1 54.1 0 0 0-16.2 0 37.6 37.6 0 0 0-1.8-3.7.22.22 0 0 0-.2-.1A58.4 58.4 0 0 0 10.9 4.9a.2.2 0 0 0-.1.1C1.6 18.1-.9 31 .3 43.7a.24.24 0 0 0 .1.2 58.9 58.9 0 0 0 17.7 8.9.22.22 0 0 0 .2-.1 42 42 0 0 0 3.6-5.9.21.21 0 0 0-.1-.3 38.7 38.7 0 0 1-5.5-2.6.22.22 0 0 1 0-.4c.4-.3.7-.5 1.1-.8a.21.21 0 0 1 .2 0c11.5 5.3 24 5.3 35.4 0a.21.21 0 0 1 .2 0l1.1.8a.22.22 0 0 1 0 .4 36.3 36.3 0 0 1-5.5 2.6.22.22 0 0 0-.1.3 47.1 47.1 0 0 0 3.6 5.9.21.21 0 0 0 .2.1 58.7 58.7 0 0 0 17.7-8.9.23.23 0 0 0 .1-.2c1.5-15.1-2.4-28-10.4-39.5a.18.18 0 0 0-.1-.2zM23.7 36c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.2 6.4-7.2c3.6 0 6.5 3.3 6.4 7.2 0 4-2.8 7.2-6.4 7.2zm23.6 0c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.2 6.4-7.2c3.6 0 6.5 3.3 6.4 7.2 0 4-2.8 7.2-6.4 7.2z"/></svg>
      Login with Discord
    </a>
  </div>
</header>

<div id="market-timer" style="text-align:center;padding:12px 20px;background:var(--surface);border-bottom:1px solid var(--border);font-weight:800;letter-spacing:.5px">
  <span id="timer-status" style="font-size:14px">—</span>
  <span id="timer-countdown" style="font-size:26px;margin-left:10px;font-variant-numeric:tabular-nums">--:--:--</span>
</div>

<div class="layout">
  <!-- News column -->
  <div class="news-col">
    <div class="card news-card">
      <div class="card-title" id="news-title">📰 Market News</div>
      <div id="news-feed"><div style="color:var(--muted);font-size:13px">No events yet — check back soon.</div></div>
    </div>
  </div>

  <!-- Center column -->
  <div>
    <!-- News ticker -->
    <div id="news-ticker" style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px 14px;margin-bottom:12px;font-size:12px;color:var(--text);overflow:hidden;white-space:nowrap;text-overflow:ellipsis;display:none">
      📰 Loading news...
    </div>

    <div class="card">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <div class="card-title" style="margin-bottom:0">SUS / USD</div>
        <span id="market-badge" style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:999px;background:#57f28722;color:var(--green)">🟢 OPEN</span>
      </div>
      <div id="closed-banner" style="display:none;background:#ed424522;border:1px solid #ed424566;border-radius:10px;padding:12px 14px;margin-bottom:12px;display:none;align-items:center;gap:12px">
        <span style="font-size:26px">🔴</span>
        <div style="flex:1">
          <div style="font-size:15px;font-weight:800;color:var(--red)">MARKET CLOSED</div>
          <div style="font-size:12px;color:var(--muted)">Price is frozen — no trading until the bell. Opens <b>12:00pm CST</b> · <span id="closed-countdown">—</span></div>
        </div>
      </div>
      <div class="price-hero">
        <span class="price" id="price">—</span>
        <span class="change-badge" id="change-badge">—</span>
        <span id="analyst-rating" style="display:none;font-size:13px;font-weight:700;padding:4px 10px;border-radius:6px"></span>
      </div>
      <div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap">
        <span style="font-size:11px;color:var(--muted);align-self:center;margin-right:4px">Zoom:</span>
        <button class="zoom-btn active" onclick="setZoom(10,'5m')" data-z="5m">5m</button>
        <button class="zoom-btn" onclick="setZoom(20,'10m')" data-z="10m">10m</button>
        <button class="zoom-btn" onclick="setZoom(60,'30m')" data-z="30m">30m</button>
        <button class="zoom-btn" onclick="setZoom(120,'1h')" data-z="1h">1h</button>
        <button class="zoom-btn" onclick="setZoom(0,'all')" data-z="all">All</button>
        <button class="zoom-btn" id="candle-toggle" onclick="toggleCandles()" style="margin-left:auto">🕯️ Candles</button>
      </div>
      <div class="chart-wrap" style="position:relative">
        <canvas id="priceChart"></canvas>
        <div id="chart-closed-overlay" style="display:none;position:absolute;inset:0;align-items:center;justify-content:center;pointer-events:none">
          <span style="font-size:clamp(22px,6vw,46px);font-weight:900;letter-spacing:4px;color:#ed424533;border:3px solid #ed424533;padding:6px 22px;border-radius:12px;transform:rotate(-8deg)">CLOSED</span>
        </div>
      </div>
      <div class="range-bar-wrap">
        <div class="range-labels"><span>Min $5.00</span><span id="range-label">—</span><span>Max $500.00</span></div>
        <div class="range-bar">
          <div class="range-fill" style="width:100%"></div>
          <div class="range-marker" id="range-marker" style="left:50%"></div>
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="stat-label">All-Time Low</div><div class="stat-value" id="stat-low">—</div></div>
        <div class="stat"><div class="stat-label">All-Time High</div><div class="stat-value" id="stat-high">—</div></div>
        <div class="stat"><div class="stat-label">Total Change</div><div class="stat-value" id="stat-change">—</div></div>
        <div class="stat"><div class="stat-label">Data Points</div><div class="stat-value" id="stat-points">—</div></div>
      </div>
      <div class="updated" id="updated">—</div>
    </div>

    <!-- Chat -->
    <div class="card">
      <div class="card-title">💬 Live Chat</div>
      <div id="chat-messages" style="height:300px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;margin-bottom:12px;scroll-behavior:smooth"></div>
      <div id="chat-input-area" style="display:flex;gap:8px">
        <input id="chat-input" class="trade-input" placeholder="Say something..." style="flex:1;font-size:13px;padding:7px 10px" onkeydown="if(event.key==='Enter')sendChat()" maxlength="300"/>
        <button class="btn btn-discord" style="padding:7px 14px;font-size:13px" onclick="sendChat()">Send</button>
      </div>
      <div id="chat-login-prompt" style="text-align:center;font-size:12px;color:var(--muted);margin-top:8px;display:none">
        <a href="/login" style="color:var(--accent);font-weight:600">Login with Discord</a> to chat
      </div>
    </div>
  </div>

  <!-- Right column -->
  <div>
    <!-- Admin panel toggle (only shown to admin) -->
    <div id="admin-toggle" style="display:none;margin-bottom:12px">
      <button class="btn" style="background:#ed424522;color:#ed4245;border:1px solid #ed424540;width:100%" onclick="toggleAdmin()">🛡️ Admin Panel</button>
    </div>
    <div id="admin-area" style="display:none"></div>

    <!-- Portfolio -->
    <div class="card">
      <div class="card-title">📊 My Portfolio</div>
      <div id="portfolio-area">
        <div style="text-align:center;padding:20px 8px">
          <div style="font-size:36px;margin-bottom:10px">📈</div>
          <div style="font-size:16px;font-weight:700;margin-bottom:6px">Start Trading SUS Stock</div>
          <div style="font-size:13px;color:var(--muted);margin-bottom:18px">Login with your Discord account to view your portfolio, track your net worth, and buy & sell in real time.</div>
          <a href="/login" style="display:inline-flex;align-items:center;gap:10px;background:#5865f2;color:#fff;font-weight:700;font-size:15px;padding:12px 24px;border-radius:10px;text-decoration:none;transition:background 0.2s" onmouseover="this.style.background='#4752c4'" onmouseout="this.style.background='#5865f2'">
            <svg width="20" height="15" viewBox="0 0 71 55" fill="white"><path d="M60.1 4.9A58.6 58.6 0 0 0 45.6.4a.2.2 0 0 0-.2.1 40.8 40.8 0 0 0-1.8 3.7 54.1 54.1 0 0 0-16.2 0 37.6 37.6 0 0 0-1.8-3.7.22.22 0 0 0-.2-.1A58.4 58.4 0 0 0 10.9 4.9a.2.2 0 0 0-.1.1C1.6 18.1-.9 31 .3 43.7a.24.24 0 0 0 .1.2 58.9 58.9 0 0 0 17.7 8.9.22.22 0 0 0 .2-.1 42 42 0 0 0 3.6-5.9.21.21 0 0 0-.1-.3 38.7 38.7 0 0 1-5.5-2.6.22.22 0 0 1 0-.4c.4-.3.7-.5 1.1-.8a.21.21 0 0 1 .2 0c11.5 5.3 24 5.3 35.4 0a.21.21 0 0 1 .2 0l1.1.8a.22.22 0 0 1 0 .4 36.3 36.3 0 0 1-5.5 2.6.22.22 0 0 0-.1.3 47.1 47.1 0 0 0 3.6 5.9.21.21 0 0 0 .2.1 58.7 58.7 0 0 0 17.7-8.9.23.23 0 0 0 .1-.2c1.5-15.1-2.4-28-10.4-39.5a.18.18 0 0 0-.1-.2zM23.7 36c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.2 6.4-7.2c3.6 0 6.5 3.3 6.4 7.2 0 4-2.8 7.2-6.4 7.2zm23.6 0c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.2 6.4-7.2c3.6 0 6.5 3.3 6.4 7.2 0 4-2.8 7.2-6.4 7.2z"/></svg>
            Login with Discord
          </a>
          <div style="font-size:11px;color:var(--muted);margin-top:12px">Everyone starts with $1,000 — no cost to play</div>
        </div>
      </div>
    </div>

    <!-- Leaderboard -->
    <div class="card">
      <div class="card-title">🏆 Leaderboard</div>
      <div id="leaderboard">Loading...</div>
      <div class="updated" id="lb-updated">—</div>
    </div>

  </div>
</div>

<!-- History drawer (pulls from left) -->
<div id="history-overlay" onclick="toggleHistory()" style="position:fixed;inset:0;background:#0006;z-index:90;display:none;opacity:0;transition:opacity .3s"></div>
<div id="history-drawer" style="position:fixed;top:0;left:-440px;width:min(440px,100vw);height:100vh;background:var(--surface);border-right:1px solid var(--border);z-index:91;transition:left .3s ease;overflow-y:auto;display:flex;flex-direction:column">
  <div style="padding:16px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;position:sticky;top:0;background:var(--surface);z-index:1">
    <span style="font-size:18px">📜</span>
    <span style="font-size:15px;font-weight:700">My History</span>
    <button onclick="toggleHistory()" style="margin-left:auto;background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer;line-height:1">✕</button>
  </div>
  <div id="history-list" style="padding:14px;flex:1">Loading...</div>
</div>

<!-- Companies drawer -->
<div id="companies-overlay" onclick="toggleCompanies()" style="position:fixed;inset:0;background:#0006;z-index:90;display:none;opacity:0;transition:opacity .3s"></div>
<div id="companies-drawer" style="position:fixed;top:0;right:-520px;width:min(520px,100vw);height:100vh;background:var(--surface);border-left:1px solid var(--border);z-index:91;transition:right .3s ease;overflow-y:auto;display:flex;flex-direction:column">
  <div style="padding:16px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;position:sticky;top:0;background:var(--surface);z-index:1">
    <span style="font-size:18px">🏢</span>
    <span style="font-size:15px;font-weight:700">Companies</span>
    <button onclick="showCreateDrawer()" class="btn btn-discord" style="margin-left:auto;font-size:12px;padding:5px 12px">+ Found ($2,000)</button>
    <button onclick="toggleCompanies()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer;line-height:1">✕</button>
  </div>
  <div id="drawer-companies-list" style="padding:14px;flex:1">Loading...</div>
</div>

<!-- Company detail (slides over drawer) -->
<div id="company-detail-panel" style="position:fixed;top:0;right:-520px;width:min(520px,100vw);height:100vh;background:var(--surface2);border-left:1px solid var(--border);z-index:92;transition:right .3s ease;overflow-y:auto">
  <div id="company-detail-inner" style="padding:20px"></div>
</div>

<!-- Create company modal -->
<div id="create-company-modal" style="position:fixed;inset:0;background:#0008;z-index:95;display:none;align-items:center;justify-content:center">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:26px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <span style="font-size:17px;font-weight:700">🏢 Found a Company</span>
      <button onclick="hideCreateDrawer()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer">✕</button>
    </div>
    <input id="dc-name" class="trade-input" placeholder="Company name"/>
    <input id="dc-ticker" class="trade-input" placeholder="Ticker (2-4 letters)" maxlength="4" style="text-transform:uppercase"/>
    <textarea id="dc-desc" class="trade-input" placeholder="Description (optional)" rows="2" style="resize:none"></textarea>
    <div style="font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Choose Type</div>
    <div id="dc-type-grid" style="display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-bottom:12px"></div>
    <input type="hidden" id="dc-type"/>
    <div id="dc-subprice-wrap" style="display:none;margin-bottom:8px">
      <div style="font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:6px">🔍 Subscription Price ($/hour)</div>
      <input type="number" id="dc-subprice" class="trade-input" placeholder="e.g. 40 — what members pay per hour for insider news" min="0"/>
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-discord" style="flex:1" onclick="submitCreateDrawer()">Found — $2,000</button>
      <button class="btn btn-logout" onclick="hideCreateDrawer()">Cancel</button>
    </div>
  </div>
</div>

<!-- Store modal -->
<div id="store-modal" style="position:fixed;inset:0;background:#0008;z-index:95;display:none;align-items:center;justify-content:center">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:24px;width:520px;max-width:95vw;max-height:90vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <span style="font-size:18px;font-weight:700">🛒 Sus Store</span>
      <button onclick="closeStore()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer">✕</button>
    </div>
    <div id="store-balance" style="font-size:12px;color:var(--muted);margin-bottom:14px">Your cash: —</div>

    <div style="background:var(--surface2);border-radius:10px;padding:14px;margin-bottom:12px">
      <div style="font-weight:700;margin-bottom:4px">🎮 In-Game Items <span style="font-size:11px;color:var(--muted);font-weight:400">(delivered in Minecraft)</span></div>
      <div id="store-mc-status" style="font-size:11px;color:var(--muted);margin-bottom:8px"></div>
      <div id="store-mc-items" style="display:grid;grid-template-columns:1fr 1fr;gap:6px"></div>
      <div id="store-cart" style="margin-top:10px"></div>
    </div>

    <div id="store-enchant-section" style="background:var(--surface2);border-radius:10px;padding:14px;margin-bottom:12px;display:none">
      <div style="font-weight:700;margin-bottom:4px">✨ Enchant Held Item</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px">Each adds <b>+1 level</b>. Buy more of the same and apply them to one item to raise the level. In Minecraft run <code style="background:var(--bg);padding:1px 5px;border-radius:4px">/susenchant</code>, hold an item, and click to apply.</div>
      <div id="store-enchants" style="display:grid;grid-template-columns:1fr 1fr;gap:6px"></div>
    </div>

    <div style="background:var(--surface2);border-radius:10px;padding:14px">
      <div style="font-weight:700;margin-bottom:6px">📰 Market News Post — $5,000</div>
      <input id="st-news" class="trade-input" placeholder="Headline for the market news feed" maxlength="140"/>
      <div style="display:flex;gap:6px">
        <button class="btn btn-buy" style="flex:1" onclick="buyNews(true)">📈 Good</button>
        <button class="btn btn-sell" style="flex:1" onclick="buyNews(false)">📉 Bad</button>
      </div>
    </div>
  </div>
</div>

<!-- Updates modal -->
<div id="updates-modal" style="position:fixed;inset:0;background:#0008;z-index:95;display:none;align-items:center;justify-content:center">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:24px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <span style="font-size:17px;font-weight:700">🆕 What's New</span>
      <button onclick="closeUpdates()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer">✕</button>
    </div>
    <div id="updates-body">Loading...</div>
  </div>
</div>

<!-- Lottery modal -->
<div id="lottery-modal" style="position:fixed;inset:0;background:#0008;z-index:95;display:none;align-items:center;justify-content:center">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:26px;width:420px;max-width:95vw;text-align:center">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <span style="font-size:17px;font-weight:700">🎟️ Lottery</span>
      <button onclick="closeLottery()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer">✕</button>
    </div>
    <div id="lottery-body">Loading...</div>
  </div>
</div>

<!-- Server Unlocks modal -->
<div id="unlocks-modal" style="position:fixed;inset:0;background:#0008;z-index:95;display:none;align-items:center;justify-content:center">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:24px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span style="font-size:17px;font-weight:700">🔓 Server-Wide Unlocks</span>
      <button onclick="closeUnlocks()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer">✕</button>
    </div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:14px">Pool money together to unlock dimensions for the whole Minecraft server. Contributions are final.</div>
    <div id="unlocks-list">Loading...</div>
  </div>
</div>

<!-- Link Minecraft modal -->
<div id="linkmc-modal" style="position:fixed;inset:0;background:#0008;z-index:95;display:none;align-items:center;justify-content:center">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:26px;width:440px;max-width:95vw;text-align:center">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <span style="font-size:17px;font-weight:700">🟩 Link Minecraft</span>
      <button onclick="closeLinkMC()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer">✕</button>
    </div>
    <div id="linkmc-body">
      <p style="font-size:13px;color:var(--muted);margin-bottom:14px">Generate a code, then run <code style="background:var(--surface2);padding:2px 6px;border-radius:5px">/suslink CODE</code> in the Minecraft server to link your account.</p>
      <button class="btn btn-discord" style="width:100%" onclick="genMCCode()">Generate Link Code</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const fmt = v => '$' + v.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
const medals = ['🥇','🥈','🥉'];

// ── Market open/close countdown (open 12pm–12am CST = UTC-6) ─────────────────────
function updateMarketTimer() {
  const nowUtc = new Date();
  // CST = UTC-6
  const cstMs = nowUtc.getTime() - 6 * 3600 * 1000;
  const cst = new Date(cstMs);
  const h = cst.getUTCHours(), m = cst.getUTCMinutes(), s = cst.getUTCSeconds();
  const isOpen = h >= 12; // open noon to midnight
  // Seconds until next boundary
  let target; // hour boundary in CST
  if (isOpen) target = 24;   // closes at midnight (24:00)
  else target = 12;          // opens at noon
  const secsNow = h * 3600 + m * 60 + s;
  let remain = target * 3600 - secsNow;
  if (remain < 0) remain += 24 * 3600;
  const hh = String(Math.floor(remain / 3600)).padStart(2, '0');
  const mm = String(Math.floor((remain % 3600) / 60)).padStart(2, '0');
  const ss = String(remain % 60).padStart(2, '0');

  const statusEl = document.getElementById('timer-status');
  const cdEl = document.getElementById('timer-countdown');
  const banner = document.getElementById('market-timer');
  if (isOpen) {
    statusEl.textContent = '🟢 MARKET OPEN — closes in';
    statusEl.style.color = 'var(--green)';
    cdEl.style.color = 'var(--green)';
    banner.style.background = 'linear-gradient(90deg, #57f28715, var(--surface))';
  } else {
    statusEl.textContent = '🔴 MARKET CLOSED — opens in';
    statusEl.style.color = 'var(--red)';
    cdEl.style.color = 'var(--red)';
    banner.style.background = 'linear-gradient(90deg, #ed424515, var(--surface))';
  }
  cdEl.textContent = `${hh}:${mm}:${ss}`;
}
updateMarketTimer();
setInterval(updateMarketTimer, 1000);

// ── Mobile nav ───────────────────────────────────────────────────────────────────
function toggleNav() {
  const n = document.getElementById('nav-buttons');
  if (n) n.classList.toggle('open');
}

// ── Updates / changelog ────────────────────────────────────────────────────────
async function openUpdates() {
  document.getElementById('updates-modal').style.display = 'flex';
  const log = await fetch('/api/changelog').then(r => r.ok ? r.json() : []).catch(() => []);
  document.getElementById('updates-body').innerHTML = log.map(e => `
    <div style="margin-bottom:14px">
      <div style="font-size:11px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px">${e.date}</div>
      ${e.items.map(i => `<div style="font-size:13px;padding:3px 0;color:var(--text)">${i}</div>`).join('')}
    </div>`).join('') || '<div style="color:var(--muted)">No updates yet.</div>';
}
function closeUpdates() { document.getElementById('updates-modal').style.display = 'none'; }

// ── Lottery ──────────────────────────────────────────────────────────────────────
let lotteryTimer = null;
async function openLottery() {
  document.getElementById('lottery-modal').style.display = 'flex';
  await loadLottery();
  if (lotteryTimer) clearInterval(lotteryTimer);
  lotteryTimer = setInterval(updateLotteryCountdown, 1000);
}
function closeLottery() {
  document.getElementById('lottery-modal').style.display = 'none';
  if (lotteryTimer) { clearInterval(lotteryTimer); lotteryTimer = null; }
}
let lotteryState = null;
async function loadLottery() {
  lotteryState = await fetch('/api/lottery').then(r => r.ok ? r.json() : null).catch(() => null);
  renderLottery();
}
function renderLottery() {
  const el = document.getElementById('lottery-body');
  const d = lotteryState;
  if (!d) { el.innerHTML = 'Unavailable.'; return; }
  if (d.open) {
    el.innerHTML = `
      <div style="font-size:13px;color:var(--green);font-weight:700;margin-bottom:4px">🟢 OPEN — closes in <span id="lot-countdown">--:--</span></div>
      <div style="font-size:32px;font-weight:800;margin:8px 0">${fmt(d.pot)}</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:4px">current prize pool · ${d.total_tickets} tickets sold</div>
      <div style="font-size:12px;margin-bottom:12px">You have <b>${d.my_tickets}</b>/3 ticket(s) · ${fmt(d.ticket_price)} each</div>
      ${d.my_tickets >= 3 ? '<div style="font-size:12px;color:var(--green);font-weight:700">✅ Max tickets bought — good luck!</div>'
        : `<div style="display:flex;gap:6px;justify-content:center">
        <input type="number" id="lot-count" class="trade-input" placeholder="# tickets" min="1" max="${3 - d.my_tickets}" value="1" style="max-width:120px;margin-bottom:0"/>
        <button class="btn btn-discord" onclick="buyTickets()">Buy Tickets</button>
      </div>`}
      <div style="font-size:10px;color:var(--muted);margin-top:8px">Max 3 tickets each. Winner drawn randomly — more tickets = better odds.</div>`;
  } else {
    el.innerHTML = `
      <div style="font-size:13px;color:var(--red);font-weight:700;margin-bottom:8px">🔴 CLOSED</div>
      <div style="font-size:13px;color:var(--muted)">Next lottery opens in <span id="lot-countdown">--:--</span></div>
      <div style="font-size:11px;color:var(--muted);margin-top:8px">Rounds open every 2 hours and run for 10 minutes.</div>`;
  }
}
function updateLotteryCountdown() {
  const el = document.getElementById('lot-countdown');
  if (!el || !lotteryState) return;
  const now = Date.now() / 1000;
  let target = lotteryState.open ? lotteryState.ends_at : lotteryState.next_open;
  let remain = Math.max(0, Math.round(target - now));
  if (remain <= 0) { loadLottery(); return; }
  const h = Math.floor(remain/3600), m = Math.floor((remain%3600)/60), s = remain%60;
  el.textContent = (h>0?h+':':'') + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
}
async function buyTickets() {
  const count = parseInt(document.getElementById('lot-count')?.value) || 1;
  const res = await fetch('/api/lottery/buy', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({count})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`Bought ${count} ticket(s)!`);
  loadLottery(); fetchMe();
}

// ── Server Unlocks ───────────────────────────────────────────────────────────────
async function openUnlocks() {
  document.getElementById('unlocks-modal').style.display = 'flex';
  loadUnlocks();
}
function closeUnlocks() { document.getElementById('unlocks-modal').style.display = 'none'; }
async function loadUnlocks() {
  const list = await fetch('/api/unlocks').then(r => r.ok ? r.json() : []).catch(() => []);
  const el = document.getElementById('unlocks-list');
  el.innerHTML = list.map(u => {
    const pct = Math.min(100, (u.pooled / u.goal) * 100);
    return `<div style="background:var(--surface2);border-radius:10px;padding:14px;margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span style="font-size:20px">${u.emoji}</span>
        <span style="font-weight:700;flex:1">${u.name}</span>
        ${u.unlocked ? '<span style="font-size:11px;font-weight:700;color:var(--green)">✅ UNLOCKED</span>' : ''}
      </div>
      <div style="height:10px;background:var(--bg);border-radius:5px;overflow:hidden;margin-bottom:4px">
        <div style="height:100%;width:${pct}%;background:${u.unlocked?'var(--green)':'var(--accent)'}"></div>
      </div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:8px">${fmt(u.pooled)} / ${fmt(u.goal)} pooled · you gave ${fmt(u.mine)}</div>
      ${u.unlocked ? '' : `<div style="display:flex;gap:6px">
        <input type="number" id="unlock-amt-${u.key}" class="trade-input" placeholder="Amount to contribute" style="flex:1;margin-bottom:0"/>
        <button class="btn btn-discord" onclick="contributeUnlock('${u.key}')">Contribute</button>
      </div>`}
    </div>`;
  }).join('');
}
async function contributeUnlock(key) {
  const amt = parseFloat(document.getElementById('unlock-amt-'+key)?.value);
  if (!amt || amt <= 0) { showToast('Enter an amount', false); return; }
  const res = await fetch('/api/unlocks/contribute', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({key, amount: amt})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(d.newly ? '🔓 UNLOCKED for the whole server!' : 'Contributed!');
  loadUnlocks(); fetchMe();
}

// ── Link Minecraft ───────────────────────────────────────────────────────────────
async function openLinkMC() {
  if (!myUserId) { showToast('Login first', false); return; }
  const me = await fetch('/api/me').then(r => r.ok ? r.json() : null).catch(() => null);
  const body = document.getElementById('linkmc-body');
  if (me && me.mc_linked) {
    body.innerHTML = `<div style="font-size:14px;color:var(--green);font-weight:700;margin-bottom:6px">✅ Linked to Minecraft</div>
      <div style="font-size:13px;color:var(--muted)">Account: <b style="color:var(--text)">${me.mc_linked}</b></div>
      <div style="font-size:12px;color:var(--muted);margin-top:10px">Your SUS cash, rewards, and status are now shared with Minecraft.</div>`;
  } else {
    body.innerHTML = `<p style="font-size:13px;color:var(--muted);margin-bottom:14px">Generate a code, then run <code style="background:var(--surface2);padding:2px 6px;border-radius:5px">/suslink CODE</code> in the Minecraft server to link your account.</p>
      <button class="btn btn-discord" style="width:100%" onclick="genMCCode()">Generate Link Code</button>`;
  }
  document.getElementById('linkmc-modal').style.display = 'flex';
}
function closeLinkMC() { document.getElementById('linkmc-modal').style.display = 'none'; }
async function genMCCode() {
  const res = await fetch('/api/mc/generate_code', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  document.getElementById('linkmc-body').innerHTML = `
    <div style="font-size:12px;color:var(--muted);margin-bottom:8px">In Minecraft, run:</div>
    <div style="font-size:24px;font-weight:800;letter-spacing:3px;background:var(--surface2);border-radius:10px;padding:14px;margin-bottom:8px">/suslink ${d.code}</div>
    <div style="font-size:11px;color:var(--muted)">Code expires in 10 minutes.</div>`;
}

// ── Store ──────────────────────────────────────────────────────────────────────
async function openStore() {
  if (!myUserId) { showToast('Login first', false); return; }
  document.getElementById('store-modal').style.display = 'flex';
  // Fill balance
  const me = await fetch('/api/me').then(r => r.ok ? r.json() : null).catch(() => null);
  if (me) document.getElementById('store-balance').textContent = 'Your cash: ' + fmt(me.cash);
  // In-game items
  const mcStatus = document.getElementById('store-mc-status');
  const mcItemsEl = document.getElementById('store-mc-items');
  if (me && me.mc_linked) {
    mcStatus.textContent = 'Linked as ' + me.mc_linked + ' — items arrive in-game automatically within 30s.';
    mcItemsCache = await fetch('/api/store/mc_items').then(r => r.ok ? r.json() : []).catch(() => []);
    mcItemsEl.innerHTML = mcItemsCache.map(it => `<button class="btn btn-buy" style="flex-direction:column;padding:8px;font-size:12px" onclick="addToCart('${it.key}')">${it.name}<span style="font-size:10px;color:var(--muted)">${fmt(it.cost)}</span></button>`).join('');
    renderCart();
    // Enchants
    document.getElementById('store-enchant-section').style.display = 'block';
    const ench = await fetch('/api/store/enchants').then(r => r.ok ? r.json() : []).catch(() => []);
    document.getElementById('store-enchants').innerHTML = ench.map(e => `<button class="btn btn-discord" style="flex-direction:column;padding:8px;font-size:12px" onclick="buyEnchant('${e.key}')">${e.name}<span style="font-size:10px;color:#cdd">${fmt(e.cost)}</span></button>`).join('');
  } else {
    mcStatus.innerHTML = '<span style="color:var(--red)">Link your Minecraft account (🟩 Link MC) to buy in-game items.</span>';
    mcItemsEl.innerHTML = '';
    document.getElementById('store-cart').innerHTML = '';
    document.getElementById('store-enchant-section').style.display = 'none';
  }
}

async function buyEnchant(enchant) {
  const res = await fetch('/api/store/buy_enchant', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enchant})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast('Bought! Hold the item in Minecraft and run /susenchant');
  fetchMe();
  const me = await fetch('/api/me').then(r => r.ok ? r.json() : null).catch(() => null);
  if (me) document.getElementById('store-balance').textContent = 'Your cash: ' + fmt(me.cash);
}

let cart = {};
let mcItemsCache = [];
function itemInfo(key) { return mcItemsCache.find(i => i.key === key) || {name:key, cost:0}; }
function addToCart(key) { cart[key] = (cart[key]||0) + 1; renderCart(); }
function removeFromCart(key) { if (cart[key]) { cart[key]--; if (cart[key] <= 0) delete cart[key]; } renderCart(); }
function renderCart() {
  const el = document.getElementById('store-cart');
  if (!el) return;
  const keys = Object.keys(cart);
  if (!keys.length) { el.innerHTML = '<div style="font-size:11px;color:var(--muted);text-align:center;padding:6px">🛒 Cart is empty — tap items to add</div>'; return; }
  let total = 0;
  let rows = keys.map(k => {
    const it = itemInfo(k); const line = it.cost * cart[k]; total += line;
    return `<div style="display:flex;align-items:center;gap:8px;font-size:12px;padding:3px 0">
      <span style="flex:1">${it.name} ×${cart[k]}</span>
      <span style="color:var(--muted)">${fmt(line)}</span>
      <button onclick="removeFromCart('${k}')" style="background:none;border:none;color:var(--red);cursor:pointer;font-size:13px">−</button>
      <button onclick="addToCart('${k}')" style="background:none;border:none;color:var(--green);cursor:pointer;font-size:13px">+</button>
    </div>`;
  }).join('');
  el.innerHTML = `<div style="border-top:1px solid var(--border);margin-top:8px;padding-top:8px">${rows}
    <div style="display:flex;justify-content:space-between;font-weight:700;margin:8px 0;font-size:13px"><span>Total</span><span>${fmt(total)}</span></div>
    <button class="btn btn-discord" style="width:100%" onclick="checkoutCart()">Checkout — ${fmt(total)}</button></div>`;
}
async function checkoutCart() {
  const total = Object.keys(cart).reduce((s,k) => s + itemInfo(k).cost * cart[k], 0);
  const summary = Object.keys(cart).map(k => `${itemInfo(k).name} ×${cart[k]}`).join('\\n');
  if (!confirm(`Confirm purchase for ${fmt(total)}?\\n\\n${summary}`)) return;
  const res = await fetch('/api/store/mc_checkout', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({cart})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`Purchased ${fmt(d.total)}! Items arrive in-game within 30s.`);
  cart = {}; renderCart(); fetchMe();
  const me = await fetch('/api/me').then(r => r.ok ? r.json() : null).catch(() => null);
  if (me) document.getElementById('store-balance').textContent = 'Your cash: ' + fmt(me.cash);
}
function closeStore() { document.getElementById('store-modal').style.display = 'none'; }

async function storeBuy(url, body, msg) {
  const res = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(msg); fetchMe();
  const me = await fetch('/api/me').then(r => r.ok ? r.json() : null).catch(() => null);
  if (me) document.getElementById('store-balance').textContent = 'Your cash: ' + fmt(me.cash);
}
function buyRole() { const name=document.getElementById('st-role-name').value.trim(); const color=document.getElementById('st-role-color').value; if(!name){showToast('Enter a role name',false);return;} storeBuy('/api/store/role',{name,color},'Role created & assigned!'); }
function buyNick() { const nick=document.getElementById('st-nick').value.trim(); if(!nick){showToast('Enter a nickname',false);return;} storeBuy('/api/store/nickname',{nick},'Nickname changed!'); }
function buyTimeout() { const t=document.getElementById('st-timeout-target').value; if(!t){showToast('Pick a user',false);return;} storeBuy('/api/store/timeout',{target_id:t},'User timed out 10 min'); }
function buyPin() { const channel_id=document.getElementById('st-pin-channel').value; const message=document.getElementById('st-pin-msg').value.trim(); if(!channel_id||!message){showToast('Pick channel & message',false);return;} storeBuy('/api/store/pin',{channel_id,message},'Message pinned!'); }
function buyAnnounce() { const channel_id=document.getElementById('st-ann-channel').value; const message=document.getElementById('st-ann-msg').value.trim(); if(!channel_id||!message){showToast('Pick channel & message',false);return;} storeBuy('/api/store/announce',{channel_id,message},'Announcement posted!'); }
function buyNews(positive) { const headline=document.getElementById('st-news').value.trim(); if(!headline){showToast('Enter a headline',false);return;} storeBuy('/api/store/news',{headline,positive},'Posted to market news!'); }

// Live countdown on insider EARLY news items
function updateEventCountdowns() {
  const now = Date.now() / 1000;
  document.querySelectorAll('.event-countdown').forEach(el => {
    const publicAt = parseInt(el.dataset.public);
    let remain = Math.max(0, Math.round(publicAt - now));
    if (remain <= 0) { el.textContent = '⚡ NOW'; return; }
    const mm = String(Math.floor(remain / 60)).padStart(2, '0');
    const ss = String(remain % 60).padStart(2, '0');
    el.textContent = `⏳ ${mm}:${ss}`;
  });
  // Subscriber next-payment countdowns
  document.querySelectorAll('.sub-due').forEach(el => {
    const due = parseInt(el.dataset.due);
    let remain = Math.round(due - now);
    if (remain <= 0) { el.textContent = 'next: due now'; return; }
    const mm = String(Math.floor(remain / 60)).padStart(2, '0');
    const ss = String(remain % 60).padStart(2, '0');
    el.textContent = `next: ${mm}:${ss}`;
  });
}
setInterval(updateEventCountdowns, 1000);
let myUserId = null;

function showToast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (ok ? 'ok' : 'err');
  setTimeout(() => t.className = 'toast', 3000);
}

// Chart
const ctx = document.getElementById('priceChart').getContext('2d');
let chart = null;
let chartMode = 'line';

function chartOptions() {
  return {
    responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
    plugins: { legend: { display: false }, tooltip: { callbacks: {
      label: c => { const r = c.raw; return Array.isArray(r) ? fmt(r[1]) : fmt(c.parsed.y); },
      title: items => items[0].label || ''
    }}},
    scales: {
      x: { display: true, stacked: false, ticks: { color: '#949ba4', maxTicksLimit: 6, maxRotation: 0, autoSkip: true, font: { size: 10 } }, grid: { display: false }, border: { display: false } },
      y: { grid: { color: '#3a3c40' }, ticks: { color: '#949ba4', callback: v => fmt(v) }, border: { display: false } }
    }
  };
}
function ensureChart(type) {
  if (chart && chart.config.type === type) return;
  if (chart) chart.destroy();
  chart = new Chart(ctx, { type: type, data: { labels: [], datasets: [] }, options: chartOptions() });
}
ensureChart('line');

function toggleCandles() {
  chartMode = chartMode === 'line' ? 'candle' : 'line';
  const btn = document.getElementById('candle-toggle');
  if (btn) btn.textContent = chartMode === 'candle' ? '📈 Line' : '🕯️ Candles';
  renderChart();
}

function buildCandles(h, t) {
  const n = h.length;
  const groupSize = Math.max(1, Math.floor(n / 30));
  const candles = [];
  let prevClose = h[0];
  for (let i = 0; i < n; i += groupSize) {
    const grp = h.slice(i, i + groupSize);
    if (!grp.length) continue;
    const open = prevClose;                 // open = previous candle's close
    const close = grp[grp.length - 1];
    const hi = Math.max(open, ...grp);
    const lo = Math.min(open, ...grp);
    candles.push({ o: open, c: close, hi: hi, lo: lo,
                   label: t[Math.min(i + grp.length - 1, t.length - 1)] || '' });
    prevClose = close;
  }
  return candles;
}

function updateClosedCountdown() {
  const el = document.getElementById('closed-countdown');
  if (!el) return;
  // Current time in CST (UTC-6), read via UTC fields
  const cst = new Date(Date.now() - 6 * 3600000);
  const secsNow = cst.getUTCHours() * 3600 + cst.getUTCMinutes() * 60 + cst.getUTCSeconds();
  let diff = 12 * 3600 - secsNow;        // seconds until noon CST
  if (diff <= 0) diff += 24 * 3600;
  const h = Math.floor(diff / 3600), m = Math.floor((diff % 3600) / 60), s = diff % 60;
  el.textContent = `opens in ${h}h ${m}m ${s}s`;
}
setInterval(() => { const b = document.getElementById('closed-banner'); if (b && b.style.display !== 'none') updateClosedCountdown(); }, 1000);

async function fetchStock() {
  const d = await fetch('/api/stock').then(r => r.json());
  const { price, change, change_pct: pct, history, timestamps } = d;
  const up = change >= 0;
  if (!history) return;
  document.getElementById('price').textContent = fmt(price);
  const badge = document.getElementById('change-badge');
  badge.textContent = `${up?'+':''}${fmt(change)} (${up?'+':''}${pct}%)`;
  badge.className = 'change-badge ' + (up ? 'up' : 'down');
  // Analyst rating (only for Analyst Firm subscribers)
  const arEl = document.getElementById('analyst-rating');
  if (arEl) {
    if (d.analyst_rating) {
      const r = d.analyst_rating;
      const buy = r.includes('Buy'), sell = r.includes('Sell');
      arEl.style.display = 'inline-block';
      arEl.textContent = '📋 ' + r;
      arEl.style.background = buy ? '#57f28722' : (sell ? '#ed424522' : 'var(--surface2)');
      arEl.style.color = buy ? 'var(--green)' : (sell ? 'var(--red)' : 'var(--muted)');
    } else {
      arEl.style.display = 'none';
    }
  }
  // Live-update open short P&L from the current price
  const spEl = document.getElementById('short-pnl-live');
  if (spEl) {
    const sh = parseFloat(spEl.dataset.shares), entry = parseFloat(spEl.dataset.entry);
    const pnl = (entry - price) * sh;
    spEl.textContent = `P&L: ${pnl >= 0 ? '+' : ''}${fmt(pnl)}`;
    spEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
  }
  fullHistory = history;
  fullTimestamps = timestamps || [];
  renderChart();

  // Market status badge
  const mb = document.getElementById('market-badge');
  if (mb && d.market_open !== undefined) {
    mb.textContent = d.market_open ? '🟢 OPEN' : '🔴 CLOSED';
    mb.style.background = d.market_open ? '#57f28722' : '#ed424522';
    mb.style.color = d.market_open ? 'var(--green)' : 'var(--red)';
  }
  // Big closed banner + chart watermark + countdown to open
  if (d.market_open !== undefined) {
    const banner = document.getElementById('closed-banner');
    const overlay = document.getElementById('chart-closed-overlay');
    if (banner) banner.style.display = d.market_open ? 'none' : 'flex';
    if (overlay) overlay.style.display = d.market_open ? 'none' : 'flex';
    if (!d.market_open) updateClosedCountdown();
  }
  // Disable the SUS trade buttons while the market is closed
  if (d.market_open !== undefined) {
    document.querySelectorAll('.btn-buy, .btn-sell').forEach(b => {
      const isTradeBtn = /trade\\(|openShort\\(|coverShort\\(|placeLimit\\(/.test(b.getAttribute('onclick') || '');
      if (isTradeBtn) {
        b.disabled = !d.market_open;
        b.style.opacity = d.market_open ? '' : '0.5';
        b.style.cursor = d.market_open ? '' : 'not-allowed';
        if (!d.market_open) b.title = 'Market closed (opens 12pm CST)';
      }
    });
  }
  // News ticker (top bar) + full news feed
  const nowSec = Math.floor(Date.now() / 1000);
  // Insider badge on the news panel title
  const newsTitle = document.getElementById('news-title');
  if (newsTitle) {
    newsTitle.innerHTML = d.is_insider
      ? '📰 Market News <span style="font-size:9px;font-weight:700;background:#5865f2;color:#fff;padding:1px 6px;border-radius:999px;margin-left:4px">🔍 INSIDER</span>'
      : '📰 Market News';
  }
  if (d.news && d.news.length) {
    const latest = d.news[d.news.length - 1];
    const ticker = document.getElementById('news-ticker');
    ticker.textContent = latest.headline;
    ticker.style.display = 'block';
    ticker.style.color = latest.positive ? 'var(--green)' : 'var(--red)';

    const feed = document.getElementById('news-feed');
    if (feed) {
      feed.innerHTML = [...d.news].reverse().map(n => {
        const t = new Date(n.ts * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        const impact = n.impact ? ` <span style="font-size:11px;font-weight:700;color:${n.impact>0?'var(--green)':'var(--red)'}">${n.impact>0?'+':''}${n.impact.toFixed(1)}%</span>` : '';
        const publicAt = n.public_at || n.ts;
        const isEarly = d.is_insider && publicAt > nowSec;
        const earlyBadge = isEarly ? ` <span style="font-size:9px;font-weight:700;background:#5865f2;color:#fff;padding:1px 5px;border-radius:999px">EARLY</span> <span class="event-countdown" data-public="${publicAt}" style="font-size:10px;font-weight:700;color:#5865f2">⏳ --:--</span>` : '';
        return `<div style="padding:8px 0;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:flex-start;${isEarly?'background:#5865f211;border-radius:6px;padding:8px':''}">
          <span style="font-size:18px;flex-shrink:0">${n.kind === 'personal' ? '💸' : (n.kind === 'announcement' ? '📢' : (n.positive ? '📈' : '📉'))}</span>
          <div style="flex:1;min-width:0">
            <div style="font-size:13px;line-height:1.4">${n.headline}${impact}${earlyBadge}</div>
            <div style="font-size:10px;color:var(--muted);margin-top:2px">${t}</div>
          </div>
        </div>`;
      }).join('');
    }
  }
  document.getElementById('range-marker').style.left = ((price - 5) / 495 * 100) + '%';
  document.getElementById('range-label').textContent = fmt(price);
  const low = Math.min(...history), high = Math.max(...history), tc = price - history[0];
  document.getElementById('stat-low').textContent = fmt(low);
  document.getElementById('stat-high').textContent = fmt(high);
  const sc = document.getElementById('stat-change');
  sc.textContent = `${tc>=0?'+':''}${fmt(tc)}`;
  sc.style.color = tc >= 0 ? 'var(--green)' : 'var(--red)';
  document.getElementById('stat-points').textContent = history.length;
  document.getElementById('updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
}

let meData = null;
function setMaxShares(kind) {
  if (!meData) return;
  const price = meData.price || 0;
  let max = 0;
  if (kind === 'buy') max = price > 0 ? Math.floor(meData.cash / (price * (1 + 0.01))) : 0;
  else if (kind === 'sell') max = meData.shares || 0;
  else if (kind === 'short') max = price > 0 ? Math.floor(meData.cash / price) : 0;
  const ids = { buy: 'trade-amount', sell: 'sell-amount', short: 'short-amount' };
  const el = document.getElementById(ids[kind]);
  if (el) el.value = Math.max(0, max);
}

async function fetchMe() {
  const res = await fetch('/api/me');
  if (!res.ok) { initChat(); return; }
  const u = await res.json();
  meData = u;
  myUserId = u.user_id;

  // Update header
  const authArea = document.getElementById('auth-area');
  const avatarUrl = u.avatar
    ? `https://cdn.discordapp.com/avatars/${u.user_id}/${u.avatar}.png?size=64`
    : `https://cdn.discordapp.com/embed/avatars/0.png`;
  authArea.innerHTML = `
    <img src="${avatarUrl}" class="avatar" alt="avatar"/>
    <span style="font-weight:600;font-size:13px">${u.username}</span>
    <a href="/logout" class="btn btn-logout">Logout</a>`;

  // Portfolio
  const pnlColor = u.pnl >= 0 ? 'var(--green)' : 'var(--red)';
  isLoggedIn = true;
  myAvgCost = (u.shares > 0) ? (u.avg_cost || 0) : 0;
  renderChart();
  const mcBtn = document.getElementById('linkmc-btn');
  if (mcBtn) {
    if (u.mc_linked) {
      mcBtn.textContent = '🟩 ' + u.mc_linked;
      mcBtn.style.borderColor = 'var(--green)';
      mcBtn.style.color = 'var(--green)';
    } else {
      mcBtn.textContent = '🟩 Link MC';
      mcBtn.style.borderColor = 'var(--border)';
      mcBtn.style.color = 'var(--text)';
    }
  }
  if (u.username === 'slasher_asher') {
    document.getElementById('admin-toggle').style.display = 'block';
    loadAdmin();
  }

  const actingId = u.acting_as ? u.acting_as.id : '';
  const myCompanies = u.my_companies || [];

  // Preserve any values the user has typed, plus which field is focused
  const portfolioArea = document.getElementById('portfolio-area');
  const savedInputs = {};
  portfolioArea.querySelectorAll('input, select').forEach(el => { if (el.id) savedInputs[el.id] = el.value; });
  const focusedId = document.activeElement && document.activeElement.closest && document.activeElement.closest('#portfolio-area') ? document.activeElement.id : null;

  portfolioArea.innerHTML = `
    ${myCompanies.length ? `<div style="margin-bottom:10px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px">Trading As</div>
      <select id="act-as-select" onchange="setActAs(this.value)" class="trade-input" style="margin-bottom:0;${u.acting_as?'border-color:var(--accent);color:var(--accent);font-weight:700':''}">
        <option value="" ${!actingId?'selected':''}>👤 ${u.username} (personal)</option>
        ${myCompanies.map(c => `<option value="${c.id}" ${actingId===c.id?'selected':''}>🏢 ${c.name} (${c.ticker})</option>`).join('')}
      </select>
    </div>` : ''}
    ${u.acting_as ? `<div style="background:#5865f222;border:1px solid var(--accent);border-radius:8px;padding:8px 10px;margin-bottom:10px;font-size:12px;font-weight:700;color:var(--accent)">🏢 Trading on behalf of ${u.acting_as.name} — treasury & SUS holdings</div>` : ''}
    <div class="portfolio-grid">
      <div class="p-stat"><div class="p-stat-label">${u.acting_as?'Company Value':'Net Worth'}</div><div class="p-stat-value">${fmt(u.net_worth)}</div></div>
      ${u.acting_as ? `<div class="p-stat"><div class="p-stat-label">Treasury</div><div class="p-stat-value">${fmt(u.cash)}</div></div>`
        : `<div class="p-stat"><div class="p-stat-label">P&L</div><div class="p-stat-value" style="color:${pnlColor}">${u.pnl>=0?'+':''}${fmt(u.pnl)}</div></div>`}
      <div class="p-stat"><div class="p-stat-label">SUS Shares</div><div class="p-stat-value" id="my-shares">${u.shares}</div></div>
      <div class="p-stat"><div class="p-stat-label">${u.acting_as?'Treasury Cash':'Cash'}</div><div class="p-stat-value" id="my-cash">${fmt(u.cash)}</div></div>
      <div class="p-stat" style="grid-column:span 2"><div class="p-stat-label">Invested Value</div><div class="p-stat-value" id="my-invested">${fmt(u.invested)}</div></div>
    </div>
    <!-- Trading tabs -->
    <div style="display:flex;gap:4px;margin-bottom:10px;flex-wrap:wrap">
      ${(u.acting_as ? ['Buy','Sell','Short'] : (u.verified ? ['Buy','Sell','Short','Limits','Send'] : ['Buy','Sell','Short','Limits'])).map(t => `<button onclick="setTab('${t.toLowerCase()}')" id="tab-${t.toLowerCase()}" class="zoom-btn ${t==='Buy'?'active':''}" style="flex:1">${t}</button>`).join('')}
    </div>
    ${u.verified ? '<div style="font-size:10px;color:var(--green);font-weight:700;margin-bottom:8px">✓ VERIFIED ACCOUNT</div>' : ''}
    ${(!u.acting_as && u.credit_tier) ? `<div style="display:flex;align-items:center;gap:8px;background:var(--surface2);border-radius:8px;padding:8px 12px;margin-bottom:10px">
      <span style="font-size:18px">${u.credit_tier.emoji}</span>
      <div style="flex:1">
        <div style="font-size:12px;font-weight:700">Credit: ${u.credit} <span style="color:${u.credit_tier.color}">${u.credit_tier.name}</span></div>
        <div style="font-size:10px;color:var(--muted)">Send limit: ${u.send_limit === null ? 'Unlimited' : fmt(u.send_limit)} · repay loans to raise it</div>
      </div>
    </div>` : ''}

    <div id="tab-buy-content">
      <div style="display:flex;gap:6px">
        <input type="number" id="trade-amount" class="trade-input" placeholder="Shares to buy..." min="1" style="flex:1;margin-bottom:0"/>
        <button class="zoom-btn" onclick="setMaxShares('buy')">Max</button>
      </div>
      <button class="btn btn-buy" style="width:100%;margin-top:8px" onclick="trade('buy')">📈 Buy SUS</button>
    </div>

    <div id="tab-sell-content" style="display:none">
      <div style="display:flex;gap:6px">
        <input type="number" id="sell-amount" class="trade-input" placeholder="Shares to sell..." min="1" style="flex:1;margin-bottom:0"/>
        <button class="zoom-btn" onclick="setMaxShares('sell')">Max</button>
      </div>
      <button class="btn btn-sell" style="width:100%;margin-top:8px" onclick="trade('sell')">📉 Sell SUS</button>
    </div>

    <div id="tab-short-content" style="display:none">
      ${u.short ? `
        <div style="background:#ed424518;border:1px solid #ed424540;border-radius:8px;padding:12px;margin-bottom:10px">
          <div style="font-weight:700;color:var(--red);margin-bottom:6px">📉 Open Short Position</div>
          <div style="font-size:13px;color:var(--muted)">${u.short.shares} shares @ ${fmt(u.short.entry_price)}</div>
          <div id="short-pnl-live" data-shares="${u.short.shares}" data-entry="${u.short.entry_price}" style="font-size:15px;font-weight:700;${u.short_pnl >= 0 ? 'color:var(--green)' : 'color:var(--red)'}">P&L: ${u.short_pnl >= 0 ? '+' : ''}${fmt(u.short_pnl)}</div>
        </div>
        <button class="btn btn-sell" style="width:100%" onclick="coverShort()">Close Short Position</button>
      ` : `
        <div style="font-size:12px;color:var(--muted);margin-bottom:8px">Short selling lets you profit when the price drops. You borrow shares and buy them back later at a lower price.</div>
        <div style="display:flex;gap:6px">
          <input type="number" id="short-amount" class="trade-input" placeholder="Shares to short..." min="1" style="flex:1;margin-bottom:0"/>
          <button class="zoom-btn" onclick="setMaxShares('short')">Max</button>
        </div>
        <button class="btn btn-sell" style="width:100%;margin-top:8px" onclick="openShort()">📉 Open Short</button>
      `}
    </div>

    <div id="tab-limits-content" style="display:none">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
        <input type="number" id="lshares" class="trade-input" placeholder="Shares" min="1"/>
        <input type="number" id="lprice" class="trade-input" placeholder="Target price" step="0.01"/>
      </div>
      <div style="display:flex;gap:6px;margin-bottom:12px">
        <button class="btn btn-buy" style="flex:1" onclick="placeLimit('buy')">🟢 Limit Buy</button>
        <button class="btn btn-sell" style="flex:1" onclick="placeLimit('sell')">🔴 Limit Sell</button>
      </div>
      <div id="orders-list" style="font-size:12px">
        ${u.limit_orders && u.limit_orders.length ? u.limit_orders.map((o,i) => `
          <div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-top:1px solid var(--border)">
            <span style="color:${o.type==='buy'?'var(--green)':'var(--red)'}">● ${o.type==='buy'?'Buy':'Sell'} ${o.shares} @ ${fmt(o.price)}</span>
            <button onclick="cancelOrder(${i})" style="margin-left:auto;background:none;border:none;color:var(--red);cursor:pointer;font-size:11px">✕ Cancel</button>
          </div>`).join('') : '<div style="color:var(--muted)">No active limit orders.</div>'}
      </div>
    </div>

    ${u.verified ? `<div id="tab-send-content" style="display:none">
      <div style="font-size:12px;color:var(--muted);margin-bottom:8px">Send cash to another verified user. Both accounts must be verified.</div>
      <select id="send-recipient" class="trade-input"><option value="">Select recipient...</option></select>
      <input type="number" id="send-amount" class="trade-input" placeholder="Amount" min="1"/>
      <button class="btn btn-discord" style="width:100%" onclick="sendMoney()">📤 Send Money</button>
    </div>` : ''}`;
  // Restore typed values and focus after re-render
  Object.keys(savedInputs).forEach(id => {
    const el = document.getElementById(id);
    if (el && savedInputs[id]) el.value = savedInputs[id];
  });
  if (focusedId) { const fe = document.getElementById(focusedId); if (fe) fe.focus(); }
  // Restore the previously active tab after re-render
  setTab(activeTab);
  if (u.verified) loadRecipients();
}

let activeTab = 'buy';
function setTab(tab) {
  // Fall back to buy if the requested tab isn't present (e.g. company mode)
  if (!document.getElementById('tab-'+tab+'-content')) tab = 'buy';
  activeTab = tab;
  ['buy','sell','short','limits','send'].forEach(t => {
    const el = document.getElementById('tab-'+t+'-content');
    const btn = document.getElementById('tab-'+t);
    if (el) el.style.display = t === tab ? 'block' : 'none';
    if (btn) btn.classList.toggle('active', t === tab);
  });
}

async function loadRecipients() {
  const sel = document.getElementById('send-recipient');
  if (!sel) return;
  const users = await fetch('/api/verified_users').then(r => r.ok ? r.json() : []).catch(() => []);
  const current = sel.value;
  sel.innerHTML = '<option value="">Select recipient...</option>' + users.map(u => `<option value="${u.id}">${u.username}</option>`).join('');
  sel.value = current;
}

async function sendMoney() {
  const recipient_id = document.getElementById('send-recipient')?.value;
  const amount = parseFloat(document.getElementById('send-amount')?.value);
  if (!recipient_id) { showToast('Pick a recipient', false); return; }
  if (!amount || amount <= 0) { showToast('Enter an amount', false); return; }
  const res = await fetch('/api/send_money', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({recipient_id, amount})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`Sent ${fmt(amount)}!`);
  document.getElementById('send-amount').value = '';
  fetchMe();
}

async function setActAs(companyId) {
  const res = await fetch('/api/act_as', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({company_id: companyId})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(companyId ? 'Now trading as company' : 'Back to personal trading');
  activeTab = 'buy';
  fetchMe();
}

async function trade(action) {
  const inputId = action === 'buy' ? 'trade-amount' : 'sell-amount';
  const shares = parseInt(document.getElementById(inputId)?.value);
  if (!shares || shares < 1) { showToast('Enter a valid number of shares', false); return; }
  const res = await fetch('/api/' + action, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({shares})
  });
  const data = await res.json();
  if (!res.ok) { showToast(data.error, false); return; }
  document.getElementById(inputId).value = '';
  if (action === 'buy') showToast(`Bought ${data.bought} shares for ${fmt(data.cost)}`);
  else showToast(`Sold ${data.sold} shares for ${fmt(data.earnings)}`);
  fetchMe();
}

async function openShort() {
  const shares = parseInt(document.getElementById('short-amount')?.value);
  if (!shares || shares < 1) { showToast('Enter a valid number of shares', false); return; }
  const res = await fetch('/api/short', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({shares})
  });
  const data = await res.json();
  if (!res.ok) { showToast(data.error, false); return; }
  showToast(`Shorted ${data.shares} shares @ ${fmt(data.entry_price)}`);
  fetchMe();
}

async function coverShort() {
  const res = await fetch('/api/cover', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  const data = await res.json();
  if (!res.ok) { showToast(data.error, false); return; }
  showToast(`Short covered. P&L: ${data.pnl >= 0 ? '+' : ''}${fmt(data.pnl)}`, data.pnl >= 0);
  fetchMe();
}

async function placeLimit(type) {
  const shares = parseInt(document.getElementById('lshares')?.value);
  const price = parseFloat(document.getElementById('lprice')?.value);
  if (!shares || !price || shares < 1 || price <= 0) { showToast('Enter valid shares and price', false); return; }
  const res = await fetch('/api/limit' + type, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({shares, price})
  });
  const data = await res.json();
  if (!res.ok) { showToast(data.error, false); return; }
  document.getElementById('lshares').value = '';
  document.getElementById('lprice').value = '';
  showToast(`Limit ${type} set: ${shares} shares @ ${fmt(price)}`);
  fetchMe();
}

async function cancelOrder(idx) {
  const res = await fetch('/api/cancel_order', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({index: idx})
  });
  const data = await res.json();
  if (!res.ok) { showToast(data.error, false); return; }
  showToast('Order cancelled');
  fetchMe();
}

async function fetchLeaderboard() {
  const users = await fetch('/api/leaderboard').then(r => r.json());
  const el = document.getElementById('leaderboard');
  if (!users.length) { el.textContent = 'No traders yet.'; return; }
  el.innerHTML = users.slice(0,15).map((u,i) => {
    let name, sub;
    if (u.is_company) {
      name = `🏢 ${u.username}`;
      sub = `CEO: ${u.ceo || 'Unknown'} · ${u.shares} SUS`;
    } else {
      name = (u.id === myUserId ? '⭐ ' + (u.username || 'You') : (u.username || 'Trader #'+u.id.slice(-4))) + (u.verified ? ' <span style="color:#57f287">✓</span>' : '') + (u.credit_emoji ? ' ' + u.credit_emoji : '');
      sub = `${u.pnl>=0?'+':''}${fmt(u.pnl)} · ${u.shares} shares`;
    }
    return `<div class="lb-row ${u.id === myUserId ? 'lb-me' : ''}">
      <div class="lb-rank">${medals[i] || '#'+(i+1)}</div>
      <div style="flex:1;min-width:0">
        <div class="lb-name">${name}</div>
        <div class="lb-pnl ${u.is_company ? '' : (u.pnl>=0?'pos':'neg')}" style="${u.is_company?'color:var(--muted)':''}">${sub}</div>
      </div>
      <div class="lb-worth">${fmt(u.net_worth)}</div>
    </div>`;
  }).join('');
  document.getElementById('lb-updated').textContent = 'Updated: ' + new Date().toLocaleTimeString();
}

// ── Chat ──────────────────────────────────────────────────────────────────────
let lastMsgId = 0;
let isLoggedIn = false;
let zoomPoints = 10; // default 5m (10 × 30s ticks)
let fullHistory = [];
let fullTimestamps = [];
let myAvgCost = 0;

function costLineDataset(len) {
  if (!myAvgCost || myAvgCost <= 0) return null;
  return { type: 'line', label: 'Your avg buy', data: new Array(len).fill(myAvgCost),
    borderColor: '#fee75c', borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, fill: false };
}

function setZoom(points, label) {
  zoomPoints = points;
  document.querySelectorAll('.zoom-btn').forEach(b => b.classList.toggle('active', b.dataset.z === label));
  renderChart();
}

function renderChart() {
  const h = zoomPoints > 0 ? fullHistory.slice(-zoomPoints) : fullHistory;
  const t = zoomPoints > 0 ? fullTimestamps.slice(-zoomPoints) : fullTimestamps;
  if (!h.length) return;
  if (chartMode === 'candle') {
    ensureChart('bar');
    const candles = buildCandles(h, t);
    chart.data.labels = candles.map(c => c.label);
    chart.data.datasets = [
      { label: 'wick', data: candles.map(c => [c.lo, c.hi]), backgroundColor: '#888',
        barThickness: 2, grouped: false, categoryPercentage: 1, barPercentage: 1 },
      { label: 'body', data: candles.map(c => [Math.min(c.o, c.c), Math.max(c.o, c.c)]),
        backgroundColor: candles.map(c => c.c >= c.o ? '#57f287' : '#ed4245'),
        grouped: false, categoryPercentage: 1, barPercentage: 0.7 },
    ];
    const cl = costLineDataset(candles.length);
    if (cl) chart.data.datasets.push(cl);
    // Zoom y-axis to the price range (don't start at $0), include cost line
    let lo = Math.min(...candles.map(c => c.lo));
    let hi = Math.max(...candles.map(c => c.hi));
    if (myAvgCost > 0) { lo = Math.min(lo, myAvgCost); hi = Math.max(hi, myAvgCost); }
    const pad = Math.max((hi - lo) * 0.1, hi * 0.01);
    chart.options.scales.y.min = Math.max(0, lo - pad);
    chart.options.scales.y.max = hi + pad;
    chart.update();
  } else {
    ensureChart('line');
    chart.options.scales.y.min = undefined;
    chart.options.scales.y.max = undefined;
    const up = h.length < 2 || h[h.length-1] >= h[0];
    chart.data.labels = t.length ? t : h.map((_,i) => i);
    chart.data.datasets = [{ data: h, borderColor: up ? '#57f287' : '#ed4245',
      backgroundColor: up ? 'rgba(87,242,135,0.08)' : 'rgba(237,66,69,0.08)',
      borderWidth: 2, pointRadius: 0, fill: true, tension: 0.3 }];
    const cl = costLineDataset(chart.data.labels.length);
    if (cl) chart.data.datasets.push(cl);
    chart.update();
  }
}

function tsToTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

function appendMessage(m, scroll=true) {
  const el = document.getElementById('chat-messages');
  const isMe = m.user_id === myUserId;
  const avatarUrl = m.avatar
    ? `https://cdn.discordapp.com/avatars/${m.user_id}/${m.avatar}.png?size=32`
    : `https://cdn.discordapp.com/embed/avatars/0.png`;
  const div = document.createElement('div');
  div.className = 'chat-msg' + (isMe ? ' me' : '');
  div.innerHTML = `
    <img class="chat-avatar" src="${avatarUrl}" alt=""/>
    <div class="chat-bubble">
      <div class="chat-name">${m.username}</div>
      <div class="chat-text">${m.text.replace(/</g,'&lt;')}</div>
      <div class="chat-time">${tsToTime(m.ts)}</div>
    </div>`;
  el.appendChild(div);
  if (scroll) el.scrollTop = el.scrollHeight;
  lastMsgId = Math.max(lastMsgId, m.id);
}

async function fetchChat() {
  const msgs = await fetch(`/api/chat?after=${lastMsgId}`).then(r => r.json()).catch(() => []);
  msgs.forEach(m => appendMessage(m));
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  const res = await fetch('/api/chat/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text})
  });
  if (!res.ok) { showToast('Could not send message', false); return; }
  const d = await res.json();
  appendMessage(d.message);
}

function initChat() {
  if (!isLoggedIn) {
    document.getElementById('chat-input-area').style.display = 'none';
    document.getElementById('chat-login-prompt').style.display = 'block';
  }
  fetchChat();
  setInterval(fetchChat, 2000);
}

fetchStock(); fetchLeaderboard();
fetchMe().then(() => initChat()).catch(() => initChat());
setInterval(fetchStock, 10000);
setInterval(fetchMe, 15000);

// ── History drawer ─────────────────────────────────────────────────────────────
let historyOpen = false;
function toggleHistory() {
  if (!myUserId) { showToast('Login first', false); return; }
  historyOpen = !historyOpen;
  const drawer = document.getElementById('history-drawer');
  const overlay = document.getElementById('history-overlay');
  drawer.style.left = historyOpen ? '0' : '-440px';
  overlay.style.display = historyOpen ? 'block' : 'none';
  setTimeout(() => { overlay.style.opacity = historyOpen ? '1' : '0'; }, 10);
  if (historyOpen) loadHistory();
}

const HIST_ICONS = {buy:'📈',sell:'📉',short:'🐻',cover:'🔄',company_buy:'🏢',company_sell:'🏢',send:'📤',receive:'📥',dividend:'💵'};
async function loadHistory() {
  const list = document.getElementById('history-list');
  const hist = await fetch('/api/history').then(r => r.ok ? r.json() : []).catch(() => []);
  if (!hist.length) { list.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:20px 0">No transactions yet.</div>'; return; }
  list.innerHTML = hist.map(h => {
    const t = new Date(h.ts * 1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
    const amtColor = h.amount > 0 ? 'var(--green)' : (h.amount < 0 ? 'var(--red)' : 'var(--muted)');
    const amtStr = h.amount !== 0 ? `${h.amount > 0 ? '+' : ''}${fmt(h.amount)}` : '';
    return `<div style="display:flex;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid var(--border)">
      <span style="font-size:18px">${HIST_ICONS[h.kind] || '•'}</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:13px">${h.detail}</div>
        <div style="font-size:10px;color:var(--muted)">${t}</div>
      </div>
      <div style="font-weight:700;font-size:13px;color:${amtColor}">${amtStr}</div>
    </div>`;
  }).join('');
}

// ── Companies drawer ───────────────────────────────────────────────────────────
const COMPANY_TYPES_MAP ={"hedge_fund":{"name":"Hedge Fund","emoji":"💼","desc":"Pool money and trade SUS together."},"day_trading":{"name":"Day Trading LLC","emoji":"⚡","desc":"Members vote every hour on buy/sell."},"index_fund":{"name":"Index Fund","emoji":"📊","desc":"Auto-buys SUS every 20min."},"insider_ring":{"name":"Insider Trading Ring","emoji":"🔍","desc":"Members see news early."},"short_cartel":{"name":"Short Selling Cartel","emoji":"🐻","desc":"Coordinated shorts hit 2× harder."},"pump_dump":{"name":"Pump & Dump Crew","emoji":"🚀","desc":"Mass buys spike the price 2×."},"lending_bank":{"name":"Lending Bank","emoji":"🏦","desc":"Lend cash at interest."},"invest_bank":{"name":"Investment Bank","emoji":"💳","desc":"Earn 3% commission on stock trades."},"savings":{"name":"Savings Account","emoji":"🐷","desc":"Earn 1% per hour on deposits."},"insurance":{"name":"Insurance Company","emoji":"🛡️","desc":"Pay out if portfolio drops 20%+."},"bounty_hunter":{"name":"Bounty Hunter","emoji":"🎯","desc":"Post bounties on players."},"market_maker":{"name":"Market Maker","emoji":"⚖️","desc":"Set buy/sell spread for users."},"sus_mafia":{"name":"Sus Mafia","emoji":"🤌","desc":"Charge protection from companies."},"wolf_pack":{"name":"Wolf Pack","emoji":"🐺","desc":"Mass buy amplifies price 3×."},"casino":{"name":"Casino","emoji":"🎰","desc":"Players gamble against your treasury."},"analyst_firm":{"name":"Analyst Firm","emoji":"📋","desc":"Sell Buy/Sell/Hold ratings as a subscription."},"copy_trading":{"name":"Copy Trading","emoji":"🔁","desc":"Subscribers auto-mirror your SUS trades."}};
let companiesOpen = false;
let detailOpen = false;
let dcSelectedType = null;
let drawerCompanies = [];

function toggleCompanies() {
  companiesOpen = !companiesOpen;
  const drawer = document.getElementById('companies-drawer');
  const overlay = document.getElementById('companies-overlay');
  drawer.style.right = companiesOpen ? '0' : '-520px';
  overlay.style.display = companiesOpen ? 'block' : 'none';
  setTimeout(() => { overlay.style.opacity = companiesOpen ? '1' : '0'; }, 10);
  if (companiesOpen) { loadDrawerCompanies(); closeDetail(); }
}

function closeDetail() {
  detailOpen = false;
  document.getElementById('company-detail-panel').style.right = '-520px';
}

async function loadDrawerCompanies() {
  drawerCompanies = await fetch('/api/companies').then(r => r.json()).catch(() => []);
  const el = document.getElementById('drawer-companies-list');
  if (!drawerCompanies.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:20px 0">No companies yet. Be the first!</div>';
    return;
  }
  el.innerHTML = drawerCompanies.map(c => {
    const t = COMPANY_TYPES_MAP[c.type] || {name:c.type, emoji:'🏢'};
    return `<div onclick="openDrawerCompany('${c.id}')" style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:10px;cursor:pointer;transition:border-color .2s" onmouseover="this.style.borderColor='var(--accent)'" onmouseout="this.style.borderColor='var(--border)'">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <span style="font-size:22px">${t.emoji}</span>
        <div style="flex:1">
          <div style="font-weight:700;font-size:14px">${c.name} ${c.marketing ? '<span style="font-size:9px;background:#fee75c22;color:#fee75c;padding:1px 5px;border-radius:999px;font-weight:700">📣 PROMOTED</span>' : ''}</div>
          <div style="font-size:11px;color:var(--accent);font-weight:700">${c.ticker} · ${t.name}</div>
        </div>
        <div style="text-align:right">
          <div style="font-size:16px;font-weight:800">${fmt(c.stock_price)}</div>
          <div style="font-size:10px;color:var(--muted)">per share</div>
        </div>
      </div>
      <div style="display:flex;gap:12px;font-size:12px;color:var(--muted);flex-wrap:wrap">
        <span>Treasury: <b style="color:var(--text)">${fmt(c.treasury)}</b></span>
        <span>SUS: <b style="color:var(--text)">${c.sus_shares || 0}</b></span>
        <span>Members: <b style="color:var(--text)">${c.member_count}</b></span>
      </div>
      ${c.description ? `<div style="font-size:11px;color:var(--muted);margin-top:6px">${c.description}</div>` : ''}
    </div>`;
  }).join('');
}

async function openDrawerCompany(cid) {
  const c = await fetch('/api/companies/' + cid).then(r => r.json());
  const t = COMPANY_TYPES_MAP[c.type] || {name:c.type, emoji:'🏢'};
  const isMember = c._is_member;
  const isCeo = c._is_ceo;

  document.getElementById('company-detail-inner').innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
      <button onclick="closeDetail()" style="background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer">←</button>
      <span style="font-size:22px">${t.emoji}</span>
      <div>
        <div style="font-size:17px;font-weight:700">${c.name}</div>
        <div style="font-size:11px;color:var(--accent);font-weight:700">${c.ticker} · ${t.name}</div>
      </div>
    </div>
    ${c.description ? `<div style="font-size:12px;color:var(--muted);margin-bottom:14px">${c.description}</div>` : ''}

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px">
      ${[['Stock Price', fmt(c._stock_price)], ['Company Value', fmt(c._value)], ['Treasury', fmt(c.treasury)], ['SUS Held', (c.sus_shares || 0) + ' shares'], ['Your Shares', c._my_shares || 0]].map(([l,v]) =>
        `<div style="background:var(--surface);border-radius:8px;padding:10px 12px"><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px">${l}</div><div style="font-size:16px;font-weight:700">${v}</div></div>`
      ).join('')}
    </div>

    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Company Stock</div>
      <div style="display:flex;gap:6px">
        <input type="number" id="d-stock-shares" class="trade-input" placeholder="Shares" style="flex:1;margin-bottom:0"/>
        <button class="btn btn-buy" onclick="dBuyStock('${cid}')">Buy</button>
        <button class="btn btn-sell" onclick="dSellStock('${cid}')">Sell</button>
      </div>
    </div>

    ${isMember ? `
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Treasury</div>
      <div style="display:flex;gap:6px">
        <input type="number" id="d-dep-amount" class="trade-input" placeholder="Amount" style="flex:1;margin-bottom:0"/>
        <button class="btn btn-buy" onclick="dDeposit('${cid}')">Deposit</button>
        <button class="btn btn-sell" onclick="dWithdraw('${cid}')">Withdraw</button>
      </div>
    </div>` : `<button class="btn btn-discord" style="width:100%;margin-bottom:12px" onclick="dJoin('${cid}')">Join Company</button>`}

    ${isCeo ? `
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">CEO — Edit Description</div>
      <div style="display:flex;gap:6px">
        <input type="text" id="d-desc-input" class="trade-input" placeholder="New description" value="${(c.description||'').replace(/"/g,'&quot;')}" maxlength="200" style="flex:1;margin-bottom:0"/>
        <button class="btn btn-discord" onclick="dSetDescription('${cid}')">Save</button>
      </div>
    </div>
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">📢 Post Announcement (1/hour, public)</div>
      <input type="text" id="d-news-input" class="trade-input" placeholder="Headline everyone will see..." maxlength="140"/>
      <div style="display:flex;gap:6px">
        <button class="btn btn-buy" style="flex:1" onclick="dPostNews('${cid}', true)">📈 Good News</button>
        <button class="btn btn-sell" style="flex:1" onclick="dPostNews('${cid}', false)">📉 Bad News</button>
      </div>
    </div>
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">🎁 Grant Free Access</div>
      <div style="display:flex;gap:6px">
        <select id="d-free-input" class="trade-input" style="flex:1;margin-bottom:0"><option value="">Select a user...</option></select>
        <button class="btn btn-discord" onclick="dGrantFree('${cid}')">Grant</button>
      </div>
      ${(c.type !== 'insider_ring' && (c._free_access_list||[]).length) ? `<div style="margin-top:6px">${c._free_access_list.map(f => `
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;padding:4px 0">
            <span>🎁 ${f.name}</span>
            <button onclick="dRevokeFree('${cid}','${f.id}')" style="background:none;border:none;color:var(--red);cursor:pointer;font-size:11px">✕ Revoke</button>
          </div>`).join('')}</div>` : ''}
    </div>
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">CEO — Trade SUS (holds ${c.sus_shares||0} shares)</div>
      <div style="display:flex;gap:6px">
        <input type="number" id="d-sus-shares" class="trade-input" placeholder="SUS shares" style="flex:1;margin-bottom:0"/>
        <button class="btn btn-buy" onclick="dTradeSus('${cid}','buy')">Buy SUS</button>
        <button class="btn btn-sell" onclick="dTradeSus('${cid}','sell')">Sell SUS</button>
      </div>
    </div>` : ''}

    ${buildDrawerTypePanel(c, isMember, isCeo)}

    <!-- Investors -->
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">💰 Investors (${(c._investors||[]).length})</div>
      ${(c._investors && c._investors.length) ? c._investors.map(inv => `
        <div style="display:flex;justify-content:space-between;font-size:13px;padding:5px 0;border-bottom:1px solid var(--border)">
          <span>${inv.name}</span>
          <span style="font-weight:700">${inv.shares} shares</span>
        </div>`).join('') : '<div style="color:var(--muted);font-size:12px">No investors yet.</div>'}
    </div>

    <!-- Upgrades -->
    ${isCeo ? `<div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">⚙️ Upgrades</div>
      <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;padding:4px 0">
        <span>🏦 Treasury Vault Lv.${(c.upgrades||{}).vault||0}/5 <span style="color:var(--muted)">+${0.25*((c.upgrades||{}).vault||0)}%/cycle</span></span>
        ${((c.upgrades||{}).vault||0) < 5 ? `<button class="btn btn-buy" style="padding:4px 8px;font-size:11px" onclick="dBuyUpgrade('${cid}','vault')">Upgrade (${fmt(10000*(((c.upgrades||{}).vault||0)+1))})</button>` : '<span style="color:var(--green);font-size:11px">MAX</span>'}
      </div>
      ${(() => {
        const mraw = (c.upgrades||{}).marketing;
        const mlvl = mraw === true ? 1 : (parseInt(mraw)||0);
        const intervalHrs = mlvl > 0 ? (6 - mlvl) : null;
        return `<div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;padding:4px 0">
          <span>📣 Marketing Dept Lv.${mlvl}/5 ${mlvl>0?`<span style="color:var(--muted)">ad every ${intervalHrs}h</span>`:''}</span>
          ${mlvl < 5 ? `<button class="btn btn-buy" style="padding:4px 8px;font-size:11px" onclick="dBuyUpgrade('${cid}','marketing')">${mlvl===0?'Buy':'Upgrade'} (${fmt(15000*(mlvl+1))})</button>` : '<span style="color:var(--green);font-size:11px">MAX</span>'}
        </div>
        ${mlvl > 0 ? `<div style="display:flex;gap:6px;margin-top:4px">
          <input type="text" id="d-ad-input" class="trade-input" placeholder="Your ad text..." value="${(c.ad_text||'').replace(/"/g,'&quot;')}" maxlength="140" style="flex:1;margin-bottom:0;font-size:12px;padding:4px 8px"/>
          <button class="btn btn-discord" style="padding:4px 8px;font-size:11px" onclick="dSetAd('${cid}')">Save Ad</button>
        </div>` : ''}`;
      })()}
    </div>` : ''}

    ${['insider_ring','analyst_firm','copy_trading'].includes(c.type) ? `<div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">🔖 Subscribers (${(c._subscribers_list||[]).length})</div>
      ${(c._subscribers_list && c._subscribers_list.length) ? c._subscribers_list.map(s => `
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;padding:5px 0;border-bottom:1px solid var(--border)">
          <span>${s.free ? '🎁 ' : ''}${s.name}</span>
          ${s.free
            ? `<span style="display:flex;align-items:center;gap:8px"><span style="font-size:11px;color:var(--green);font-weight:700">∞ Free</span>${isCeo ? `<button onclick="dRevokeFree('${cid}','${s.id}')" style="background:none;border:none;color:var(--red);cursor:pointer;font-size:11px">✕ Revoke</button>` : ''}</span>`
            : `<span class="sub-due" data-due="${s.next_due}" style="font-size:11px;color:var(--accent);font-weight:700">next: --</span>`}
        </div>`).join('') : '<div style="color:var(--muted);font-size:12px">No subscribers yet.</div>'}
    </div>` : ''}
  `;

  detailOpen = true;
  document.getElementById('company-detail-panel').style.right = '0';
  populateFreeSelect();
}

let allUsersCache = null;
async function populateFreeSelect() {
  const sel = document.getElementById('d-free-input');
  if (!sel) return;
  if (!allUsersCache) {
    allUsersCache = await fetch('/api/all_users').then(r => r.ok ? r.json() : []).catch(() => []);
  }
  sel.innerHTML = '<option value="">Select a user...</option>' +
    allUsersCache.map(u => `<option value="${u.id}">${u.username}</option>`).join('');
}

function buildDrawerTypePanel(c, isMember, isCeo) {
  const cid = c.id;
  switch(c.type) {
    case 'hedge_fund': return `<div style="margin-bottom:12px;background:var(--surface);border-radius:8px;padding:12px">
      <div style="font-size:12px;font-weight:700;margin-bottom:4px">💼 Hedge Fund</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px">Members deposit cash; the CEO trades SUS with the pooled treasury and distributes profits by deposit share.</div>
      <div style="font-size:12px;margin-bottom:8px">Your deposit: <b>${fmt(c._my_deposit||0)}</b></div>
      ${isCeo ? `<div style="display:flex;gap:6px"><input type="number" id="d-dist-amt" class="trade-input" placeholder="Amount to distribute" style="flex:1;margin-bottom:0"/><button class="btn btn-buy" onclick="dDistribute('${cid}')">Distribute Profits</button></div>` : ''}
    </div>`;
    case 'short_cartel': return `<div style="margin-bottom:12px;background:var(--surface);border-radius:8px;padding:12px">
      <div style="font-size:12px;font-weight:700;margin-bottom:4px">🐻 Short Selling Cartel</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:6px">Members earn a <b>2× bonus</b> on profitable SUS short covers — the extra payout comes from the cartel treasury, so keep it funded via deposits.</div>
      <div style="font-size:12px;color:${isMember?'var(--green)':'var(--muted)'}">${isMember ? '✓ Your shorts are amplified.' : 'Join to amplify your shorts.'}</div>
    </div>`;
    case 'index_fund': return `<div style="margin-bottom:12px;background:var(--surface);border-radius:8px;padding:12px">
      <div style="font-size:12px;font-weight:700;margin-bottom:4px">📊 Index Fund</div>
      <div style="font-size:11px;color:var(--muted)">Automatically buys SUS every 20 min with any idle treasury cash. Deposit and hold — the fund's value tracks the market passively. Currently holds <b>${c.sus_shares||0}</b> SUS.</div>
    </div>`;
    case 'invest_bank': return `<div style="margin-bottom:12px;background:var(--surface);border-radius:8px;padding:12px">
      <div style="font-size:12px;font-weight:700;margin-bottom:4px">💳 Investment Bank</div>
      <div style="font-size:11px;color:var(--muted)">Earns a 3% commission on <b>every company stock trade</b> across the whole market, paid into this treasury automatically. Treasury: <b>${fmt(c.treasury)}</b>.</div>
    </div>`;
    case 'casino': {
      const games = {coinflip:{name:'Coin Flip',emoji:'🪙',desc:'48% → 2×'},highlow:{name:'High Card',emoji:'🃏',desc:'45% → 2.1×'},dice:{name:'Dice Roll',emoji:'🎲',desc:'33% → 2.8×'},roulette:{name:'Roulette',emoji:'🎡',desc:'25% → 3.6×'},slots:{name:'Slots',emoji:'🎰',desc:'10% → 8×'}};
      return `<div style="margin-bottom:12px;background:var(--surface);border-radius:8px;padding:12px">
      <div style="font-size:12px;font-weight:700;margin-bottom:4px">🎰 Casino</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px">Pick a game and bet against the house. Bankroll: <b>${fmt(c.treasury)}</b></div>
      <div id="casino-result" style="font-size:15px;font-weight:800;text-align:center;margin-bottom:8px;min-height:22px"></div>
      <input type="number" id="d-bet" class="trade-input" placeholder="Bet amount" min="1"/>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
        ${Object.entries(games).map(([k,g]) => `<button class="btn btn-buy" style="flex-direction:column;padding:8px;font-size:12px" onclick="dGamble('${cid}','${k}')">${g.emoji} ${g.name}<span style="font-size:9px;color:var(--muted);font-weight:400">${g.desc}</span></button>`).join('')}
      </div>
    </div>`;
    }
    case 'lending_bank': return `<div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Loans (8% interest)</div>
      ${c._my_loan ? `<div style="color:var(--red);margin-bottom:6px;font-size:13px">You owe: ${fmt(c._my_loan.due)}</div><button class="btn btn-sell" style="width:100%" onclick="dRepayLoan('${cid}')">Repay Loan</button>`
      : `<input type="number" id="d-loan-amt" class="trade-input" placeholder="Loan amount"/><button class="btn btn-discord" style="width:100%" onclick="dRequestLoan('${cid}')">Request Loan</button>`}
    </div>`;
    case 'savings': {
      const owed = Object.values(c.deposits || {}).reduce((s, v) => s + v, 0);
      const profit = (c._value || 0) - owed;
      return `<div style="margin-bottom:12px;background:var(--surface);border-radius:8px;padding:12px">
      <div style="font-size:12px;font-weight:700;margin-bottom:4px">🐷 Savings — 1% per hour</div>
      <div style="font-size:13px;color:var(--green)">Your deposit: ${fmt(c._my_deposit||0)}</div>
      <div style="font-size:11px;color:var(--muted)">Deposit to earn interest. The CEO invests the pooled deposits and keeps the gains.</div>
      ${isCeo ? `<div style="margin-top:8px;border-top:1px solid var(--border);padding-top:8px">
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px">💰 Bank Profit Split</div>
        <div style="display:flex;justify-content:space-between;font-size:12px"><span>Bank value</span><b>${fmt(c._value||0)}</b></div>
        <div style="display:flex;justify-content:space-between;font-size:12px"><span>Owed to depositors</span><b style="color:var(--red)">${fmt(owed)}</b></div>
        <div style="display:flex;justify-content:space-between;font-size:13px;font-weight:700;margin-top:2px"><span>Your profit</span><span style="color:${profit>=0?'var(--green)':'var(--red)'}">${profit>=0?'+':''}${fmt(profit)}</span></div>
      </div>` : ''}
    </div>`;
    }
    case 'insurance': return `<div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Insurance</div>
      ${c._my_policy ? `<div style="color:var(--green);font-size:13px">Covered: ${fmt(c._my_policy.coverage)}</div>`
      : `<input type="number" id="d-premium" class="trade-input" placeholder="Premium ($50 min)" value="50"/><button class="btn btn-discord" style="width:100%" onclick="dBuyInsurance('${cid}')">Buy Coverage</button>`}
    </div>`;
    case 'bounty_hunter': return `<div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Post Bounty (30% to company)</div>
      <input id="d-bounty-target" class="trade-input" placeholder="Target User ID"/>
      <input type="number" id="d-bounty-amt" class="trade-input" placeholder="Bounty amount"/>
      <button class="btn btn-sell" style="width:100%" onclick="dPostBounty('${cid}')">Post Bounty</button>
      <div style="font-size:11px;color:var(--muted);margin-top:4px">Active: ${(c.bounties||[]).length} bounties</div>
    </div>`;
    case 'market_maker': return `<div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Market Maker · Buy@${fmt(c.spread_sell||0)} Sell@${fmt(c.spread_buy||0)}</div>
      ${isCeo ? `<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:6px"><input type="number" id="d-spread-buy" class="trade-input" placeholder="Buy price" style="margin-bottom:0"/><input type="number" id="d-spread-sell" class="trade-input" placeholder="Sell price" style="margin-bottom:0"/></div><button class="btn btn-discord" style="width:100%;margin-bottom:8px" onclick="dSetSpread('${cid}')">Set Spread</button>` : ''}
      <div style="display:flex;gap:6px">
        <input type="number" id="d-mm-shares" class="trade-input" placeholder="Shares" style="flex:1;margin-bottom:0"/>
        <button class="btn btn-buy" onclick="dMmTrade('${cid}','buy')">Buy</button>
        <button class="btn btn-sell" onclick="dMmTrade('${cid}','sell')">Sell</button>
      </div>
    </div>`;
    case 'day_trading': case 'pump_dump': case 'wolf_pack': {
      const vote = c.vote || {};
      return `<div style="margin-bottom:12px">
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Vote</div>
        <div style="display:flex;gap:6px">
          ${['buy','sell','hold'].map(v => `<button class="btn ${v==='buy'?'btn-buy':v==='sell'?'btn-sell':'btn-logout'}" style="flex:1" onclick="dCastVote('${cid}','${v}')">${v.toUpperCase()} (${(vote[v]||[]).length})</button>`).join('')}
        </div>
      </div>`;
    }
    case 'sus_mafia': return `<div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">🤌 Pay Protection</div>
      <input type="number" id="d-prot-amt" class="trade-input" placeholder="Amount" value="100"/>
      <button class="btn btn-sell" style="width:100%" onclick="dPayProtection('${cid}')">Pay Protection</button>
    </div>`;
    case 'insider_ring': {
      const subbed = c.subscribers && c.subscribers[myUserId];
      const price = c.sub_price || 0;
      const subCount = c.subscribers ? Object.keys(c.subscribers).length : 0;
      const early = c.early || 0;
      const lead = 2 + early; // minutes before the public
      let inner = `<div style="font-size:12px;font-weight:700;margin-bottom:6px">🔍 Insider Ring · ${subCount} subscriber(s)</div>`;
      if (isCeo) {
        const upkeep = early * 1500;
        const earlyBtns = [0,1,2,3].map(l => `<button class="zoom-btn" style="${l===early?'background:#5865f2;color:#fff;border-color:#5865f2':''}" onclick="dSetEarly('${cid}',${l})">${2+l} min</button>`).join('');
        inner += `<div style="font-size:11px;color:var(--muted);margin-bottom:4px">You're the owner — you get all news free.</div>
          <div style="display:flex;gap:6px;align-items:center;margin-bottom:10px">
            <input type="number" id="d-subprice" class="trade-input" placeholder="$/hour" value="${price}" style="flex:1;margin-bottom:0"/>
            <button class="btn btn-discord" onclick="dSetSubPrice('${cid}')">Set Price</button>
          </div>
          <div style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px">⏱️ Lead Time</div>
          <div style="font-size:11px;color:var(--muted);margin-bottom:6px">How far ahead of the public your members see news. Faster tiers cost upkeep every 20 min from the treasury.</div>
          <div style="display:flex;gap:4px;margin-bottom:6px">${earlyBtns}</div>
          <div style="font-size:12px;color:${early>0?'var(--red)':'var(--muted)'}">Upkeep: <b>${upkeep>0?fmt(upkeep)+' / 20 min':'free'}</b></div>`;
      } else if (subbed) {
        inner += `<div style="font-size:13px;color:var(--green);margin-bottom:6px">✓ Subscribed — you get news <b>${lead} min</b> early (${fmt(price)}/hr)</div>
          <button class="btn btn-sell" style="width:100%" onclick="dUnsubscribe('${cid}')">Cancel Subscription</button>`;
      } else {
        inner += `<div style="font-size:12px;color:var(--muted);margin-bottom:6px">Subscribe for ${fmt(price)}/hour to get all market news <b>${lead} minutes</b> before everyone else.</div>
          <button class="btn btn-discord" style="width:100%" onclick="dSubscribe('${cid}')">Subscribe — ${fmt(price)}/hr</button>`;
      }
      return `<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:12px">${inner}</div>`;
    }
    case 'analyst_firm': {
      const subbed = c.subscribers && c.subscribers[myUserId];
      const price = c.sub_price || 0;
      const subCount = c.subscribers ? Object.keys(c.subscribers).length : 0;
      let inner = `<div style="font-size:12px;font-weight:700;margin-bottom:6px">📋 Analyst Firm · ${subCount} subscriber(s)</div>`;
      if (isCeo) {
        const cur = c.rating || 'Offline';
        const ratingBtns = ['Offline','Buy','Hold','Sell'].map(r => {
          const active = cur === r;
          const col = r==='Buy'?'btn-buy':r==='Sell'?'btn-sell':'btn-logout';
          return `<button class="btn ${col}" style="flex:1;${active?'outline:2px solid #fff':''}" onclick="dSetRating('${cid}','${r}')">${r}</button>`;
        }).join('');
        inner += `<div style="font-size:11px;color:var(--muted);margin-bottom:4px">Set the rating your subscribers see (current: <b>${cur}</b>):</div>
          <div style="display:flex;gap:4px;margin-bottom:8px">${ratingBtns}</div>
          <div style="display:flex;gap:6px;align-items:center">
            <input type="number" id="d-subprice" class="trade-input" placeholder="$/hour" value="${price}" style="flex:1;margin-bottom:0"/>
            <button class="btn btn-discord" onclick="dSetSubPrice('${cid}')">Set Price</button>
          </div>`;
      } else if (subbed) {
        inner += `<div style="font-size:13px;color:var(--green);margin-bottom:6px">✓ Subscribed — Buy/Sell/Hold ratings show on the market chart (${fmt(price)}/hr)</div>
          <button class="btn btn-sell" style="width:100%" onclick="dUnsubscribe('${cid}')">Cancel Subscription</button>`;
      } else {
        inner += `<div style="font-size:12px;color:var(--muted);margin-bottom:6px">Subscribe for ${fmt(price)}/hour to get live Buy/Sell/Hold ratings on SUS.</div>
          <button class="btn btn-discord" style="width:100%" onclick="dSubscribe('${cid}')">Subscribe — ${fmt(price)}/hr</button>`;
      }
      return `<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:12px">${inner}</div>`;
    }
    case 'copy_trading': {
      const sub = c.subscribers && c.subscribers[myUserId];
      const price = c.sub_price || 0;
      const subCount = c.subscribers ? Object.keys(c.subscribers).length : 0;
      let inner = `<div style="font-size:12px;font-weight:700;margin-bottom:6px">🔁 Copy Trading · ${subCount} subscriber(s)</div>`;
      if (isCeo) {
        inner += `<div style="font-size:11px;color:var(--muted);margin-bottom:4px">Subscribers mirror the SUS trades you make in <b>Trade As Company</b> mode.</div>
          <div style="display:flex;gap:6px;align-items:center">
            <input type="number" id="d-subprice" class="trade-input" placeholder="$/hour" value="${price}" style="flex:1;margin-bottom:0"/>
            <button class="btn btn-discord" onclick="dSetSubPrice('${cid}')">Set Price</button>
          </div>`;
      } else if (sub) {
        const on = sub.copy;
        inner += `<div style="font-size:12px;color:var(--green);margin-bottom:6px">✓ Subscribed (${fmt(price)}/hr)</div>
          <button class="btn ${on?'btn-sell':'btn-buy'}" style="width:100%;margin-bottom:6px" onclick="dCopyToggle('${cid}')">Auto-Copy: ${on?'ON ✅ (click to turn off)':'OFF (click to turn on)'}</button>
          <button class="btn btn-logout" style="width:100%" onclick="dUnsubscribe('${cid}')">Cancel Subscription</button>
          <div style="font-size:10px;color:var(--muted);margin-top:4px">When ON, you auto-buy/sell SUS whenever the company does.</div>`;
      } else {
        inner += `<div style="font-size:12px;color:var(--muted);margin-bottom:6px">Subscribe for ${fmt(price)}/hour, then toggle auto-copy to mirror the company's SUS trades.</div>
          <button class="btn btn-discord" style="width:100%" onclick="dSubscribe('${cid}')">Subscribe — ${fmt(price)}/hr</button>`;
      }
      return `<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:12px">${inner}</div>`;
    }
    default: return '';
  }
}

// Drawer company action helpers
async function dAction(url, body, successMsg, cid) {
  const res = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return false; }
  showToast(successMsg); openDrawerCompany(cid); loadDrawerCompanies(); return true;
}
async function dJoin(cid) { await dAction(`/api/companies/${cid}/join`, {}, 'Joined!', cid); }
async function dBuyStock(cid) { const s=parseInt(document.getElementById('d-stock-shares')?.value); if(!s){showToast('Enter shares',false);return;} await dAction(`/api/companies/${cid}/buy_stock`,{shares:s},`Bought ${s} shares`,cid); }
async function dSellStock(cid) { const s=parseInt(document.getElementById('d-stock-shares')?.value); if(!s){showToast('Enter shares',false);return;} await dAction(`/api/companies/${cid}/sell_stock`,{shares:s},'Sold shares',cid); }
async function dDeposit(cid) { const a=parseFloat(document.getElementById('d-dep-amount')?.value); if(!a){showToast('Enter amount',false);return;} await dAction(`/api/companies/${cid}/deposit`,{amount:a},`Deposited ${fmt(a)}`,cid); }
async function dWithdraw(cid) { const a=parseFloat(document.getElementById('d-dep-amount')?.value); if(!a){showToast('Enter amount',false);return;} await dAction(`/api/companies/${cid}/withdraw`,{amount:a},`Withdrew ${fmt(a)}`,cid); }
async function dTradeSus(cid,action) { const s=parseInt(document.getElementById('d-sus-shares')?.value); if(!s){showToast('Enter shares',false);return;} await dAction(`/api/companies/${cid}/trade_sus`,{action,shares:s},`${action==='buy'?'Bought':'Sold'} ${s} SUS`,cid); }
async function dCastVote(cid,vote) { await dAction(`/api/companies/${cid}/vote`,{vote},`Voted ${vote.toUpperCase()}`,cid); }
async function dRequestLoan(cid) { const a=parseFloat(document.getElementById('d-loan-amt')?.value); if(!a){showToast('Enter amount',false);return;} await dAction(`/api/companies/${cid}/loan`,{amount:a},`Loan received`,cid); }
async function dRepayLoan(cid) { await dAction(`/api/companies/${cid}/repay`,{},'Loan repaid!',cid); }
async function dBuyInsurance(cid) { const p=parseFloat(document.getElementById('d-premium')?.value||50); await dAction(`/api/companies/${cid}/insure`,{premium:p},'Insured!',cid); }
async function dPostBounty(cid) { const t=document.getElementById('d-bounty-target')?.value,a=parseFloat(document.getElementById('d-bounty-amt')?.value); await dAction(`/api/companies/${cid}/bounty`,{target_id:t,amount:a},'Bounty posted!',cid); }
async function dSetSpread(cid) { const b=parseFloat(document.getElementById('d-spread-buy')?.value),s=parseFloat(document.getElementById('d-spread-sell')?.value); await dAction(`/api/companies/${cid}/set_spread`,{buy:b,sell:s},'Spread set!',cid); }
async function dMmTrade(cid,action) { const s=parseInt(document.getElementById('d-mm-shares')?.value); if(!s){showToast('Enter shares',false);return;} await dAction(`/api/companies/${cid}/market_trade`,{action,shares:s},'Trade executed',cid); }
async function dPayProtection(cid) { const a=parseFloat(document.getElementById('d-prot-amt')?.value||100); await dAction(`/api/companies/${cid}/pay_protection`,{amount:a},'Protection paid.',cid); }
async function dGamble(cid, game) {
  const betEl = document.getElementById('d-bet');
  const bet = parseFloat(betEl?.value);
  if (!bet || bet <= 0) { showToast('Enter a bet', false); return; }
  const res = await fetch(`/api/companies/${cid}/gamble`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({bet, game})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  const resEl = document.getElementById('casino-result');
  if (resEl) {
    resEl.textContent = d.win ? `🎉 ${d.game} WIN! +${fmt(d.profit)}` : `💀 ${d.game} — lost ${fmt(d.bet)}`;
    resEl.style.color = d.win ? 'var(--green)' : 'var(--red)';
  }
  showToast(d.win ? `Won ${fmt(d.profit)}!` : `Lost ${fmt(d.bet)}`, d.win);
  loadDrawerCompanies();
  fetchMe();
}
async function dSubscribe(cid) { await dAction(`/api/companies/${cid}/subscribe`,{},'Subscribed to insider ring!',cid); }
async function dUnsubscribe(cid) { await dAction(`/api/companies/${cid}/unsubscribe`,{},'Subscription cancelled',cid); }
async function dSetSubPrice(cid) { const p=parseFloat(document.getElementById('d-subprice')?.value)||0; await dAction(`/api/companies/${cid}/set_sub_price`,{sub_price:p},'Price updated',cid); }
async function dSetRating(cid, rating) { await dAction(`/api/companies/${cid}/set_rating`,{rating},'Rating set to '+rating,cid); }
async function dSetEarly(cid, level) { await dAction(`/api/companies/${cid}/set_early`,{level},'Lead time set to '+(2+level)+' min',cid); }
async function dCopyToggle(cid) { await dAction(`/api/companies/${cid}/copy_toggle`,{},'Auto-copy toggled',cid); }
async function dSetDescription(cid) { const desc=document.getElementById('d-desc-input')?.value||''; await dAction(`/api/companies/${cid}/set_description`,{description:desc},'Description updated',cid); }
async function dBuyUpgrade(cid, upgrade) { await dAction(`/api/companies/${cid}/buy_upgrade`,{upgrade},'Upgrade purchased!',cid); }
async function dSetAd(cid) { const ad=document.getElementById('d-ad-input')?.value||''; await dAction(`/api/companies/${cid}/set_ad`,{ad_text:ad},'Ad saved',cid); }
async function dDistribute(cid) { const a=parseFloat(document.getElementById('d-dist-amt')?.value); if(!a){showToast('Enter amount',false);return;} await dAction(`/api/companies/${cid}/distribute`,{amount:a},'Profits distributed!',cid); }
async function dPostNews(cid, positive) { const h=document.getElementById('d-news-input')?.value.trim(); if(!h){showToast('Enter a headline',false);return;} await dAction(`/api/companies/${cid}/post_news`,{headline:h,positive},'Announcement posted!',cid); }
async function dGrantFree(cid) { const t=document.getElementById('d-free-input')?.value.trim(); if(!t){showToast('Pick a user',false);return;} await dAction(`/api/companies/${cid}/grant_free`,{user_id:t},'Free access granted!',cid); }
async function dRevokeFree(cid, uid) { await dAction(`/api/companies/${cid}/revoke_free`,{user_id:uid},'Free access revoked',cid); }

// Create company
function showCreateDrawer() {
  if (!myUserId) { showToast('Login first', false); return; }
  const grid = document.getElementById('dc-type-grid');
  grid.innerHTML = Object.entries(COMPANY_TYPES_MAP).map(([k,v]) =>
    `<div onclick="selectDcType('${k}',this)" style="background:var(--surface2);border:2px solid var(--border);border-radius:8px;padding:8px;cursor:pointer;text-align:center">
      <div style="font-size:20px">${v.emoji}</div>
      <div style="font-size:11px;font-weight:700;margin-top:3px">${v.name}</div>
      <div style="font-size:9px;color:var(--muted);margin-top:2px">${v.desc}</div>
    </div>`).join('');
  document.getElementById('create-company-modal').style.display = 'flex';
}
function hideCreateDrawer() { document.getElementById('create-company-modal').style.display = 'none'; }
function selectDcType(type, el) {
  document.querySelectorAll('#dc-type-grid > div').forEach(e => e.style.borderColor='var(--border)');
  el.style.borderColor = 'var(--accent)';
  document.getElementById('dc-type').value = type; dcSelectedType = type;
  document.getElementById('dc-subprice-wrap').style.display = ['insider_ring','analyst_firm','copy_trading'].includes(type) ? 'block' : 'none';
}
async function submitCreateDrawer() {
  const name=document.getElementById('dc-name').value.trim();
  const ticker=document.getElementById('dc-ticker').value.trim().toUpperCase();
  const desc=document.getElementById('dc-desc').value.trim();
  const type=document.getElementById('dc-type').value;
  const sub_price=parseFloat(document.getElementById('dc-subprice')?.value) || 0;
  if(!name||!ticker||!type){showToast('Fill all fields and pick a type',false);return;}
  const res=await fetch('/api/companies/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,ticker,description:desc,type,sub_price})});
  const d=await res.json();
  if(!res.ok){showToast(d.error,false);return;}
  showToast(`${name} founded!`); hideCreateDrawer(); loadDrawerCompanies();
}
setInterval(fetchLeaderboard, 15000);

// ── Admin panel ────────────────────────────────────────────────────────────────
let adminOpen = false;
function toggleAdmin() {
  adminOpen = !adminOpen;
  document.getElementById('admin-area').style.display = adminOpen ? 'block' : 'none';
  document.querySelector('#admin-toggle button').textContent = adminOpen ? '🛡️ Hide Admin Panel' : '🛡️ Admin Panel';
}
async function loadAdmin() {
  const res = await fetch('/api/admin/users');
  if (!res.ok) return;
  const users = await res.json();

  let html = `
    <div class="card">
      <div class="card-title" style="color:#ed4245">🛡️ Admin Panel</div>

      <div style="margin-bottom:14px">
        <div class="stat-label" style="margin-bottom:6px">Set Stock Price</div>
        <div style="display:flex;gap:8px">
          <input id="adm-price" type="number" class="trade-input" placeholder="New price..." style="max-width:160px"/>
          <button class="btn btn-sell" onclick="adminSetPrice()">Set Price</button>
        </div>
      </div>

      <div style="margin-bottom:14px">
        <div class="stat-label" style="margin-bottom:6px">Market News</div>
        <button class="btn btn-buy" onclick="adminFireNews()">📰 Fire Random News</button>
      </div>

      <div style="margin-bottom:14px">
        <div class="stat-label" style="margin-bottom:6px">Lottery</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <button class="btn btn-buy" onclick="adminLottery('start')">🎟️ Start Round</button>
          <button class="btn btn-discord" onclick="adminLottery('draw')">🎲 Draw Now</button>
          <button class="btn btn-sell" onclick="adminLottery('cancel')">✖ Cancel (refund)</button>
        </div>
      </div>

      <div style="margin-bottom:14px;border:1px solid #ed424540;border-radius:8px;padding:10px">
        <div class="stat-label" style="margin-bottom:6px;color:#ed4245">⚠️ Danger Zone</div>
        <button class="btn btn-sell" style="width:100%;background:#ed424540" onclick="adminResetMarket()">🔄 Reset Entire Market</button>
        <div style="font-size:10px;color:var(--muted);margin-top:4px">Resets everyone to $1,000 / 0 shares, wipes the price, all companies, shorts, loans, and news. Keeps verified status and bans.</div>
      </div>

      <div class="stat-label" style="margin-bottom:8px">Users</div>
      <div id="adm-users">`;

  users.forEach(u => {
    html += `
      <div style="background:var(--surface2);border-radius:8px;padding:10px 12px;margin-bottom:8px">
        <div style="font-weight:700;margin-bottom:6px">${u.username} <span style="color:var(--muted);font-size:11px">#${u.id.slice(-4)}</span> ${u.verified ? '<span style="font-size:9px;font-weight:700;background:#57f28722;color:var(--green);padding:1px 6px;border-radius:999px">✓ VERIFIED</span>' : ''}</div>
        <div style="font-size:12px;color:var(--muted);margin-bottom:8px">Cash: ${fmt(u.cash)} · Shares: ${u.shares} · NW: ${fmt(u.net_worth)}</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">
          <input id="shares-${u.id}" type="number" class="trade-input" placeholder="Shares (neg=remove)" style="width:150px;font-size:12px;padding:5px 8px"/>
          <button class="btn btn-buy" style="font-size:12px;padding:5px 10px" onclick="adminGiveShares('${u.id}')">± Shares</button>
          <input id="cash-${u.id}" type="number" class="trade-input" placeholder="Cash (neg=remove)" style="width:150px;font-size:12px;padding:5px 8px"/>
          <button class="btn btn-buy" style="font-size:12px;padding:5px 10px;background:#fee75c22;color:#fee75c;border-color:#fee75c40" onclick="adminGiveCash('${u.id}')">± Cash</button>
          <button class="btn ${u.verified ? 'btn-sell' : 'btn-buy'}" style="font-size:12px;padding:5px 10px" onclick="adminVerify('${u.id}', ${!u.verified})">${u.verified ? 'Unverify' : '✓ Verify'}</button>
          <button class="btn btn-sell" style="font-size:12px;padding:5px 10px" onclick="adminReset('${u.id}', '${u.username}')">Reset</button>
          ${u.banned
            ? `<button class="btn btn-buy" style="font-size:12px;padding:5px 10px" onclick="adminBan('${u.id}',0,'${u.username}')">Unban</button>`
            : `<button class="btn btn-sell" style="font-size:12px;padding:5px 10px" onclick="adminBan('${u.id}',60,'${u.username}')">Ban 1h</button>
               <button class="btn btn-sell" style="font-size:12px;padding:5px 10px;background:#ed424540" onclick="adminBan('${u.id}',-1,'${u.username}')">Ban Perm</button>`}
        </div>
        ${u.banned ? '<div style="font-size:10px;color:var(--red);font-weight:700;margin-top:4px">🚫 BANNED</div>' : ''}
      </div>`;
  });

  html += `</div></div>`;
  document.getElementById('admin-area').innerHTML = html;
}

async function adminSetPrice() {
  const price = parseFloat(document.getElementById('adm-price').value);
  if (!price || price <= 0) { showToast('Enter a valid price', false); return; }
  const res = await fetch('/api/admin/set_price', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({price}) });
  const d = await res.json();
  if (d.ok) { showToast(`Price set to ${fmt(price)}`); fetchStock(); loadAdmin(); }
}

async function adminGiveShares(uid) {
  const shares = parseInt(document.getElementById('shares-'+uid).value);
  if (!shares || isNaN(shares)) { showToast('Enter a number of shares', false); return; }
  const res = await fetch('/api/admin/give_shares', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({user_id: uid, shares}) });
  const d = await res.json();
  if (d.ok) { showToast(`Updated shares`); loadAdmin(); }
}

async function adminGiveCash(uid) {
  const amount = parseFloat(document.getElementById('cash-'+uid).value);
  if (isNaN(amount)) { showToast('Enter a cash amount', false); return; }
  const res = await fetch('/api/admin/give_cash', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({user_id: uid, amount}) });
  const d = await res.json();
  if (d.ok) { showToast(`Updated cash`); loadAdmin(); }
}

async function adminReset(uid, name) {
  if (!confirm(`Reset ${name} to $1000 and 0 shares?`)) return;
  const res = await fetch('/api/admin/reset_user', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({user_id: uid}) });
  const d = await res.json();
  if (d.ok) { showToast(`Reset ${name}`); loadAdmin(); }
}

async function adminVerify(uid, verified) {
  const res = await fetch('/api/admin/verify', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({user_id: uid, verified}) });
  const d = await res.json();
  if (d.ok) { showToast(verified ? 'User verified' : 'User unverified'); loadAdmin(); }
}

async function adminBan(uid, minutes, name) {
  if (minutes === -1 && !confirm(`Permanently ban ${name}?`)) return;
  const res = await fetch('/api/admin/ban', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({user_id: uid, minutes}) });
  const d = await res.json();
  if (d.ok) { showToast(`${name} ${d.result}`); loadAdmin(); }
}

async function adminLottery(action) {
  if (action === 'cancel' && !confirm('Cancel the current lottery and refund all tickets?')) return;
  const res = await fetch('/api/admin/lottery', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action}) });
  const d = await res.json();
  if (d.ok) { showToast('Lottery ' + action + ' done'); }
  else { showToast(d.error || 'Failed', false); }
}

async function adminFireNews() {
  const res = await fetch('/api/admin/fire_news', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' });
  const d = await res.json();
  if (d.ok) { showToast(`📰 ${d.impact > 0 ? '+' : ''}${d.impact}% — ${d.headline}`); fetchStock(); }
  else { showToast(d.error || 'Failed', false); }
}

async function adminResetMarket() {
  if (!confirm('Reset the ENTIRE market? This wipes everyone\\'s money, all companies, and the stock price.')) return;
  if (!confirm('Are you absolutely sure? This cannot be undone.')) return;
  const res = await fetch('/api/admin/reset_market', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' });
  const d = await res.json();
  if (d.ok) { showToast('Market reset complete'); loadAdmin(); fetchMe(); fetchStock(); fetchLeaderboard(); }
  else { showToast(d.error || 'Reset failed', false); }
}
</script>
</body>
</html>
"""

COMPANIES_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Sus Stock — Companies</title>
<style>
  :root { --bg:#1e1f22;--surface:#2b2d31;--surface2:#313338;--accent:#5865f2;--green:#57f287;--red:#ed4245;--text:#dbdee1;--muted:#949ba4;--border:#3a3c40; }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;min-height:100vh}
  header{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 28px;display:flex;align-items:center;gap:16px}
  header h1{font-size:20px;font-weight:700}
  .nav-link{color:var(--muted);text-decoration:none;font-size:13px;font-weight:600}
  .nav-link:hover{color:var(--text)}
  .auth-area{margin-left:auto;display:flex;align-items:center;gap:10px}
  .btn{padding:7px 16px;border-radius:8px;font-size:13px;font-weight:600;border:none;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:6px}
  .btn-primary{background:var(--accent);color:#fff}
  .btn-primary:hover{background:#4752c4}
  .btn-green{background:#57f28722;color:var(--green);border:1px solid #57f28740}
  .btn-red{background:#ed424522;color:var(--red);border:1px solid #ed424540}
  .btn-muted{background:var(--surface2);color:var(--muted)}
  .avatar{width:30px;height:30px;border-radius:50%}
  .container{max-width:1200px;margin:0 auto;padding:24px 28px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px 24px;margin-bottom:16px}
  .card-title{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:14px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
  .company-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px;cursor:pointer;transition:border-color .2s}
  .company-card:hover{border-color:var(--accent)}
  .company-header{display:flex;align-items:center;gap:12px;margin-bottom:12px}
  .company-emoji{font-size:28px}
  .company-name{font-size:16px;font-weight:700}
  .company-ticker{font-size:12px;color:var(--muted);font-weight:700}
  .company-type{font-size:11px;color:var(--accent);font-weight:700;text-transform:uppercase;letter-spacing:.5px}
  .stat-row{display:flex;justify-content:space-between;font-size:13px;padding:4px 0;border-bottom:1px solid var(--border)}
  .stat-row:last-child{border-bottom:none}
  .stat-label{color:var(--muted)}
  .stat-value{font-weight:700}
  input,select,textarea{background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 12px;font-size:13px;width:100%;outline:none;margin-bottom:8px}
  input:focus,select:focus{border-color:var(--accent)}
  .modal-bg{position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center;z-index:100;display:none}
  .modal{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:28px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto}
  .modal h2{font-size:18px;font-weight:700;margin-bottom:16px}
  .tab-bar{display:flex;gap:4px;margin-bottom:14px;flex-wrap:wrap}
  .tab-btn{background:var(--surface2);border:1px solid var(--border);color:var(--muted);font-size:12px;font-weight:700;padding:5px 12px;border-radius:6px;cursor:pointer}
  .tab-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
  .badge{font-size:10px;font-weight:700;padding:2px 7px;border-radius:999px}
  .toast{position:fixed;bottom:24px;right:24px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 18px;font-size:13px;font-weight:600;opacity:0;transition:opacity .3s;pointer-events:none;z-index:200}
  .toast.show{opacity:1}
  .toast.ok{border-color:var(--green);color:var(--green)}
  .toast.err{border-color:var(--red);color:var(--red)}
  .type-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;margin-bottom:12px}
  .type-option{background:var(--surface2);border:2px solid var(--border);border-radius:8px;padding:10px;cursor:pointer;text-align:center}
  .type-option.selected{border-color:var(--accent)}
  .type-option .emoji{font-size:22px}
  .type-option .tname{font-size:12px;font-weight:700;margin-top:4px}
  .type-option .tdesc{font-size:10px;color:var(--muted);margin-top:2px}
</style>
</head>
<body>
<header>
  <span style="font-size:22px">🏢</span>
  <h1>Companies</h1>
  <a href="/" class="nav-link">← Back to Market</a>
  <div class="auth-area" id="auth-area">
    <a href="/login?next=/companies" class="btn btn-primary">Login with Discord</a>
  </div>
</header>

<div class="container">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
    <div>
      <div style="font-size:22px;font-weight:800">Sus Corp Market</div>
      <div style="color:var(--muted);font-size:13px">Found or join companies. Each has unique mechanics.</div>
    </div>
    <button class="btn btn-primary" onclick="showCreate()">+ Found Company ($2,000)</button>
  </div>

  <div id="companies-grid" class="grid">Loading...</div>
</div>

<!-- Company detail modal -->
<div class="modal-bg" id="detail-modal">
  <div class="modal" id="detail-content"></div>
</div>

<!-- Create company modal -->
<div class="modal-bg" id="create-modal">
  <div class="modal">
    <h2>🏢 Found a Company</h2>
    <input id="c-name" placeholder="Company name"/>
    <input id="c-ticker" placeholder="Ticker (2-4 letters)" maxlength="4" style="text-transform:uppercase"/>
    <textarea id="c-desc" placeholder="Description (optional)" rows="2" style="resize:none"></textarea>
    <div class="card-title">Choose Type</div>
    <div class="type-grid" id="type-grid"></div>
    <input type="hidden" id="c-type"/>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn btn-primary" style="flex:1" onclick="submitCreate()">Found Company — $2,000</button>
      <button class="btn btn-muted" onclick="hideCreate()">Cancel</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const fmt = v => '$' + v.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
let myUserId = null;
let myUsername = null;
let allCompanies = [];
let selectedType = null;

function showToast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show ' + (ok?'ok':'err');
  setTimeout(() => t.className = 'toast', 3000);
}

async function init() {
  const me = await fetch('/api/me').then(r => r.ok ? r.json() : null).catch(() => null);
  if (me) {
    myUserId = me.user_id; myUsername = me.username;
    const avatarUrl = me.avatar ? `https://cdn.discordapp.com/avatars/${me.user_id}/${me.avatar}.png?size=64` : '';
    document.getElementById('auth-area').innerHTML = `${avatarUrl ? `<img src="${avatarUrl}" class="avatar"/>` : ''}<span style="font-weight:600;font-size:13px">${me.username}</span><a href="/logout" class="btn btn-muted" style="font-size:12px;padding:5px 12px">Logout</a><a href="/" class="btn btn-muted" style="font-size:12px;padding:5px 12px">📈 Market</a>`;
  }
  await loadCompanies();
  buildTypeGrid();
}

function buildTypeGrid() {
  const types = COMPANY_TYPES_MAP;
  const grid = document.getElementById('type-grid');
  grid.innerHTML = Object.entries(types).map(([k,v]) => `
    <div class="type-option" onclick="selectType('${k}',this)">
      <div class="emoji">${v.emoji}</div>
      <div class="tname">${v.name}</div>
      <div class="tdesc">${v.desc}</div>
    </div>`).join('');
}

function selectType(type, el) {
  document.querySelectorAll('.type-option').forEach(e => e.classList.remove('selected'));
  el.classList.add('selected'); selectedType = type;
  document.getElementById('c-type').value = type;
}

async function loadCompanies() {
  allCompanies = await fetch('/api/companies').then(r => r.json()).catch(() => []);
  renderCompanies();
}

function renderCompanies() {
  const types = COMPANY_TYPES_MAP;
  const grid = document.getElementById('companies-grid');
  if (!allCompanies.length) { grid.innerHTML = '<div style="color:var(--muted);padding:20px">No companies yet. Be the first to found one!</div>'; return; }
  grid.innerHTML = allCompanies.map(c => {
    const t = types[c.type] || {name: c.type, emoji: '🏢'};
    return `<div class="company-card" onclick="openCompany('${c.id}')">
      <div class="company-header">
        <div class="company-emoji">${t.emoji}</div>
        <div>
          <div class="company-name">${c.name}</div>
          <div class="company-ticker">${c.ticker} · <span class="company-type">${t.name}</span></div>
        </div>
        <div style="margin-left:auto;text-align:right">
          <div style="font-size:18px;font-weight:800">${fmt(c.stock_price)}</div>
          <div style="font-size:11px;color:var(--muted)">per share</div>
        </div>
      </div>
      <div class="stat-row"><span class="stat-label">Treasury</span><span class="stat-value">${fmt(c.treasury)}</span></div>
      <div class="stat-row"><span class="stat-label">Company Value</span><span class="stat-value">${fmt(c.value)}</span></div>
      <div class="stat-row"><span class="stat-label">Members</span><span class="stat-value">${c.member_count}</span></div>
      <div class="stat-row"><span class="stat-label">Description</span><span class="stat-value" style="color:var(--muted);font-size:11px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${c.description || '—'}</span></div>
    </div>`;
  }).join('');
}

async function openCompany(cid) {
  const c = await fetch(`/api/companies/${cid}`).then(r => r.json());
  const types = COMPANY_TYPES_MAP;
  const t = types[c.type] || {name:c.type, emoji:'🏢'};
  const isMember = c._is_member;
  const isCeo = c._is_ceo;

  let typePanel = buildTypePanel(c, isMember, isCeo);

  document.getElementById('detail-content').innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px">
      <div>
        <div style="font-size:24px;font-weight:800">${t.emoji} ${c.name}</div>
        <div style="color:var(--muted);font-size:13px">${c.ticker} · ${t.name}</div>
        <div style="color:var(--muted);font-size:12px;margin-top:4px">${c.description || ''}</div>
      </div>
      <button onclick="closeDetail()" style="background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer">✕</button>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px">
      <div style="background:var(--surface2);border-radius:8px;padding:12px"><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px">Stock Price</div><div style="font-size:20px;font-weight:700">${fmt(c._stock_price)}</div></div>
      <div style="background:var(--surface2);border-radius:8px;padding:12px"><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px">Company Value</div><div style="font-size:20px;font-weight:700">${fmt(c._value)}</div></div>
      <div style="background:var(--surface2);border-radius:8px;padding:12px"><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px">Treasury</div><div style="font-size:16px;font-weight:700">${fmt(c.treasury)}</div></div>
      <div style="background:var(--surface2);border-radius:8px;padding:12px"><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px">Your Shares</div><div style="font-size:16px;font-weight:700">${c._my_shares || 0}</div></div>
    </div>

    <!-- Stock trading -->
    <div style="margin-bottom:16px">
      <div class="card-title">Company Stock</div>
      <div style="display:flex;gap:8px">
        <input type="number" id="stock-shares" placeholder="Shares" min="1" style="flex:1;margin-bottom:0"/>
        <button class="btn btn-green" onclick="buyStock('${cid}')">Buy</button>
        <button class="btn btn-red" onclick="sellStock('${cid}')">Sell</button>
      </div>
    </div>

    <!-- Deposit/Withdraw -->
    ${isMember ? `<div style="margin-bottom:16px">
      <div class="card-title">Treasury</div>
      <div style="display:flex;gap:8px">
        <input type="number" id="dep-amount" placeholder="Amount" style="flex:1;margin-bottom:0"/>
        <button class="btn btn-green" onclick="deposit('${cid}')">Deposit</button>
        <button class="btn btn-red" onclick="withdraw('${cid}')">Withdraw</button>
      </div>
    </div>` : `<button class="btn btn-primary" style="width:100%;margin-bottom:16px" onclick="joinCompany('${cid}')">Join Company</button>`}

    <!-- CEO: Trade SUS with treasury -->
    ${isCeo ? `<div style="margin-bottom:16px">
      <div class="card-title">CEO Controls — Trade SUS</div>
      <div style="display:flex;gap:8px">
        <input type="number" id="sus-shares" placeholder="SUS shares" style="flex:1;margin-bottom:0"/>
        <button class="btn btn-green" onclick="tradeSus('${cid}','buy')">Buy SUS</button>
        <button class="btn btn-red" onclick="tradeSus('${cid}','sell')">Sell SUS</button>
      </div>
      <div style="font-size:11px;color:var(--muted);margin-top:4px">Company holds: ${c.sus_shares || 0} SUS shares</div>
    </div>` : ''}

    <!-- Type-specific panel -->
    ${typePanel}
  `;
  document.getElementById('detail-modal').style.display = 'flex';
}

function buildTypePanel(c, isMember, isCeo) {
  const cid = c.id;
  switch(c.type) {
    case 'lending_bank': return `
      <div><div class="card-title">Loans</div>
      ${c._my_loan ? `<div style="color:var(--red);margin-bottom:8px">You owe: ${fmt(c._my_loan.due)}</div><button class="btn btn-red" style="width:100%" onclick="repayLoan('${cid}')">Repay Loan</button>` :
      `<input type="number" id="loan-amt" placeholder="Loan amount"/><button class="btn btn-primary" style="width:100%" onclick="requestLoan('${cid}')">Request Loan (8% interest)</button>`}</div>`;
    case 'savings': return `
      <div><div class="card-title">Savings (1% per hour)</div>
      <div style="margin-bottom:8px;color:var(--green)">Your deposit: ${fmt(c._my_deposit || 0)}</div>
      <div style="font-size:11px;color:var(--muted)">Deposit into treasury to earn interest automatically.</div></div>`;
    case 'insurance': return `
      <div><div class="card-title">Insurance</div>
      ${c._my_policy ? `<div style="color:var(--green)">Covered: ${fmt(c._my_policy.coverage)}</div>` :
      `<input type="number" id="premium-amt" placeholder="Premium amount ($50 min)" value="50"/><button class="btn btn-primary" style="width:100%" onclick="buyInsurance('${cid}')">Buy Coverage</button>`}</div>`;
    case 'bounty_hunter': return `
      <div><div class="card-title">Post a Bounty</div>
      <input id="bounty-target" placeholder="Target User ID"/>
      <input type="number" id="bounty-amt" placeholder="Bounty amount"/>
      <button class="btn btn-red" style="width:100%" onclick="postBounty('${cid}')">Post Bounty (30% to company)</button>
      <div style="margin-top:8px;font-size:11px;color:var(--muted)">Active bounties: ${(c.bounties||[]).length}</div></div>`;
    case 'market_maker': return `
      <div><div class="card-title">Trade at Spread${isCeo ? ' (CEO sets spread)' : ''}</div>
      ${isCeo ? `<div style="display:flex;gap:8px;margin-bottom:8px"><input type="number" id="spread-buy" placeholder="Buy price" style="flex:1"/><input type="number" id="spread-sell" placeholder="Sell price" style="flex:1"/></div><button class="btn btn-primary" onclick="setSpread('${cid}')" style="width:100%;margin-bottom:8px">Set Spread</button>` : ''}
      <div style="margin-bottom:8px;font-size:13px">Buy @ ${fmt(c.spread_sell||0)} · Sell @ ${fmt(c.spread_buy||0)}</div>
      <div style="display:flex;gap:8px"><input type="number" id="mm-shares" placeholder="Shares" style="flex:1;margin-bottom:0"/>
      <button class="btn btn-green" onclick="mmTrade('${cid}','buy')">Buy</button>
      <button class="btn btn-red" onclick="mmTrade('${cid}','sell')">Sell</button></div></div>`;
    case 'day_trading': case 'pump_dump': case 'wolf_pack': {
      const vote = c.vote || {};
      const total = (vote.buy||[]).length + (vote.sell||[]).length + (vote.hold||[]).length;
      return `<div><div class="card-title">Vote</div>
        <div style="display:flex;gap:8px">${['buy','sell','hold'].map(v => `<button class="btn ${v==='buy'?'btn-green':v==='sell'?'btn-red':'btn-muted'}" style="flex:1" onclick="castVote('${cid}','${v}')">${v.toUpperCase()} (${(vote[v]||[]).length})</button>`).join('')}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:6px">${total} votes cast · Executes when majority reached</div></div>`;
    }
    case 'sus_mafia': return `
      <div><div class="card-title">🤌 Pay Protection</div>
      <input type="number" id="prot-amt" placeholder="Amount" value="100"/>
      <button class="btn btn-red" style="width:100%" onclick="payProtection('${cid}')">Pay Protection</button>
      <div style="font-size:11px;color:var(--muted);margin-top:4px">The mafia appreciates your cooperation.</div></div>`;
    case 'insider_ring': return `
      <div><div class="card-title">🔍 Insider Feed</div>
      ${isMember ? `<div style="font-size:13px;color:var(--green)">You receive news 2 minutes early. Check the market news panel.</div>` : '<div style="color:var(--muted);font-size:13px">Join to access early news.</div>'}</div>`;
    default: return '';
  }
}

function closeDetail() { document.getElementById('detail-modal').style.display = 'none'; }

async function joinCompany(cid) {
  const res = await fetch(`/api/companies/${cid}/join`, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast('Joined company!'); openCompany(cid); loadCompanies();
}

async function buyStock(cid) {
  const shares = parseInt(document.getElementById('stock-shares').value);
  if (!shares) { showToast('Enter shares', false); return; }
  const res = await fetch(`/api/companies/${cid}/buy_stock`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({shares})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`Bought ${shares} shares for ${fmt(d.cost)}`); openCompany(cid); loadCompanies();
}

async function sellStock(cid) {
  const shares = parseInt(document.getElementById('stock-shares').value);
  if (!shares) { showToast('Enter shares', false); return; }
  const res = await fetch(`/api/companies/${cid}/sell_stock`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({shares})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`Sold for ${fmt(d.earnings)}`); openCompany(cid); loadCompanies();
}

async function deposit(cid) {
  const amount = parseFloat(document.getElementById('dep-amount').value);
  if (!amount) { showToast('Enter amount', false); return; }
  const res = await fetch(`/api/companies/${cid}/deposit`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({amount})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`Deposited ${fmt(amount)}`); openCompany(cid);
}

async function withdraw(cid) {
  const amount = parseFloat(document.getElementById('dep-amount').value);
  if (!amount) { showToast('Enter amount', false); return; }
  const res = await fetch(`/api/companies/${cid}/withdraw`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({amount})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`Withdrew ${fmt(amount)}`); openCompany(cid);
}

async function tradeSus(cid, action) {
  const shares = parseInt(document.getElementById('sus-shares').value);
  if (!shares) { showToast('Enter shares', false); return; }
  const res = await fetch(`/api/companies/${cid}/trade_sus`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action, shares})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`${action === 'buy' ? 'Bought' : 'Sold'} ${shares} SUS shares`); openCompany(cid);
}

async function castVote(cid, vote) {
  const res = await fetch(`/api/companies/${cid}/vote`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({vote})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`Voted ${vote.toUpperCase()}`); openCompany(cid);
}

async function requestLoan(cid) {
  const amount = parseFloat(document.getElementById('loan-amt')?.value);
  if (!amount) { showToast('Enter amount', false); return; }
  const res = await fetch(`/api/companies/${cid}/loan`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({amount})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`Loan of ${fmt(amount)} received. Due: ${fmt(d.due)}`); openCompany(cid);
}

async function repayLoan(cid) {
  const res = await fetch(`/api/companies/${cid}/repay`, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast('Loan repaid!'); openCompany(cid);
}

async function buyInsurance(cid) {
  const premium = parseFloat(document.getElementById('premium-amt')?.value || 50);
  const res = await fetch(`/api/companies/${cid}/insure`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({premium})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`Insured! Coverage: ${fmt(d.coverage)}`); openCompany(cid);
}

async function postBounty(cid) {
  const target_id = document.getElementById('bounty-target')?.value;
  const amount = parseFloat(document.getElementById('bounty-amt')?.value);
  const res = await fetch(`/api/companies/${cid}/bounty`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({target_id, amount})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast('Bounty posted!'); openCompany(cid);
}

async function setSpread(cid) {
  const buy = parseFloat(document.getElementById('spread-buy')?.value);
  const sell = parseFloat(document.getElementById('spread-sell')?.value);
  const res = await fetch(`/api/companies/${cid}/set_spread`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({buy, sell})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast('Spread set!'); openCompany(cid);
}

async function mmTrade(cid, action) {
  const shares = parseInt(document.getElementById('mm-shares')?.value);
  if (!shares) { showToast('Enter shares', false); return; }
  const res = await fetch(`/api/companies/${cid}/market_trade`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action, shares})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`Trade executed`); openCompany(cid);
}

async function payProtection(cid) {
  const amount = parseFloat(document.getElementById('prot-amt')?.value || 100);
  const res = await fetch(`/api/companies/${cid}/pay_protection`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({amount})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast('Protection paid. You are safe... for now.'); openCompany(cid);
}

function showCreate() {
  if (!myUserId) { showToast('Login first', false); return; }
  document.getElementById('create-modal').style.display = 'flex';
}

function hideCreate() { document.getElementById('create-modal').style.display = 'none'; }

async function submitCreate() {
  const name = document.getElementById('c-name').value.trim();
  const ticker = document.getElementById('c-ticker').value.trim().toUpperCase();
  const desc = document.getElementById('c-desc').value.trim();
  const type = selectedType;
  if (!name || !ticker || !type) { showToast('Fill in all fields and select a type', false); return; }
  const res = await fetch('/api/companies/create', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, ticker, description: desc, type})});
  const d = await res.json();
  if (!res.ok) { showToast(d.error, false); return; }
  showToast(`${name} founded!`); hideCreate(); loadCompanies();
}

init();
setInterval(loadCompanies, 30000);
</script>
</body>
</html>
"""

GUIDE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Sus Stock — Company Guide</title>
<style>
  :root{--bg:#1e1f22;--surface:#2b2d31;--surface2:#313338;--accent:#5865f2;--green:#57f287;--red:#ed4245;--text:#dbdee1;--muted:#949ba4;--border:#3a3c40;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;line-height:1.6}
  header{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 28px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:10}
  header h1{font-size:20px;font-weight:700}
  a.nav{color:var(--accent);text-decoration:none;font-size:13px;font-weight:700}
  .wrap{max-width:860px;margin:0 auto;padding:28px 24px 80px}
  h2{font-size:22px;margin:28px 0 8px;border-bottom:1px solid var(--border);padding-bottom:8px}
  h3{font-size:17px;margin:20px 0 4px;display:flex;align-items:center;gap:8px}
  p{color:var(--text);margin:6px 0;font-size:14px}
  .muted{color:var(--muted);font-size:13px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 20px;margin:14px 0}
  .tag{display:inline-block;font-size:11px;font-weight:700;padding:2px 9px;border-radius:999px;margin-right:6px}
  .tag.passive{background:#57f28722;color:var(--green)}
  .tag.active{background:#5865f222;color:var(--accent)}
  .tag.risk{background:#ed424522;color:var(--red)}
  ul{margin:6px 0 6px 22px}
  li{font-size:14px;margin:3px 0}
  code{background:var(--surface2);padding:1px 6px;border-radius:5px;font-size:13px}
  .toc{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:6px;margin:14px 0}
  .toc a{color:var(--text);text-decoration:none;font-size:13px;background:var(--surface2);padding:7px 10px;border-radius:8px;border:1px solid var(--border)}
  .toc a:hover{border-color:var(--accent)}
</style>
</head>
<body>
<header>
  <span style="font-size:22px">📖</span>
  <h1>Company Guide</h1>
  <a href="/" class="nav">← Back to Market</a>
</header>
<div class="wrap">

<h2>How companies work (the basics)</h2>
<div class="card">
  <p><b>Founding:</b> Anyone can found a company for <code>$2,000</code>. You become the CEO and start owning all 1,000 shares.</p>
  <p><b>Company stock:</b> Each company issues tradeable shares. The price is <code>company value ÷ shares issued</code>, where <b>company value = treasury cash + (SUS shares the company holds × SUS price)</b>.</p>
  <p><b>Treasury:</b> A shared pot of cash. Members deposit into it, services earn into it, and the CEO can trade SUS with it. A bigger treasury = higher stock price = richer shareholders.</p>
  <p><b>Investing:</b> Buy a company's stock; if its value grows (smart CEO trades, passive earnings, etc.), your shares are worth more and you sell for profit.</p>
  <p><b>Trading as a company:</b> A CEO can switch the main trading menu to act on the company's behalf — buying, selling, and shorting SUS straight from the treasury.</p>
  <p><b>Tags below:</b> <span class="tag passive">Passive</span> earns automatically · <span class="tag active">Active</span> needs you to act · <span class="tag risk">Risky</span> can lose money.</p>
</div>

<h2>Credit score &amp; trust</h2>
<div class="card">
  <p>Every account has a <b>credit score</b> from 300–850, starting at <b>500</b>. It controls how much money you're trusted to send.</p>
  <p><b>Score goes up:</b> repaying a loan in full <code>+25</code>.</p>
  <p><b>Score goes down:</b> defaulting on loan interest (can't afford a payment) <code>−20</code>.</p>
  <p><b>Send limits by tier (real FICO ranges):</b></p>
  <ul>
    <li>🔴 <b>Poor</b> (300–579): send up to <b>$1,000</b> at a time</li>
    <li>🟡 <b>Fair</b> (580–669): up to <b>$5,000</b></li>
    <li>🥇 <b>Good</b> (670–739): up to <b>$25,000</b></li>
    <li>🟢 <b>Very Good</b> (740–799): up to <b>$100,000</b></li>
    <li>💎 <b>Exceptional</b> (800–850): <b>unlimited</b></li>
  </ul>
  <p class="muted">Your tier badge shows on the leaderboard and in your portfolio. Borrow and repay loans from a Lending Bank to build trust.</p>
</div>

<div class="toc">
  <a href="#hedge_fund">💼 Hedge Fund</a>
  <a href="#day_trading">⚡ Day Trading LLC</a>
  <a href="#index_fund">📊 Index Fund</a>
  <a href="#insider_ring">🔍 Insider Ring</a>
  <a href="#short_cartel">🐻 Short Cartel</a>
  <a href="#pump_dump">🚀 Pump & Dump</a>
  <a href="#lending_bank">🏦 Lending Bank</a>
  <a href="#invest_bank">💳 Investment Bank</a>
  <a href="#savings">🐷 Savings Account</a>
  <a href="#insurance">🛡️ Insurance</a>
  <a href="#bounty_hunter">🎯 Bounty Hunter</a>
  <a href="#market_maker">⚖️ Market Maker</a>
  <a href="#sus_mafia">🤌 Sus Mafia</a>
  <a href="#wolf_pack">🐺 Wolf Pack</a>
  <a href="#casino">🎰 Casino</a>
</div>

<h2>The 15 company types</h2>

<div class="card" id="hedge_fund">
  <h3>💼 Hedge Fund <span class="tag active">Active</span></h3>
  <p>Members pool cash and the CEO trades SUS with it, then shares out the winnings.</p>
  <ul>
    <li>Members <b>deposit</b> cash into the treasury.</li>
    <li>The CEO uses <b>Trade As Company</b> mode to buy/sell/short SUS with the pooled money.</li>
    <li>The CEO clicks <b>Distribute Profits</b> to split an amount of treasury among members <b>proportional to their deposits</b>.</li>
  </ul>
  <p class="muted">Great when you trust the CEO to trade well. Returns scale with how much you deposited.</p>
</div>

<div class="card" id="day_trading">
  <h3>⚡ Day Trading LLC <span class="tag active">Active</span></h3>
  <p>The whole team votes on what to do, and the majority wins.</p>
  <ul>
    <li>Members vote <b>Buy</b>, <b>Sell</b>, or <b>Hold</b> each round.</li>
    <li>When the vote period ends, the majority action auto-executes: Buy spends ~half the treasury on SUS; Sell dumps all the company's SUS.</li>
  </ul>
  <p class="muted">Democratic trading — good for active groups that like to debate the market.</p>
</div>

<div class="card" id="index_fund">
  <h3>📊 Index Fund <span class="tag passive">Passive</span></h3>
  <p>A hands-off fund that just rides the market.</p>
  <ul>
    <li>Every 20 minutes it <b>automatically buys SUS</b> with any idle treasury cash.</li>
    <li>No CEO action needed — deposit, hold, and let the fund's value track SUS over time.</li>
  </ul>
  <p class="muted">Lowest effort, steady exposure to the market.</p>
</div>

<div class="card" id="insider_ring">
  <h3>🔍 Insider Trading Ring <span class="tag passive">Passive income</span></h3>
  <p>Sell early access to market news as a paid subscription.</p>
  <ul>
    <li>The CEO sets an <b>hourly subscription price</b>.</li>
    <li>Subscribers see every market event (earnings, flash crashes, cycle shifts) <b>2 minutes before the public</b>, with a live countdown — time to trade before the price moves.</li>
    <li>Subscribers are billed hourly into the treasury. The CEO gets the news free and can <b>grant free access</b> to anyone.</li>
  </ul>
  <p class="muted">The most powerful information edge in the game, and a steady earner for the owner.</p>
</div>

<div class="card" id="short_cartel">
  <h3>🐻 Short Selling Cartel <span class="tag risk">Risky</span></h3>
  <p>A club that amplifies its members' short bets.</p>
  <ul>
    <li>Members who profit on a SUS short cover get a <b>2× bonus</b>, paid from the cartel treasury.</li>
    <li>Keep the treasury funded (via deposits) so it can pay out the bonuses.</li>
  </ul>
  <p class="muted">Best when the market is trending down and members short aggressively.</p>
</div>

<div class="card" id="pump_dump">
  <h3>🚀 Pump &amp; Dump Crew <span class="tag risk">Risky</span></h3>
  <p>Coordinate to spike the price, then cash out.</p>
  <ul>
    <li>Members vote to <b>pump</b> — a majority pushes the price target up ~2×.</li>
    <li>Then vote to <b>dump</b> — the company sells its SUS into the spike.</li>
  </ul>
  <p class="muted">High risk, high drama. Timing the dump is everything.</p>
</div>

<div class="card" id="lending_bank">
  <h3>🏦 Lending Bank <span class="tag passive">Passive income</span></h3>
  <p>Loan cash to players and collect interest.</p>
  <ul>
    <li>Players <b>request a loan</b> from the treasury and owe it back with <b>8% interest</b>.</li>
    <li>Interest is collected automatically each cycle into the treasury.</li>
  </ul>
  <p class="muted">Steady income as long as borrowers keep borrowing.</p>
</div>

<div class="card" id="invest_bank">
  <h3>💳 Investment Bank <span class="tag passive">Passive income</span></h3>
  <p>Take a cut of all the trading in the game.</p>
  <ul>
    <li>Earns a <b>3% commission</b> on <b>every company stock trade</b> across the whole market, paid straight into the treasury.</li>
    <li>No action needed — the busier the market, the more it earns.</li>
  </ul>
  <p class="muted">Best when lots of company stock is being traded.</p>
</div>

<div class="card" id="savings">
  <h3>🐷 Savings Account <span class="tag passive">Passive</span></h3>
  <p>A safe place to park cash and earn guaranteed interest.</p>
  <ul>
    <li>Deposit cash and earn <b>1% per hour</b>, paid from the treasury.</li>
    <li>No risk to the depositor — interest is guaranteed while the treasury can pay.</li>
  </ul>
  <p class="muted">Low risk, slow steady growth.</p>
</div>

<div class="card" id="insurance">
  <h3>🛡️ Insurance Company <span class="tag passive">Passive income</span></h3>
  <p>Sell protection against market crashes.</p>
  <ul>
    <li>Players pay a <b>premium</b> for coverage.</li>
    <li>If a policyholder's net worth drops <b>20%+</b>, the company automatically <b>pays them out</b>.</li>
  </ul>
  <p class="muted">Profits in calm markets, pays out in crashes — price your premiums wisely.</p>
</div>

<div class="card" id="bounty_hunter">
  <h3>🎯 Bounty Hunter <span class="tag active">Active</span></h3>
  <p>Players put hits on each other's portfolios.</p>
  <ul>
    <li>A player posts a <b>bounty</b> on a target (30% fee goes to the company).</li>
    <li>If the target's net worth drops enough, the bounty <b>pays out</b> to the poster.</li>
  </ul>
  <p class="muted">A way to profit from (or cause) other players' losses.</p>
</div>

<div class="card" id="market_maker">
  <h3>⚖️ Market Maker <span class="tag passive">Passive income</span></h3>
  <p>Be the middleman — buy low, sell high, pocket the spread.</p>
  <ul>
    <li>The CEO sets a <b>buy price</b> and a <b>sell price</b> (the spread).</li>
    <li>Players trade SUS directly with the company at those prices; the gap is the company's profit.</li>
  </ul>
  <p class="muted">Earns on volume. Keep both treasury cash and SUS shares stocked.</p>
</div>

<div class="card" id="sus_mafia">
  <h3>🤌 Sus Mafia <span class="tag active">Active</span></h3>
  <p>Run a protection racket.</p>
  <ul>
    <li>Players and companies <b>pay protection</b> into the treasury.</li>
    <li>Lean on others to keep the payments coming.</li>
  </ul>
  <p class="muted">As much a social game as an economic one.</p>
</div>

<div class="card" id="wolf_pack">
  <h3>🐺 Wolf Pack <span class="tag risk">Risky</span></h3>
  <p>Move as one to slam the market up.</p>
  <ul>
    <li>A majority <b>buy vote</b> amplifies the price target by ~<b>3×</b> — an even bigger move than a pump &amp; dump.</li>
    <li>Profits are meant to be shared by the pack.</li>
  </ul>
  <p class="muted">The biggest coordinated price swings in the game.</p>
</div>

<div class="card" id="casino">
  <h3>🎰 Casino <span class="tag passive">Passive income</span> <span class="tag risk">Risky for players</span></h3>
  <p>Players gamble against your treasury; the house edge earns you money over time.</p>
  <ul>
    <li>Players bet on 5 games with different odds:</li>
    <li>🪙 <b>Coin Flip</b> — 48% to win 2× (4% house edge)</li>
    <li>🃏 <b>High Card</b> — 45% to win 2.1× (5.5% edge)</li>
    <li>🎲 <b>Dice Roll</b> — 33% to win 2.8× (7.6% edge)</li>
    <li>🎡 <b>Roulette</b> — 25% to win 3.6× (10% edge)</li>
    <li>🎰 <b>Slots</b> — 10% to win 8× jackpot (20% edge)</li>
    <li>Wins are paid from the treasury; losses go into it. Over time the house profits.</li>
  </ul>
  <p class="muted">Owners earn steadily; players chase the jackpot. Keep the treasury funded to cover big wins.</p>
</div>

</div>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
