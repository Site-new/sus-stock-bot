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


def fmt(amount):
    return f"${amount:,.2f}"


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
                      "cash": u["balance"], "net_worth": net_worth})
    users.sort(key=lambda x: x["net_worth"], reverse=True)
    return jsonify(users)


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
    import datetime as dt
    cst = dt.timezone(dt.timedelta(hours=-6))
    hour = dt.datetime.now(cst).hour
    market_open = hour >= 12
    cycle = data.get("bull_bear", "neutral")
    sentiment = data.get("sentiment", 50)

    # Insider ring members see news immediately; everyone else waits for public_at
    is_insider = user_in_insider_ring(session.get("user_id"))
    now = int(time.time())
    all_news = data.get("news_feed", [])
    if is_insider:
        news = all_news[-10:]
    else:
        news = [n for n in all_news if n.get("public_at", n.get("ts", 0)) <= now][-10:]

    return jsonify({
        "price": price, "change": change, "change_pct": pct,
        "history": history[-100:], "timestamps": timestamps,
        "market_open": market_open, "bull_bear": cycle,
        "sentiment": sentiment, "news": news, "is_insider": is_insider,
    })


@app.route("/api/leaderboard")
def api_leaderboard():
    data = load_data()
    price = data["stock_price"]
    users = []
    for uid, u in data["users"].items():
        invested = round(u["shares"] * price, 2)
        net_worth = round(u["balance"] + invested, 2)
        username = get_discord_username(uid)
        users.append({"id": uid, "username": username, "shares": u["shares"], "cash": u["balance"],
                      "invested": invested, "net_worth": net_worth,
                      "pnl": round(net_worth - STARTING_BALANCE, 2)})
    users.sort(key=lambda x: x["net_worth"], reverse=True)
    return jsonify(users)


@app.route("/api/me")
def api_me():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    data = load_data()
    u = get_user(data, session["user_id"])
    save_data(data)
    price = data["stock_price"]
    invested = round(u["shares"] * price, 2)
    net_worth = round(u["balance"] + invested, 2)
    uid = session["user_id"]
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
    })


@app.route("/api/buy", methods=["POST"])
def api_buy():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    shares = int(request.json.get("shares", 0))
    if shares <= 0:
        return jsonify({"error": "invalid amount"}), 400
    data = load_data()
    u = get_user(data, session["user_id"])
    price = data["stock_price"]
    cost = round(price * shares, 2)
    if u["balance"] < cost:
        return jsonify({"error": f"Not enough cash. Need {fmt(cost)}, have {fmt(u['balance'])}"}), 400
    u["balance"] = round(u["balance"] - cost, 2)
    u["shares"] += shares
    save_data(data)
    return jsonify({"ok": True, "bought": shares, "cost": cost, "balance": u["balance"], "shares": u["shares"]})


@app.route("/api/sell", methods=["POST"])
def api_sell():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    shares = int(request.json.get("shares", 0))
    if shares <= 0:
        return jsonify({"error": "invalid amount"}), 400
    data = load_data()
    u = get_user(data, session["user_id"])
    price = data["stock_price"]
    if u["shares"] < shares:
        return jsonify({"error": f"You only have {u['shares']} shares"}), 400
    earnings = round(price * shares, 2)
    u["shares"] -= shares
    u["balance"] = round(u["balance"] + earnings, 2)
    save_data(data)
    return jsonify({"ok": True, "sold": shares, "earnings": earnings, "balance": u["balance"], "shares": u["shares"]})


@app.route("/api/short", methods=["POST"])
def api_short():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    shares = int(request.json.get("shares", 0))
    if shares <= 0:
        return jsonify({"error": "invalid amount"}), 400
    data = load_data()
    uid = session["user_id"]
    if uid in data.get("shorts", {}):
        return jsonify({"error": "You already have an open short. Cover it first."}), 400
    price = data["stock_price"]
    get_user(data, uid)
    data.setdefault("shorts", {})[uid] = {"shares": shares, "entry_price": price}
    save_data(data)
    return jsonify({"ok": True, "shares": shares, "entry_price": price})


@app.route("/api/cover", methods=["POST"])
def api_cover():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    data = load_data()
    uid = session["user_id"]
    shorts = data.get("shorts", {})
    if uid not in shorts:
        return jsonify({"error": "No open short position."}), 400
    short = shorts.pop(uid)
    price = data["stock_price"]
    pnl = round((short["entry_price"] - price) * short["shares"], 2)
    u = get_user(data, uid)
    u["balance"] = round(max(0, u["balance"] + pnl), 2)
    data["shorts"] = shorts
    save_data(data)
    return jsonify({"ok": True, "pnl": pnl, "balance": u["balance"]})


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


