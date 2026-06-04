import json
import os
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
    if not os.path.exists(DATA_FILE):
        return {"users": {}, "stock_price": 50.0, "price_history": [50.0]}
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


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

    return redirect("/")


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
    return jsonify({"price": price, "change": change, "change_pct": pct, "history": history[-100:], "timestamps": timestamps})


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
    return jsonify({
        "username": session["username"],
        "avatar": session.get("avatar"),
        "user_id": session["user_id"],
        "shares": u["shares"],
        "cash": u["balance"],
        "invested": invested,
        "net_worth": net_worth,
        "pnl": round(net_worth - STARTING_BALANCE, 2),
        "price": price,
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


# ── Chat ──────────────────────────────────────────────────────────────────────

def load_chat():
    if not os.path.exists(CHAT_FILE):
        return []
    with open(CHAT_FILE, "r") as f:
        return json.load(f)

def save_chat(messages):
    with open(CHAT_FILE, "w") as f:
        json.dump(messages[-200:], f)  # keep last 200 messages


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
    return jsonify({"ok": True, "message": msg})


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


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

  .layout { display: grid; grid-template-columns: 1fr 340px; gap: 20px; padding: 20px 28px; max-width: 1400px; margin: 0 auto; }
  @media(max-width:900px){ .layout{ grid-template-columns:1fr; } }

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
  <div class="auth-area" id="auth-area">
    <a href="/login" class="btn btn-discord">
      <svg width="16" height="12" viewBox="0 0 71 55" fill="white"><path d="M60.1 4.9A58.6 58.6 0 0 0 45.6.4a.2.2 0 0 0-.2.1 40.8 40.8 0 0 0-1.8 3.7 54.1 54.1 0 0 0-16.2 0 37.6 37.6 0 0 0-1.8-3.7.22.22 0 0 0-.2-.1A58.4 58.4 0 0 0 10.9 4.9a.2.2 0 0 0-.1.1C1.6 18.1-.9 31 .3 43.7a.24.24 0 0 0 .1.2 58.9 58.9 0 0 0 17.7 8.9.22.22 0 0 0 .2-.1 42 42 0 0 0 3.6-5.9.21.21 0 0 0-.1-.3 38.7 38.7 0 0 1-5.5-2.6.22.22 0 0 1 0-.4c.4-.3.7-.5 1.1-.8a.21.21 0 0 1 .2 0c11.5 5.3 24 5.3 35.4 0a.21.21 0 0 1 .2 0l1.1.8a.22.22 0 0 1 0 .4 36.3 36.3 0 0 1-5.5 2.6.22.22 0 0 0-.1.3 47.1 47.1 0 0 0 3.6 5.9.21.21 0 0 0 .2.1 58.7 58.7 0 0 0 17.7-8.9.23.23 0 0 0 .1-.2c1.5-15.1-2.4-28-10.4-39.5a.18.18 0 0 0-.1-.2zM23.7 36c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.2 6.4-7.2c3.6 0 6.5 3.3 6.4 7.2 0 4-2.8 7.2-6.4 7.2zm23.6 0c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.2 6.4-7.2c3.6 0 6.5 3.3 6.4 7.2 0 4-2.8 7.2-6.4 7.2z"/></svg>
      Login with Discord
    </a>
  </div>
</header>

<div class="layout">
  <!-- Left column -->
  <div>
    <div class="card">
      <div class="card-title">SUS / USD</div>
      <div class="price-hero">
        <span class="price" id="price">—</span>
        <span class="change-badge" id="change-badge">—</span>
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

<!-- Chat panel -->
<div id="chat-panel" style="position:fixed;bottom:0;right:24px;width:320px;z-index:50;display:flex;flex-direction:column;box-shadow:0 -4px 24px #0006">
  <div id="chat-header" onclick="toggleChat()" style="background:var(--accent);color:#fff;padding:10px 16px;border-radius:12px 12px 0 0;cursor:pointer;display:flex;align-items:center;gap:8px;user-select:none">
    <span style="font-size:16px">💬</span>
    <span style="font-weight:700;font-size:14px">Live Chat</span>
    <span id="chat-unread" style="background:#fff;color:var(--accent);font-size:11px;font-weight:800;padding:1px 7px;border-radius:999px;display:none"></span>
    <span style="margin-left:auto;font-size:12px" id="chat-chevron">▲</span>
  </div>
  <div id="chat-body" style="background:var(--surface);border:1px solid var(--border);border-top:none;display:flex;flex-direction:column;height:360px">
    <div id="chat-messages" style="flex:1;overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:8px;scroll-behavior:smooth"></div>
    <div id="chat-input-area" style="padding:10px 12px;border-top:1px solid var(--border);display:flex;gap:8px">
      <input id="chat-input" class="trade-input" placeholder="Say something..." style="flex:1;font-size:13px;padding:7px 10px" onkeydown="if(event.key==='Enter')sendChat()" maxlength="300"/>
      <button class="btn btn-discord" style="padding:7px 12px;font-size:13px" onclick="sendChat()">Send</button>
    </div>
    <div id="chat-login-prompt" style="padding:12px;text-align:center;font-size:12px;color:var(--muted);border-top:1px solid var(--border);display:none">
      <a href="/login" style="color:var(--accent);font-weight:600">Login with Discord</a> to chat
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
  document.getElementById('price').textContent = fmt(price);
  const badge = document.getElementById('change-badge');
  badge.textContent = `${up?'+':''}${fmt(change)} (${up?'+':''}${pct}%)`;
  badge.className = 'change-badge ' + (up ? 'up' : 'down');
  chart.data.datasets[0].borderColor = up ? '#57f287' : '#ed4245';
  chart.data.datasets[0].backgroundColor = up ? 'rgba(87,242,135,0.08)' : 'rgba(237,66,69,0.08)';
  chart.data.labels = timestamps && timestamps.length ? timestamps : history.map((_,i) => i);
  chart.data.datasets[0].data = history;
  chart.update();
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
    <input type="number" id="trade-amount" class="trade-input" placeholder="Number of shares..." min="1"/>
    <div class="trade-btns">
      <button class="btn btn-buy" style="flex:1" onclick="trade('buy')">📈 Buy</button>
      <button class="btn btn-sell" style="flex:1" onclick="trade('sell')">📉 Sell</button>
    </div>`;
}

async function trade(action) {
  const shares = parseInt(document.getElementById('trade-amount').value);
  if (!shares || shares < 1) { showToast('Enter a valid number of shares', false); return; }
  const res = await fetch('/api/' + action, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shares })
  });
  const data = await res.json();
  if (!res.ok) { showToast(data.error, false); return; }
  if (action === 'buy') {
    showToast(`Bought ${data.bought} shares for ${fmt(data.cost)}`);
  } else {
    showToast(`Sold ${data.sold} shares for ${fmt(data.earnings)}`);
  }
  document.getElementById('trade-amount').value = '';
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
let chatOpen = true;
let lastMsgId = 0;
let unreadCount = 0;
let isLoggedIn = false;

function toggleChat() {
  chatOpen = !chatOpen;
  document.getElementById('chat-body').style.display = chatOpen ? 'flex' : 'none';
  document.getElementById('chat-chevron').textContent = chatOpen ? '▲' : '▼';
  if (chatOpen) { unreadCount = 0; document.getElementById('chat-unread').style.display = 'none'; }
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
  if (!chatOpen && !isMe) {
    unreadCount++;
    const badge = document.getElementById('chat-unread');
    badge.textContent = unreadCount;
    badge.style.display = 'inline';
  }
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
