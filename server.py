import json
import os
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)
DATA_FILE = "data.json"
STARTING_BALANCE = 1000.0


def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}, "stock_price": 50.0, "price_history": [50.0]}
    with open(DATA_FILE, "r") as f:
        return json.load(f)


@app.route("/api/stock")
def api_stock():
    data = load_data()
    history = data.get("price_history", [data["stock_price"]])
    price = data["stock_price"]
    prev = history[-2] if len(history) >= 2 else price
    change = round(price - prev, 2)
    pct = round((change / prev * 100) if prev else 0, 2)
    return jsonify({
        "price": price,
        "change": change,
        "change_pct": pct,
        "history": history[-100:],
    })


@app.route("/api/leaderboard")
def api_leaderboard():
    data = load_data()
    price = data["stock_price"]
    users = []
    for uid, u in data["users"].items():
        invested = round(u["shares"] * price, 2)
        net_worth = round(u["balance"] + invested, 2)
        pnl = round(net_worth - STARTING_BALANCE, 2)
        users.append({
            "id": uid,
            "shares": u["shares"],
            "cash": u["balance"],
            "invested": invested,
            "net_worth": net_worth,
            "pnl": pnl,
        })
    users.sort(key=lambda x: x["net_worth"], reverse=True)
    return jsonify(users)


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
    --bg: #1e1f22;
    --surface: #2b2d31;
    --surface2: #313338;
    --accent: #5865f2;
    --green: #57f287;
    --red: #ed4245;
    --text: #dbdee1;
    --muted: #949ba4;
    --border: #3a3c40;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; min-height: 100vh; }
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 16px 32px;
    display: flex;
    align-items: center;
    gap: 14px;
  }
  header h1 { font-size: 22px; font-weight: 700; }
  .tag { background: var(--accent); color: #fff; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px; }
  .live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 1.5s infinite; margin-left: auto; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .layout { display: grid; grid-template-columns: 1fr 340px; gap: 20px; padding: 24px 32px; max-width: 1400px; margin: 0 auto; }
  @media(max-width:900px){ .layout{ grid-template-columns:1fr; } }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px 24px; }
  .card-title { font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); margin-bottom: 16px; }
  .price-hero { display: flex; align-items: baseline; gap: 12px; margin-bottom: 6px; }
  .price-hero .price { font-size: 42px; font-weight: 800; letter-spacing: -1px; }
  .change-badge { font-size: 14px; font-weight: 700; padding: 4px 10px; border-radius: 6px; }
  .change-badge.up { background: #57f28722; color: var(--green); }
  .change-badge.down { background: #ed424522; color: var(--red); }
  .price-sub { font-size: 12px; color: var(--muted); margin-bottom: 20px; }
  .chart-wrap { position: relative; height: 260px; }
  .range-bar-wrap { margin-top: 14px; }
  .range-labels { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-bottom: 4px; }
  .range-bar { height: 6px; border-radius: 3px; background: var(--border); position: relative; }
  .range-fill { height: 100%; border-radius: 3px; background: linear-gradient(90deg, var(--red), var(--green)); }
  .range-marker { position: absolute; top: 50%; transform: translate(-50%,-50%); width: 12px; height: 12px; border-radius: 50%; background: #fff; border: 2px solid var(--accent); }
  .lb-row { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border); }
  .lb-row:last-child { border-bottom: none; }
  .lb-rank { font-size: 18px; width: 28px; text-align: center; flex-shrink: 0; }
  .lb-name { font-weight: 600; font-size: 14px; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lb-worth { font-weight: 700; font-size: 14px; color: var(--green); flex-shrink: 0; }
  .lb-pnl { font-size: 11px; color: var(--muted); }
  .lb-pnl.pos { color: var(--green); }
  .lb-pnl.neg { color: var(--red); }
  .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 16px; }
  .stat { background: var(--surface2); border-radius: 8px; padding: 12px 14px; }
  .stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 4px; }
  .stat-value { font-size: 18px; font-weight: 700; }
  .updated { font-size: 11px; color: var(--muted); margin-top: 10px; }
</style>
</head>
<body>
<header>
  <span style="font-size:24px">📈</span>
  <h1>Sus Stock Market</h1>
  <span class="tag">LIVE</span>
  <div class="live-dot"></div>
</header>
<div class="layout">
  <div>
    <div class="card">
      <div class="card-title">SUS / USD</div>
      <div class="price-hero">
        <span class="price" id="price">—</span>
        <span class="change-badge" id="change-badge">—</span>
      </div>
      <div class="price-sub">Sus Stock · updates every 30s</div>
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
  <div>
    <div class="card">
      <div class="card-title">🏆 Leaderboard</div>
      <div id="leaderboard">Loading...</div>
      <div class="updated" id="lb-updated">—</div>
    </div>
  </div>
</div>
<script>
const fmt = v => '$' + v.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
const medals = ['🥇','🥈','🥉'];
const ctx = document.getElementById('priceChart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: { labels: [], datasets: [{ data: [], borderColor: '#57f287', backgroundColor: 'rgba(87,242,135,0.08)', borderWidth: 2, pointRadius: 0, fill: true, tension: 0.3 }] },
  options: {
    responsive: true, maintainAspectRatio: false, animation: { duration: 400 },
    plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => fmt(c.parsed.y) } } },
    scales: { x: { display: false }, y: { grid: { color: '#3a3c40' }, ticks: { color: '#949ba4', callback: v => fmt(v) }, border: { display: false } } }
  }
});
async function fetchStock() {
  try {
    const d = await fetch('/api/stock').then(r => r.json());
    const { price, change, change_pct: pct, history } = d;
    const up = change >= 0;
    document.getElementById('price').textContent = fmt(price);
    const badge = document.getElementById('change-badge');
    badge.textContent = `${up?'+':''}${fmt(change)} (${up?'+':''}${pct}%)`;
    badge.className = 'change-badge ' + (up ? 'up' : 'down');
    chart.data.datasets[0].borderColor = up ? '#57f287' : '#ed4245';
    chart.data.datasets[0].backgroundColor = up ? 'rgba(87,242,135,0.08)' : 'rgba(237,66,69,0.08)';
    chart.data.labels = history.map((_,i) => i);
    chart.data.datasets[0].data = history;
    chart.update();
    document.getElementById('range-marker').style.left = ((price - 5) / 495 * 100) + '%';
    document.getElementById('range-label').textContent = fmt(price);
    const low = Math.min(...history), high = Math.max(...history), first = history[0], tc = price - first;
    document.getElementById('stat-low').textContent = fmt(low);
    document.getElementById('stat-high').textContent = fmt(high);
    const sc = document.getElementById('stat-change');
    sc.textContent = `${tc>=0?'+':''}${fmt(tc)}`;
    sc.style.color = tc >= 0 ? 'var(--green)' : 'var(--red)';
    document.getElementById('stat-points').textContent = history.length;
    document.getElementById('updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
  } catch(e) { console.error(e); }
}
async function fetchLeaderboard() {
  try {
    const users = await fetch('/api/leaderboard').then(r => r.json());
    const el = document.getElementById('leaderboard');
    if (!users.length) { el.textContent = 'No traders yet.'; return; }
    el.innerHTML = users.slice(0,10).map((u,i) => `
      <div class="lb-row">
        <div class="lb-rank">${medals[i] || '#'+(i+1)}</div>
        <div>
          <div class="lb-name">User #${u.id.slice(-4)}</div>
          <div class="lb-pnl ${u.pnl>=0?'pos':'neg'}">${u.pnl>=0?'+':''}${fmt(u.pnl)} P&L · ${u.shares} shares</div>
        </div>
        <div class="lb-worth">${fmt(u.net_worth)}</div>
      </div>`).join('');
    document.getElementById('lb-updated').textContent = 'Updated: ' + new Date().toLocaleTimeString();
  } catch(e) { console.error(e); }
}
fetchStock(); fetchLeaderboard();
setInterval(fetchStock, 10000);
setInterval(fetchLeaderboard, 15000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