def user_in_insider_ring(user_id):
    """True if the user is a member of any Insider Trading Ring company."""
    if not user_id:
        return False
    try:
        companies = load_companies()
        for c in companies.values():
            if c.get("type") == "insider_ring" and str(user_id) in c.get("members", {}):
                return True
    except Exception:
        pass
    return False


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
        })
    result.sort(key=lambda x: x["value"], reverse=True)
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
    company = create_company(session["user_id"], name, ticker, ctype, desc)
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
    if c["type"] == "savings":
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
    if c["type"] == "savings":
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
    rate = 0.05  # 5% per 20min cycle
    c["treasury"] = round(c["treasury"] - amount, 2)
    c.setdefault("loans", {})[uid] = {"amount": amount, "rate": rate, "due": round(amount * 1.2, 2)}
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


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/companies")
def companies_page():
    return render_template_string(COMPANIES_HTML)


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
  @media(max-width:1100px){ .layout{ grid-template-columns: 1fr 340px; } .news-col{ display:none; } }
  @media(max-width:800px){ .layout{ grid-template-columns:1fr; } }

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
</style>
</head>
<body>

<header>
  <span style="font-size:22px">📈</span>
  <h1>Sus Stock Market</h1>
  <span class="tag">LIVE</span>
  <div class="live-dot"></div>
  <button onclick="toggleCompanies()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:13px;font-weight:700;padding:5px 14px;border-radius:8px;cursor:pointer;margin-left:8px">🏢 Companies</button>
  <div class="auth-area" id="auth-area">
    <a href="/login" class="btn btn-discord">
      <svg width="16" height="12" viewBox="0 0 71 55" fill="white"><path d="M60.1 4.9A58.6 58.6 0 0 0 45.6.4a.2.2 0 0 0-.2.1 40.8 40.8 0 0 0-1.8 3.7 54.1 54.1 0 0 0-16.2 0 37.6 37.6 0 0 0-1.8-3.7.22.22 0 0 0-.2-.1A58.4 58.4 0 0 0 10.9 4.9a.2.2 0 0 0-.1.1C1.6 18.1-.9 31 .3 43.7a.24.24 0 0 0 .1.2 58.9 58.9 0 0 0 17.7 8.9.22.22 0 0 0 .2-.1 42 42 0 0 0 3.6-5.9.21.21 0 0 0-.1-.3 38.7 38.7 0 0 1-5.5-2.6.22.22 0 0 1 0-.4c.4-.3.7-.5 1.1-.8a.21.21 0 0 1 .2 0c11.5 5.3 24 5.3 35.4 0a.21.21 0 0 1 .2 0l1.1.8a.22.22 0 0 1 0 .4 36.3 36.3 0 0 1-5.5 2.6.22.22 0 0 0-.1.3 47.1 47.1 0 0 0 3.6 5.9.21.21 0 0 0 .2.1 58.7 58.7 0 0 0 17.7-8.9.23.23 0 0 0 .1-.2c1.5-15.1-2.4-28-10.4-39.5a.18.18 0 0 0-.1-.2zM23.7 36c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.2 6.4-7.2c3.6 0 6.5 3.3 6.4 7.2 0 4-2.8 7.2-6.4 7.2zm23.6 0c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.2 6.4-7.2c3.6 0 6.5 3.3 6.4 7.2 0 4-2.8 7.2-6.4 7.2z"/></svg>
      Login with Discord
    </a>
  </div>
</header>

<div class="layout">
  <!-- News column -->
  <div class="news-col">
    <div class="card" style="height:calc(100vh - 120px);overflow-y:auto;position:sticky;top:20px">
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
        <span id="cycle-badge" style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:999px;background:var(--surface2);color:var(--muted)">😐 Neutral</span>
        <span id="sentiment-badge" style="font-size:10px;padding:2px 8px;border-radius:999px;background:var(--surface2);color:var(--muted);margin-left:auto">Sentiment 50</span>
      </div>
      <div class="price-hero">
        <span class="price" id="price">—</span>
        <span class="change-badge" id="change-badge">—</span>
      </div>
      <div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap">
        <span style="font-size:11px;color:var(--muted);align-self:center;margin-right:4px">Zoom:</span>
        <button class="zoom-btn active" onclick="setZoom(10,'5m')" data-z="5m">5m</button>
        <button class="zoom-btn" onclick="setZoom(20,'10m')" data-z="10m">10m</button>
        <button class="zoom-btn" onclick="setZoom(60,'30m')" data-z="30m">30m</button>
        <button class="zoom-btn" onclick="setZoom(120,'1h')" data-z="1h">1h</button>
        <button class="zoom-btn" onclick="setZoom(0,'all')" data-z="all">All</button>
      </div>
      <div class="chart-wrap"><canvas id="priceChart"></canvas></div>
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
    <div style="display:flex;gap:8px">
      <button class="btn btn-discord" style="flex:1" onclick="submitCreateDrawer()">Found — $2,000</button>
      <button class="btn btn-logout" onclick="hideCreateDrawer()">Cancel</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const fmt = v => '$' + v.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
const medals = ['🥇','🥈','🥉'];
let myUserId = null;

function showToast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (ok ? 'ok' : 'err');
  setTimeout(() => t.className = 'toast', 3000);
}

// Chart
const ctx = document.getElementById('priceChart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: { labels: [], datasets: [{ data: [], borderColor: '#57f287', backgroundColor: 'rgba(87,242,135,0.08)', borderWidth: 2, pointRadius: 0, fill: true, tension: 0.3 }] },
  options: {
    responsive: true, maintainAspectRatio: false, animation: { duration: 400 },
    plugins: { legend: { display: false }, tooltip: { callbacks: {
      label: c => fmt(c.parsed.y),
      title: items => items[0].label || ''
    }}},
    scales: {
      x: {
        display: true,
        ticks: {
          color: '#949ba4',
          maxTicksLimit: 6,
          maxRotation: 0,
          autoSkip: true,
          font: { size: 10 }
        },
        grid: { display: false },
        border: { display: false }
      },
      y: { grid: { color: '#3a3c40' }, ticks: { color: '#949ba4', callback: v => fmt(v) }, border: { display: false } }
    }
  }
});

async function fetchStock() {
  const d = await fetch('/api/stock').then(r => r.json());
  const { price, change, change_pct: pct, history, timestamps } = d;
  const up = change >= 0;
  if (!history) return;
  document.getElementById('price').textContent = fmt(price);
  const badge = document.getElementById('change-badge');
  badge.textContent = `${up?'+':''}${fmt(change)} (${up?'+':''}${pct}%)`;
  badge.className = 'change-badge ' + (up ? 'up' : 'down');
  fullHistory = history;
  fullTimestamps = timestamps || [];
  renderChart();

  // Market status badges
  const mb = document.getElementById('market-badge');
  if (d.market_open !== undefined) {
    mb.textContent = d.market_open ? '🟢 OPEN' : '🔴 CLOSED';
    mb.style.background = d.market_open ? '#57f28722' : '#ed424522';
    mb.style.color = d.market_open ? 'var(--green)' : 'var(--red)';
  }
  const cb = document.getElementById('cycle-badge');
  if (d.bull_bear) {
    const cycleMap = {bull: '🐂 Bull', bear: '🐻 Bear', neutral: '😐 Neutral'};
    cb.textContent = cycleMap[d.bull_bear] || '😐 Neutral';
    cb.style.color = d.bull_bear === 'bull' ? 'var(--green)' : (d.bull_bear === 'bear' ? 'var(--red)' : 'var(--muted)');
  }
  if (d.sentiment !== undefined) {
    const s = d.sentiment;
    const sl = s > 75 ? 'Extreme Greed 😏' : s > 55 ? 'Greed 😌' : s < 25 ? 'Extreme Fear 😱' : s < 45 ? 'Fear 😰' : 'Neutral 😐';
    document.getElementById('sentiment-badge').textContent = `${sl} (${s})`;
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
        const isEarly = d.is_insider && (n.public_at || n.ts) > nowSec;
        const earlyBadge = isEarly ? ` <span style="font-size:9px;font-weight:700;background:#5865f2;color:#fff;padding:1px 5px;border-radius:999px">EARLY</span>` : '';
        return `<div style="padding:8px 0;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:flex-start;${isEarly?'background:#5865f211;border-radius:6px;padding:8px':''}">
          <span style="font-size:18px;flex-shrink:0">${n.positive ? '📈' : '📉'}</span>
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

async function fetchMe() {
  const res = await fetch('/api/me');
  if (!res.ok) { initChat(); return; }
  const u = await res.json();
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
  if (u.username === 'slasher_asher') {
    document.getElementById('admin-toggle').style.display = 'block';
    loadAdmin();
  }

  document.getElementById('portfolio-area').innerHTML = `
    <div class="portfolio-grid">
      <div class="p-stat"><div class="p-stat-label">Net Worth</div><div class="p-stat-value">${fmt(u.net_worth)}</div></div>
      <div class="p-stat"><div class="p-stat-label">P&L</div><div class="p-stat-value" style="color:${pnlColor}">${u.pnl>=0?'+':''}${fmt(u.pnl)}</div></div>
      <div class="p-stat"><div class="p-stat-label">SUS Shares</div><div class="p-stat-value" id="my-shares">${u.shares}</div></div>
      <div class="p-stat"><div class="p-stat-label">Cash</div><div class="p-stat-value" id="my-cash">${fmt(u.cash)}</div></div>
      <div class="p-stat" style="grid-column:span 2"><div class="p-stat-label">Invested Value</div><div class="p-stat-value" id="my-invested">${fmt(u.invested)}</div></div>
    </div>
    <!-- Trading tabs -->
    <div style="display:flex;gap:4px;margin-bottom:10px;flex-wrap:wrap">
      ${['Buy','Sell','Short','Limits'].map(t => `<button onclick="setTab('${t.toLowerCase()}')" id="tab-${t.toLowerCase()}" class="zoom-btn ${t==='Buy'?'active':''}" style="flex:1">${t}</button>`).join('')}
    </div>

    <div id="tab-buy-content">
      <input type="number" id="trade-amount" class="trade-input" placeholder="Shares to buy..." min="1"/>
      <button class="btn btn-buy" style="width:100%;margin-top:8px" onclick="trade('buy')">📈 Buy SUS</button>
    </div>

    <div id="tab-sell-content" style="display:none">
      <input type="number" id="sell-amount" class="trade-input" placeholder="Shares to sell..." min="1"/>
      <button class="btn btn-sell" style="width:100%;margin-top:8px" onclick="trade('sell')">📉 Sell SUS</button>
    </div>

    <div id="tab-short-content" style="display:none">
      ${u.short ? `
        <div style="background:#ed424518;border:1px solid #ed424540;border-radius:8px;padding:12px;margin-bottom:10px">
          <div style="font-weight:700;color:var(--red);margin-bottom:6px">📉 Open Short Position</div>
          <div style="font-size:13px;color:var(--muted)">${u.short.shares} shares @ ${fmt(u.short.entry_price)}</div>
          <div style="font-size:15px;font-weight:700;${u.short_pnl >= 0 ? 'color:var(--green)' : 'color:var(--red)'}">P&L: ${u.short_pnl >= 0 ? '+' : ''}${fmt(u.short_pnl)}</div>
        </div>
        <button class="btn btn-sell" style="width:100%" onclick="coverShort()">Close Short Position</button>
      ` : `
        <div style="font-size:12px;color:var(--muted);margin-bottom:8px">Short selling lets you profit when the price drops. You borrow shares and buy them back later at a lower price.</div>
        <input type="number" id="short-amount" class="trade-input" placeholder="Shares to short..." min="1"/>
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
    </div>`;
  // Restore the previously active tab after re-render
  setTab(activeTab);
}

let activeTab = 'buy';
function setTab(tab) {
  activeTab = tab;
  ['buy','sell','short','limits'].forEach(t => {
    const el = document.getElementById('tab-'+t+'-content');
    const btn = document.getElementById('tab-'+t);
    if (el) el.style.display = t === tab ? 'block' : 'none';
    if (btn) btn.classList.toggle('active', t === tab);
  });
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
  el.innerHTML = users.slice(0,10).map((u,i) => `
    <div class="lb-row ${u.id === myUserId ? 'lb-me' : ''}">
      <div class="lb-rank">${medals[i] || '#'+(i+1)}</div>
      <div style="flex:1;min-width:0">
        <div class="lb-name">${u.id === myUserId ? '⭐ ' + (u.username || 'You') : (u.username || 'Trader #'+u.id.slice(-4))}</div>
        <div class="lb-pnl ${u.pnl>=0?'pos':'neg'}">${u.pnl>=0?'+':''}${fmt(u.pnl)} · ${u.shares} shares</div>
      </div>
      <div class="lb-worth">${fmt(u.net_worth)}</div>
    </div>`).join('');
  document.getElementById('lb-updated').textContent = 'Updated: ' + new Date().toLocaleTimeString();
}

// ── Chat ──────────────────────────────────────────────────────────────────────
let lastMsgId = 0;
let isLoggedIn = false;
let zoomPoints = 10; // default 5m (10 × 30s ticks)
let fullHistory = [];
let fullTimestamps = [];

function setZoom(points, label) {
  zoomPoints = points;
  document.querySelectorAll('.zoom-btn').forEach(b => b.classList.toggle('active', b.dataset.z === label));
  renderChart();
}

function renderChart() {
  const h = zoomPoints > 0 ? fullHistory.slice(-zoomPoints) : fullHistory;
  const t = zoomPoints > 0 ? fullTimestamps.slice(-zoomPoints) : fullTimestamps;
  const up = h.length < 2 || h[h.length-1] >= h[0];
  chart.data.datasets[0].borderColor = up ? '#57f287' : '#ed4245';
  chart.data.datasets[0].backgroundColor = up ? 'rgba(87,242,135,0.08)' : 'rgba(237,66,69,0.08)';
  chart.data.labels = t.length ? t : h.map((_,i) => i);
  chart.data.datasets[0].data = h;
  chart.update();
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

// ── Companies drawer ───────────────────────────────────────────────────────────
const COMPANY_TYPES_MAP = {"hedge_fund":{"name":"Hedge Fund","emoji":"💼","desc":"Pool money and trade SUS together."},"day_trading":{"name":"Day Trading LLC","emoji":"⚡","desc":"Members vote every hour on buy/sell."},"index_fund":{"name":"Index Fund","emoji":"📊","desc":"Auto-buys SUS every 20min."},"insider_ring":{"name":"Insider Trading Ring","emoji":"🔍","desc":"Members see news early."},"short_cartel":{"name":"Short Selling Cartel","emoji":"🐻","desc":"Coordinated shorts hit 2× harder."},"pump_dump":{"name":"Pump & Dump Crew","emoji":"🚀","desc":"Mass buys spike the price 2×."},"lending_bank":{"name":"Lending Bank","emoji":"🏦","desc":"Lend cash at interest."},"invest_bank":{"name":"Investment Bank","emoji":"💳","desc":"Earn 3% commission on stock trades."},"savings":{"name":"Savings Account","emoji":"🐷","desc":"Earn 3% every 20min on deposits."},"insurance":{"name":"Insurance Company","emoji":"🛡️","desc":"Pay out if portfolio drops 20%+."},"bounty_hunter":{"name":"Bounty Hunter","emoji":"🎯","desc":"Post bounties on players."},"market_maker":{"name":"Market Maker","emoji":"⚖️","desc":"Set buy/sell spread for users."},"sus_mafia":{"name":"Sus Mafia","emoji":"🤌","desc":"Charge protection from companies."},"wolf_pack":{"name":"Wolf Pack","emoji":"🐺","desc":"Mass buy amplifies price 3×."}};
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
          <div style="font-weight:700;font-size:14px">${c.name}</div>
          <div style="font-size:11px;color:var(--accent);font-weight:700">${c.ticker} · ${t.name}</div>
        </div>
        <div style="text-align:right">
          <div style="font-size:16px;font-weight:800">${fmt(c.stock_price)}</div>
          <div style="font-size:10px;color:var(--muted)">per share</div>
        </div>
      </div>
      <div style="display:flex;gap:12px;font-size:12px;color:var(--muted)">
        <span>Treasury: <b style="color:var(--text)">${fmt(c.treasury)}</b></span>
        <span>Value: <b style="color:var(--text)">${fmt(c.value)}</b></span>
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
      ${[['Stock Price', fmt(c._stock_price)], ['Company Value', fmt(c._value)], ['Treasury', fmt(c.treasury)], ['Your Shares', c._my_shares || 0]].map(([l,v]) =>
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
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">CEO — Trade SUS (holds ${c.sus_shares||0} shares)</div>
      <div style="display:flex;gap:6px">
        <input type="number" id="d-sus-shares" class="trade-input" placeholder="SUS shares" style="flex:1;margin-bottom:0"/>
        <button class="btn btn-buy" onclick="dTradeSus('${cid}','buy')">Buy SUS</button>
        <button class="btn btn-sell" onclick="dTradeSus('${cid}','sell')">Sell SUS</button>
      </div>
    </div>` : ''}

    ${buildDrawerTypePanel(c, isMember, isCeo)}
  `;

  detailOpen = true;
  document.getElementById('company-detail-panel').style.right = '0';
}

function buildDrawerTypePanel(c, isMember, isCeo) {
  const cid = c.id;
  switch(c.type) {
    case 'lending_bank': return `<div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Loans (20% interest)</div>
      ${c._my_loan ? `<div style="color:var(--red);margin-bottom:6px;font-size:13px">You owe: ${fmt(c._my_loan.due)}</div><button class="btn btn-sell" style="width:100%" onclick="dRepayLoan('${cid}')">Repay Loan</button>`
      : `<input type="number" id="d-loan-amt" class="trade-input" placeholder="Loan amount"/><button class="btn btn-discord" style="width:100%" onclick="dRequestLoan('${cid}')">Request Loan</button>`}
    </div>`;
    case 'savings': return `<div style="margin-bottom:12px;background:var(--surface);border-radius:8px;padding:12px">
      <div style="font-size:12px;font-weight:700;margin-bottom:4px">🐷 Savings — 3% per 20min</div>
      <div style="font-size:13px;color:var(--green)">Your deposit: ${fmt(c._my_deposit||0)}</div>
      <div style="font-size:11px;color:var(--muted)">Deposit to treasury to earn interest automatically.</div>
    </div>`;
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
    case 'insider_ring': return `<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:12px">
      <div style="font-size:12px;font-weight:700;margin-bottom:4px">🔍 Insider Ring</div>
      <div style="font-size:12px;color:${isMember?'var(--green)':'var(--muted)'}">${isMember?'You receive market news early.':'Join to access early news.'}</div>
    </div>`;
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
}
async function submitCreateDrawer() {
  const name=document.getElementById('dc-name').value.trim();
  const ticker=document.getElementById('dc-ticker').value.trim().toUpperCase();
  const desc=document.getElementById('dc-desc').value.trim();
  const type=document.getElementById('dc-type').value;
  if(!name||!ticker||!type){showToast('Fill all fields and pick a type',false);return;}
  const res=await fetch('/api/companies/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,ticker,description:desc,type})});
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

      <div class="stat-label" style="margin-bottom:8px">Users</div>
      <div id="adm-users">`;

  users.forEach(u => {
    html += `
      <div style="background:var(--surface2);border-radius:8px;padding:10px 12px;margin-bottom:8px">
        <div style="font-weight:700;margin-bottom:6px">${u.username} <span style="color:var(--muted);font-size:11px">#${u.id.slice(-4)}</span></div>
        <div style="font-size:12px;color:var(--muted);margin-bottom:8px">Cash: ${fmt(u.cash)} · Shares: ${u.shares} · NW: ${fmt(u.net_worth)}</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">
          <input id="shares-${u.id}" type="number" class="trade-input" placeholder="Shares (neg=remove)" style="width:150px;font-size:12px;padding:5px 8px"/>
          <button class="btn btn-buy" style="font-size:12px;padding:5px 10px" onclick="adminGiveShares('${u.id}')">± Shares</button>
          <input id="cash-${u.id}" type="number" class="trade-input" placeholder="Cash (neg=remove)" style="width:150px;font-size:12px;padding:5px 8px"/>
          <button class="btn btn-buy" style="font-size:12px;padding:5px 10px;background:#fee75c22;color:#fee75c;border-color:#fee75c40" onclick="adminGiveCash('${u.id}')">± Cash</button>
          <button class="btn btn-sell" style="font-size:12px;padding:5px 10px" onclick="adminReset('${u.id}', '${u.username}')">Reset</button>
        </div>
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
      `<input type="number" id="loan-amt" placeholder="Loan amount"/><button class="btn btn-primary" style="width:100%" onclick="requestLoan('${cid}')">Request Loan (20% interest)</button>`}</div>`;
    case 'savings': return `
      <div><div class="card-title">Savings (3% per 20min)</div>
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
      ${isMember ? `<div style="font-size:13px;color:var(--green)">You receive news 5 minutes early. Check the market news panel.</div>` : '<div style="color:var(--muted);font-size:13px">Join to access early news.</div>'}</div>`;
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
