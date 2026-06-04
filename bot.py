import discord
from discord.ext import commands, tasks
import json
import os
import random
import time
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta, timezone
from headlines import get_headline

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

ALLOWED_CHANNEL = "sus-stock"
SUS_ONLY_CHANNEL = "sus-only"
CHAT_CHANNEL = "susstock-chat"

DATA_FILE = os.environ.get("DATA_FILE", "/data/data.json" if os.path.isdir("/data") else "data.json")
CHAT_FILE = DATA_FILE.replace("data.json", "chat.json")
CHAT_META_FILE = DATA_FILE.replace("data.json", "chat_meta.json")
STARTING_BALANCE = 1000.0
STOCK_NAME = "SUS"

MIN_PRICE = 5.0
MAX_PRICE = 500.0
BASE_PRICE = 50.0

CST = timezone(timedelta(hours=-6))

def is_market_open():
    """Open 12pm–12am CST."""
    return datetime.now(CST).hour >= 12

def add_news_event(data, headline, positive, price_impact_pct):
    """Append an event to the news feed stored in data.json."""
    events = data.get("news_feed", [])
    events.append({
        "headline": headline,
        "positive": positive,
        "impact": round(price_impact_pct, 2),
        "ts": int(time.time()),
    })
    data["news_feed"] = events[-50:]  # keep last 50

# Webhook message IDs — loaded from disk so restarts keep editing the same messages
chart_webhook = None

def load_msg_ids():
    try:
        with open(DATA_FILE, "r") as f:
            d = json.load(f)
        return d.get("_msg_ids", {})
    except Exception:
        return {}

def save_msg_ids(ids):
    try:
        with open(DATA_FILE, "r") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d["_msg_ids"] = ids
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=2)

_msg_ids = {}  # populated on startup


def load_chat():
    if not os.path.exists(CHAT_FILE):
        return []
    with open(CHAT_FILE, "r") as f:
        return json.load(f)

def save_chat(messages):
    with open(CHAT_FILE, "w") as f:
        json.dump(messages[-200:], f)

def save_chat_meta(meta):
    with open(CHAT_META_FILE, "w") as f:
        json.dump(meta, f)

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}, "stock_price": BASE_PRICE, "price_history": [BASE_PRICE], "timestamps": []}
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


def format_price(amount):
    return f"${amount:,.2f}"


def build_chart(history, current_price):
    prices = history[-60:]  # last 60 data points
    xs = list(range(len(prices)))

    up = prices[-1] >= prices[0]
    line_color = "#57F287" if up else "#ED4245"
    fill_color = "#57F28740" if up else "#ED424540"

    fig, ax = plt.subplots(figsize=(8, 3.5))
    fig.patch.set_facecolor("#2B2D31")
    ax.set_facecolor("#2B2D31")

    ax.plot(xs, prices, color=line_color, linewidth=2, zorder=3)
    ax.fill_between(xs, prices, min(prices) * 0.98, color=fill_color, zorder=2)

    ax.set_xlim(0, max(len(prices) - 1, 1))
    ax.set_ylim(min(prices) * 0.95, max(prices) * 1.05)

    ax.tick_params(colors="#AAAAAA", labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor("#444444")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:.2f}"))
    ax.set_xlabel("← older     newer →", color="#888888", fontsize=7)
    ax.set_title(f"SUS Stock  —  {format_price(current_price)}", color="white", fontsize=13, fontweight="bold", pad=10)
    ax.grid(axis="y", color="#3A3C40", linewidth=0.5, zorder=1)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", dpi=130, facecolor=fig.get_facecolor())
    buf.seek(0)
    plt.close(fig)
    return buf


# ── Buy / Sell modals and buttons ──────────────────────────────────────────────

class BuyModal(discord.ui.Modal, title="Buy SUS Stock"):
    shares_input = discord.ui.TextInput(label="How many shares?", min_length=1, max_length=6)

    def __init__(self, price, balance):
        super().__init__()
        self.shares_input.placeholder = f"Price: {format_price(price)}  |  Your cash: {format_price(balance)}"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            shares = int(self.shares_input.value)
            if shares <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Enter a positive whole number.", ephemeral=True)
            return

        data = load_data()
        user = get_user(data, interaction.user.id)
        price = data["stock_price"]
        total_cost = round(price * shares, 2)

        if user["balance"] < total_cost:
            max_shares = int(user["balance"] // price)
            await interaction.response.send_message(
                f"❌ Not enough cash. Need {format_price(total_cost)}, have {format_price(user['balance'])}.\n"
                f"You can afford up to **{max_shares} shares**.",
                ephemeral=True,
            )
            return

        user["balance"] = round(user["balance"] - total_cost, 2)
        user["shares"] += shares
        save_data(data)

        embed = discord.Embed(title="✅ Purchase Successful", color=0x57F287)
        embed.add_field(name="Bought", value=f"{shares} SUS @ {format_price(price)}", inline=True)
        embed.add_field(name="Total Cost", value=format_price(total_cost), inline=True)
        embed.add_field(name="Remaining Cash", value=format_price(user["balance"]), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class SellModal(discord.ui.Modal, title="Sell SUS Stock"):
    shares_input = discord.ui.TextInput(label="How many shares?", min_length=1, max_length=6)

    def __init__(self, price, owned):
        super().__init__()
        self.shares_input.placeholder = f"Price: {format_price(price)}  |  You own: {owned} shares"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            shares = int(self.shares_input.value)
            if shares <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Enter a positive whole number.", ephemeral=True)
            return

        data = load_data()
        user = get_user(data, interaction.user.id)
        price = data["stock_price"]

        if user["shares"] < shares:
            await interaction.response.send_message(
                f"❌ You only have **{user['shares']} shares** to sell.", ephemeral=True
            )
            return

        earnings = round(price * shares, 2)
        user["shares"] -= shares
        user["balance"] = round(user["balance"] + earnings, 2)
        save_data(data)

        embed = discord.Embed(title="✅ Sale Successful", color=0x57F287)
        embed.add_field(name="Sold", value=f"{shares} SUS @ {format_price(price)}", inline=True)
        embed.add_field(name="Earnings", value=format_price(earnings), inline=True)
        embed.add_field(name="New Cash Balance", value=format_price(user["balance"]), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class PortfolioView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="My Portfolio", style=discord.ButtonStyle.primary, emoji="📊", custom_id="sus_portfolio")
    async def portfolio_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        user = get_user(data, interaction.user.id)
        save_data(data)
        price = data["stock_price"]

        invested = round(user["shares"] * price, 2)
        net_worth = round(user["balance"] + invested, 2)
        pnl = round(net_worth - STARTING_BALANCE, 2)
        pnl_str = f"{'+' if pnl >= 0 else ''}{format_price(pnl)}"
        color = 0x57F287 if pnl >= 0 else 0xED4245

        embed = discord.Embed(title=f"📊 {interaction.user.display_name}'s Portfolio", color=color)
        embed.add_field(name="Net Worth", value=format_price(net_worth), inline=True)
        embed.add_field(name="All-Time P&L", value=pnl_str, inline=True)
        embed.add_field(name="​", value="​", inline=False)
        embed.add_field(name="SUS Shares", value=str(user["shares"]), inline=True)
        embed.add_field(name="Invested Value", value=format_price(invested), inline=True)
        embed.add_field(name="Cash (uninvested)", value=format_price(user["balance"]), inline=True)
        embed.set_footer(text=f"SUS price: {format_price(price)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class TradeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.success, emoji="📈", custom_id="sus_buy")
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        user = get_user(data, interaction.user.id)
        save_data(data)
        await interaction.response.send_modal(BuyModal(data["stock_price"], user["balance"]))

    @discord.ui.button(label="Sell", style=discord.ButtonStyle.danger, emoji="📉", custom_id="sus_sell")
    async def sell_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        user = get_user(data, interaction.user.id)
        save_data(data)
        await interaction.response.send_modal(SellModal(data["stock_price"], user["shares"]))


@bot.check
async def only_in_sus_stock(ctx):
    if ctx.channel.name != ALLOWED_CHANNEL:
        await ctx.send(f"❌ Sus Stock commands can only be used in **#sus-stock**.")
        return False
    return True


@bot.event
async def on_ready():
    global _msg_ids
    print(f"Sus Stock bot is online as {bot.user}")
    bot.add_view(TradeView())
    bot.add_view(PortfolioView())
    _msg_ids = load_msg_ids()
    await bot.tree.sync()
    print("Slash commands synced.")
    bot.loop.create_task(startup_messages())
    fluctuate_price.start()
    update_price_target.start()
    earnings_report.start()
    pay_dividends.start()
    update_bull_bear.start()
    insider_tip.start()


@bot.event
async def on_message(message):
    if message.author.bot:
        await bot.process_commands(message)
        return

    if message.channel.name.lower() == CHAT_CHANNEL.lower():
        # Save channel ID so server.py can mirror web messages back here
        save_chat_meta({"discord_channel_id": str(message.channel.id)})
        # Bridge Discord message to website chat
        if message.content.strip():
            messages = load_chat()
            msg_id = (messages[-1]["id"] + 1) if messages else 1
            avatar_hash = str(message.author.avatar) if message.author.avatar else None
            messages.append({
                "id": msg_id,
                "user_id": str(message.author.id),
                "username": message.author.display_name,
                "avatar": avatar_hash,
                "text": message.content,
                "ts": int(time.time()),
                "source": "discord",
            })
            save_chat(messages)
            print(f"[chat] Discord→Web: {message.author.display_name}: {message.content}")
        await bot.process_commands(message)
        return

    if message.channel.name == SUS_ONLY_CHANNEL:
        if message.content.strip().lower() != "sus":
            try:
                await message.delete()
                await message.author.timeout(timedelta(minutes=5), reason="Only 'sus' is allowed in this channel.")
                warn = await message.channel.send(
                    f"🔇 {message.author.mention} only **sus** is allowed here. You've been timed out for 5 minutes.",
                    delete_after=8,
                )
            except discord.Forbidden:
                pass
            return

    await bot.process_commands(message)


async def startup_messages():
    """Post chart → leaderboard → portfolio in order on startup."""
    await bot.wait_until_ready()

    channel = None
    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name=ALLOWED_CHANNEL)
        if channel:
            break
    if not channel:
        return

    global chart_webhook
    try:
        hooks = await channel.webhooks()
        chart_webhook = next((w for w in hooks if w.name == "Sus Stock Chart"), None)
        if chart_webhook is None:
            chart_webhook = await channel.create_webhook(name="Sus Stock Chart")
    except Exception as e:
        print(f"Webhook setup error: {e}")
        return

    await post_chart()
    await post_leaderboard()
    await post_portfolio()


# ── Slash commands ─────────────────────────────────────────────────────────────

@bot.tree.command(name="buy", description="Buy shares of SUS stock")
async def slash_buy(interaction: discord.Interaction, shares: int):
    if interaction.channel.name != ALLOWED_CHANNEL:
        await interaction.response.send_message(f"❌ Use this in **#sus-stock**.", ephemeral=True)
        return
    if shares <= 0:
        await interaction.response.send_message("Please enter a positive number of shares.", ephemeral=True)
        return

    data = load_data()
    user = get_user(data, interaction.user.id)
    price = data["stock_price"]
    total_cost = round(price * shares, 2)

    if user["balance"] < total_cost:
        max_shares = int(user["balance"] // price)
        await interaction.response.send_message(
            f"❌ Not enough cash. You need {format_price(total_cost)} but only have {format_price(user['balance'])}.\n"
            f"You can afford up to **{max_shares} shares**.",
            ephemeral=True,
        )
        return

    user["balance"] = round(user["balance"] - total_cost, 2)
    user["shares"] += shares
    save_data(data)

    embed = discord.Embed(title="✅ Purchase Successful", color=0x57F287)
    embed.add_field(name="Bought", value=f"{shares} SUS @ {format_price(price)}", inline=True)
    embed.add_field(name="Total Cost", value=format_price(total_cost), inline=True)
    embed.add_field(name="Remaining Cash", value=format_price(user["balance"]), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="sell", description="Sell shares of SUS stock")
async def slash_sell(interaction: discord.Interaction, shares: int):
    if interaction.channel.name != ALLOWED_CHANNEL:
        await interaction.response.send_message(f"❌ Use this in **#sus-stock**.", ephemeral=True)
        return
    if shares <= 0:
        await interaction.response.send_message("Please enter a positive number of shares.", ephemeral=True)
        return

    data = load_data()
    user = get_user(data, interaction.user.id)
    price = data["stock_price"]

    if user["shares"] < shares:
        await interaction.response.send_message(
            f"❌ You only have **{user['shares']} shares** to sell.", ephemeral=True
        )
        return

    earnings = round(price * shares, 2)
    user["shares"] -= shares
    user["balance"] = round(user["balance"] + earnings, 2)
    save_data(data)

    embed = discord.Embed(title="✅ Sale Successful", color=0x57F287)
    embed.add_field(name="Sold", value=f"{shares} SUS @ {format_price(price)}", inline=True)
    embed.add_field(name="Earnings", value=format_price(earnings), inline=True)
    embed.add_field(name="New Cash Balance", value=format_price(user["balance"]), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)



@bot.tree.command(name="networth", description="Check your or another user's net worth")
async def slash_networth(interaction: discord.Interaction, user: discord.Member = None):
    if interaction.channel.name != ALLOWED_CHANNEL:
        await interaction.response.send_message(f"❌ Use this in **#sus-stock**.", ephemeral=True)
        return

    target = user or interaction.user
    data = load_data()
    u = get_user(data, target.id)
    save_data(data)
    price = data["stock_price"]

    stock_value = round(u["shares"] * price, 2)
    net_worth = round(u["balance"] + stock_value, 2)
    pnl = round(net_worth - STARTING_BALANCE, 2)
    pnl_str = f"{'+' if pnl >= 0 else ''}{format_price(pnl)}"
    color = 0x57F287 if pnl >= 0 else 0xED4245

    embed = discord.Embed(title=f"📊 {target.display_name}'s Net Worth", color=color)
    embed.add_field(name="Cash", value=format_price(u["balance"]), inline=True)
    embed.add_field(name="SUS Shares", value=str(u["shares"]), inline=True)
    embed.add_field(name="Stock Value", value=format_price(stock_value), inline=True)
    embed.add_field(name="Net Worth", value=format_price(net_worth), inline=True)
    embed.add_field(name="All-Time P&L", value=pnl_str, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="short", description="Short sell SUS stock (profit if price drops)")
async def slash_short(interaction: discord.Interaction, shares: int):
    if interaction.channel.name != ALLOWED_CHANNEL:
        await interaction.response.send_message("❌ Use this in **#sus-stock**.", ephemeral=True)
        return
    if shares <= 0:
        await interaction.response.send_message("Enter a positive number of shares.", ephemeral=True)
        return
    data = load_data()
    uid = str(interaction.user.id)
    if uid in data.get("shorts", {}):
        await interaction.response.send_message("❌ You already have an open short. Use `/cover` first.", ephemeral=True)
        return
    price = data["stock_price"]
    get_user(data, uid)
    data.setdefault("shorts", {})[uid] = {"shares": shares, "entry_price": price}
    save_data(data)
    embed = discord.Embed(title="📉 Short Position Opened", color=0xED4245)
    embed.add_field(name="Shorted", value=f"{shares} SUS @ {format_price(price)}", inline=True)
    embed.add_field(name="Profit if price drops to", value=format_price(price * 0.8), inline=True)
    embed.set_footer(text="Use /cover to close your short position.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="cover", description="Close your short position")
async def slash_cover(interaction: discord.Interaction):
    if interaction.channel.name != ALLOWED_CHANNEL:
        await interaction.response.send_message("❌ Use this in **#sus-stock**.", ephemeral=True)
        return
    data = load_data()
    uid = str(interaction.user.id)
    shorts = data.get("shorts", {})
    if uid not in shorts:
        await interaction.response.send_message("❌ You don't have an open short position.", ephemeral=True)
        return
    short = shorts.pop(uid)
    price = data["stock_price"]
    pnl = round((short["entry_price"] - price) * short["shares"], 2)
    u = get_user(data, uid)
    u["balance"] = round(max(0, u["balance"] + pnl), 2)
    data["shorts"] = shorts
    save_data(data)
    color = 0x57F287 if pnl >= 0 else 0xED4245
    embed = discord.Embed(title="✅ Short Covered", color=color)
    embed.add_field(name="Entry", value=format_price(short["entry_price"]), inline=True)
    embed.add_field(name="Exit", value=format_price(price), inline=True)
    pnl_str = f"{'+' if pnl >= 0 else ''}{format_price(pnl)}"
    embed.add_field(name="P&L", value=pnl_str, inline=True)
    embed.add_field(name="New Balance", value=format_price(u["balance"]), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="limitbuy", description="Auto-buy when price drops to your target")
async def slash_limitbuy(interaction: discord.Interaction, shares: int, price: float):
    if interaction.channel.name != ALLOWED_CHANNEL:
        await interaction.response.send_message("❌ Use this in **#sus-stock**.", ephemeral=True)
        return
    data = load_data()
    u = get_user(data, interaction.user.id)
    cost = round(shares * price, 2)
    if u["balance"] < cost:
        await interaction.response.send_message(f"❌ Not enough cash. Need {format_price(cost)}.", ephemeral=True)
        return
    u["balance"] = round(u["balance"] - cost, 2)  # reserve funds
    data.setdefault("limit_orders", []).append({
        "user_id": str(interaction.user.id), "type": "buy", "shares": shares, "price": round(price, 2)
    })
    save_data(data)
    embed = discord.Embed(title="✅ Limit Buy Set", color=0x57F287)
    embed.add_field(name="Buy", value=f"{shares} SUS if price ≤ {format_price(price)}", inline=False)
    embed.add_field(name="Funds Reserved", value=format_price(cost), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="limitsell", description="Auto-sell when price rises to your target")
async def slash_limitsell(interaction: discord.Interaction, shares: int, price: float):
    if interaction.channel.name != ALLOWED_CHANNEL:
        await interaction.response.send_message("❌ Use this in **#sus-stock**.", ephemeral=True)
        return
    data = load_data()
    u = get_user(data, interaction.user.id)
    if u["shares"] < shares:
        await interaction.response.send_message(f"❌ You only have {u['shares']} shares.", ephemeral=True)
        return
    u["shares"] -= shares  # reserve shares
    data.setdefault("limit_orders", []).append({
        "user_id": str(interaction.user.id), "type": "sell", "shares": shares, "price": round(price, 2)
    })
    save_data(data)
    embed = discord.Embed(title="✅ Limit Sell Set", color=0x57F287)
    embed.add_field(name="Sell", value=f"{shares} SUS if price ≥ {format_price(price)}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="market", description="Check market status, sentiment, and cycle")
async def slash_market(interaction: discord.Interaction):
    if interaction.channel.name != ALLOWED_CHANNEL:
        await interaction.response.send_message("❌ Use this in **#sus-stock**.", ephemeral=True)
        return
    data = load_data()
    open_status = "🟢 OPEN" if is_market_open() else "🔴 CLOSED (opens 12pm CST)"
    cycle = data.get("bull_bear", "neutral")
    cycle_label = "🐂 Bull" if cycle == "bull" else ("🐻 Bear" if cycle == "bear" else "😐 Neutral")
    sentiment = data.get("sentiment", 50)
    sentiment_label = "Extreme Greed 😏" if sentiment > 75 else ("Greed 😌" if sentiment > 55 else ("Fear 😰" if sentiment < 25 else ("Neutral 😐" if sentiment < 45 else "Caution ⚠️")))
    embed = discord.Embed(title="📊 Market Status", color=0x5865F2)
    embed.add_field(name="Status", value=open_status, inline=True)
    embed.add_field(name="Cycle", value=cycle_label, inline=True)
    embed.add_field(name="Sentiment", value=f"{sentiment}/100 — {sentiment_label}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def post_chart():
    global _msg_ids
    if chart_webhook is None:
        return
    data = load_data()
    price = data["stock_price"]
    history = data.get("price_history", [price])

    buf = build_chart(history, price)
    file = discord.File(buf, filename="sus_stock.png")
    prev = history[-2] if len(history) >= 2 else price
    change = price - prev
    pct = (change / prev * 100) if prev else 0
    arrow = "📈" if change >= 0 else "📉"
    color = 0x57F287 if change >= 0 else 0xED4245

    embed = discord.Embed(title=f"{arrow} SUS Live Chart", color=color)
    embed.add_field(name="Price", value=format_price(price), inline=True)
    embed.add_field(name="Change", value=f"{'+' if change >= 0 else ''}{format_price(change)} ({pct:+.2f}%)", inline=True)
    embed.set_image(url="attachment://sus_stock.png")
    embed.set_footer(text=f"Last updated {datetime.now().strftime('%H:%M:%S')}")

    mid = _msg_ids.get("chart")
    try:
        if mid is None:
            msg = await chart_webhook.send(embed=embed, file=file, view=TradeView(), wait=True)
            _msg_ids["chart"] = msg.id
            save_msg_ids(_msg_ids)
        else:
            await chart_webhook.edit_message(mid, embed=embed, attachments=[file], view=TradeView())
    except Exception as e:
        print(f"Chart update error: {e}")
        _msg_ids.pop("chart", None)
        save_msg_ids(_msg_ids)


async def post_leaderboard():
    global _msg_ids
    if chart_webhook is None:
        return
    data = load_data()
    price = data["stock_price"]

    rankings = [(uid, round(u["balance"] + u["shares"] * price, 2)) for uid, u in data["users"].items()]
    rankings.sort(key=lambda x: x[1], reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, worth) in enumerate(rankings[:10]):
        medal = medals[i] if i < 3 else f"**#{i+1}**"
        try:
            member = await bot.fetch_user(int(uid))
            name = member.display_name
        except Exception:
            name = f"User {uid}"
        lines.append(f"{medal} **{name}** — {format_price(worth)}")

    embed = discord.Embed(title="🏆 Sus Stock Leaderboard", color=0xFEE75C)
    embed.description = "\n".join(lines) if lines else "No traders yet — be the first!"
    embed.set_footer(text=f"SUS price: {format_price(price)}  •  Updated {datetime.now().strftime('%H:%M:%S')}")

    mid = _msg_ids.get("leaderboard")
    try:
        if mid is None:
            msg = await chart_webhook.send(embed=embed, wait=True)
            _msg_ids["leaderboard"] = msg.id
            save_msg_ids(_msg_ids)
        else:
            await chart_webhook.edit_message(mid, embed=embed)
    except Exception as e:
        print(f"Leaderboard update error: {e}")
        _msg_ids.pop("leaderboard", None)
        save_msg_ids(_msg_ids)


async def post_portfolio():
    global _msg_ids
    if chart_webhook is None:
        return
    mid = _msg_ids.get("portfolio")
    if mid is not None:
        return  # already posted, never needs re-posting
    embed = discord.Embed(
        title="📊 Your Portfolio",
        description="Click the button below to privately view your shares, net worth, cash, and invested value.",
        color=0x5865F2,
    )
    try:
        msg = await chart_webhook.send(embed=embed, view=PortfolioView(), wait=True)
        _msg_ids["portfolio"] = msg.id
        save_msg_ids(_msg_ids)
    except Exception as e:
        print(f"Portfolio post error: {e}")


@tasks.loop(hours=1)
async def update_price_target():
    """Pick a new random target every hour for the price to drift toward."""
    data = load_data()
    new_target = round(random.uniform(MIN_PRICE, MAX_PRICE), 2)
    data["price_target"] = new_target
    save_data(data)
    print(f"[price_target] New hourly target: ${new_target}")


@update_price_target.before_loop
async def before_target():
    await bot.wait_until_ready()


@tasks.loop(seconds=30)
async def fluctuate_price():
    try:
        if not is_market_open():
            return

        data = load_data()
        price = data["stock_price"]
        target = data.get("price_target", random.uniform(MIN_PRICE, MAX_PRICE))

        # Bull/Bear cycle bias
        cycle = data.get("bull_bear", "neutral")
        bias = 0.025 if cycle == "bull" else (-0.025 if cycle == "bear" else 0)

        # Fear/Greed sentiment (0=fear, 100=greed) affects volatility
        sentiment = data.get("sentiment", 50)
        volatility = 0.03 + (abs(sentiment - 50) / 50) * 0.05  # 3%-8%

        # Flash crash: 0.8% chance per tick
        flash_crash = False
        if random.random() < 0.008:
            flash_crash = True
            crash_pct = random.uniform(-0.45, -0.30)
            change_pct = crash_pct
            headline = "⚡ FLASH CRASH: Sus Corp Plummets in Seconds!"
            add_news_event(data, headline, False, crash_pct * 100)
            print(f"[flash_crash] {crash_pct:.1%}")
        else:
            change_pct = random.uniform(-volatility, volatility) + bias

        reversion = (target - price) * 0.04
        new_price = round(max(MIN_PRICE, min(MAX_PRICE, price * (1 + change_pct) + reversion)), 2)
        if new_price == price:
            return

        # Stock split: if price >= 400, split 2-for-1
        if new_price >= 400:
            new_price = round(new_price / 2, 2)
            for uid, u in data["users"].items():
                u["shares"] *= 2
                # Also double short positions
                if "shorts" in data and uid in data["shorts"]:
                    data["shorts"][uid]["shares"] *= 2
                    data["shorts"][uid]["entry_price"] /= 2
            headline = f"📊 Sus Corp Stock Split 2-for-1 at ${new_price*2:.2f}! All shares doubled."
            add_news_event(data, headline, True, 0)
            print(f"[split] 2-for-1 at ${new_price*2:.2f}")

        data["stock_price"] = new_price
        history = data.get("price_history", [])
        history.append(new_price)
        if len(history) > 200:
            history = history[-200:]
        data["price_history"] = history
        timestamps = data.get("price_timestamps", [])
        timestamps.append(datetime.now(CST).strftime("%H:%M"))
        if len(timestamps) > 200:
            timestamps = timestamps[-200:]
        data["price_timestamps"] = timestamps

        # Check limit orders
        remaining_orders = []
        for order in data.get("limit_orders", []):
            uid = order["user_id"]
            u = data["users"].get(uid, {"balance": 0, "shares": 0})
            executed = False
            if order["type"] == "buy" and new_price <= order["price"]:
                cost = round(new_price * order["shares"], 2)
                if u["balance"] >= cost:
                    u["balance"] = round(u["balance"] - cost, 2)
                    u["shares"] += order["shares"]
                    executed = True
                    print(f"[limit] buy {order['shares']} for {uid} at {new_price}")
            elif order["type"] == "sell" and new_price >= order["price"]:
                if u["shares"] >= order["shares"]:
                    u["shares"] -= order["shares"]
                    u["balance"] = round(u["balance"] + new_price * order["shares"], 2)
                    executed = True
                    print(f"[limit] sell {order['shares']} for {uid} at {new_price}")
            if not executed:
                remaining_orders.append(order)
        data["limit_orders"] = remaining_orders

        # Auto margin call: cover shorts where loss > 150% of entry value
        for uid, short in list(data.get("shorts", {}).items()):
            loss = (new_price - short["entry_price"]) * short["shares"]
            max_loss = short["entry_price"] * short["shares"] * 1.5
            if loss >= max_loss:
                u = data["users"].get(uid, {"balance": 0, "shares": 0})
                u["balance"] = round(max(0, u["balance"] - loss), 2)
                del data["shorts"][uid]
                print(f"[margin_call] {uid} auto-covered at {new_price}")

        save_data(data)
    except Exception as e:
        print(f"[fluctuate_price] data error: {e}")
        return

    try:
        await post_chart()
    except Exception as e:
        print(f"[fluctuate_price] chart error: {e}")

    try:
        await post_leaderboard()
    except Exception as e:
        print(f"[fluctuate_price] leaderboard error: {e}")


@fluctuate_price.error
async def fluctuate_price_error(error):
    print(f"[fluctuate_price] task error: {error}")
    fluctuate_price.restart()


@fluctuate_price.before_loop
async def before_fluctuate():
    await bot.wait_until_ready()


@tasks.loop(minutes=20)
async def earnings_report():
    """Post a random earnings report causing a price spike or drop."""
    if not is_market_open():
        return
    data = load_data()
    positive = random.random() > 0.45
    impact_pct = random.uniform(0.08, 0.25) if positive else random.uniform(-0.22, -0.08)
    headline = get_headline(positive)
    price = data["stock_price"]
    new_price = round(max(MIN_PRICE, min(MAX_PRICE, price * (1 + impact_pct))), 2)
    data["stock_price"] = new_price
    history = data.get("price_history", [])
    history.append(new_price)
    data["price_history"] = history[-200:]
    timestamps = data.get("price_timestamps", [])
    timestamps.append(datetime.now(CST).strftime("%H:%M"))
    data["price_timestamps"] = timestamps[-200:]
    add_news_event(data, f"📰 EARNINGS: {headline}", positive, impact_pct * 100)
    save_data(data)
    print(f"[earnings] {'📈' if positive else '📉'} {impact_pct:.1%} — {headline}")
    try:
        await post_chart()
        await post_leaderboard()
    except Exception:
        pass


@earnings_report.before_loop
async def before_earnings():
    await bot.wait_until_ready()


@tasks.loop(minutes=20)
async def pay_dividends():
    """Pay $0.50 per share to all shareholders."""
    data = load_data()
    total_paid = 0
    recipients = 0
    for uid, u in data["users"].items():
        if u["shares"] > 0:
            payout = round(u["shares"] * 0.50, 2)
            u["balance"] = round(u["balance"] + payout, 2)
            total_paid += payout
            recipients += 1
    if recipients > 0:
        add_news_event(data, f"💵 Dividend payout: $0.50/share paid to {recipients} shareholders (${total_paid:.2f} total)", True, 0)
        save_data(data)
        print(f"[dividends] Paid ${total_paid:.2f} to {recipients} users")


@pay_dividends.before_loop
async def before_dividends():
    await bot.wait_until_ready()


@tasks.loop(minutes=30)
async def update_bull_bear():
    """Randomly shift the market into bull, bear, or neutral cycle."""
    data = load_data()
    expires = data.get("bull_bear_expires", 0)
    if time.time() < expires:
        return
    cycle = random.choices(["bull", "bear", "neutral"], weights=[35, 35, 30])[0]
    duration_hours = random.randint(8, 36)
    data["bull_bear"] = cycle
    data["bull_bear_expires"] = time.time() + duration_hours * 3600
    # Adjust sentiment
    data["sentiment"] = random.randint(65, 90) if cycle == "bull" else (random.randint(10, 35) if cycle == "bear" else random.randint(40, 60))
    label = "🐂 Bull Market" if cycle == "bull" else ("🐻 Bear Market" if cycle == "bear" else "😐 Neutral Market")
    add_news_event(data, f"📊 Market Cycle Shift: {label} expected for next {duration_hours}h", cycle == "bull", 0)
    save_data(data)
    print(f"[bull_bear] {cycle} for {duration_hours}h")


@update_bull_bear.before_loop
async def before_bull_bear():
    await bot.wait_until_ready()


@tasks.loop(minutes=20)
async def insider_tip():
    """DM a random shareholder a hint about the next price direction."""
    data = load_data()
    if not is_market_open():
        return
    shareholders = [(uid, u) for uid, u in data["users"].items() if u["shares"] > 0]
    if not shareholders:
        return
    uid, _ = random.choice(shareholders)
    cycle = data.get("bull_bear", "neutral")
    target = data.get("price_target", 50)
    price = data["stock_price"]
    going_up = target > price or cycle == "bull"
    hint = (
        f"🔍 **Insider Tip:** My sources say Sus Corp is looking {'strong' if going_up else 'shaky'} right now. "
        f"{'Might be a good time to load up...' if going_up else 'You might want to be careful with your position...'} 🤫"
    )
    try:
        user = await bot.fetch_user(int(uid))
        await user.send(hint)
        print(f"[insider_tip] Sent to {user.name}")
    except Exception as e:
        print(f"[insider_tip] Failed: {e}")


@insider_tip.before_loop
async def before_insider():
    await bot.wait_until_ready()


@bot.command(name="chattest")
async def chattest_cmd(ctx):
    """Shows what channel the bot thinks is the chat channel."""
    found = discord.utils.get(ctx.guild.text_channels, name=CHAT_CHANNEL)
    if found:
        await ctx.send(f"✅ Chat channel found: **#{found.name}** (ID: `{found.id}`)")
    else:
        channels = [c.name for c in ctx.guild.text_channels]
        await ctx.send(f"❌ No channel named `{CHAT_CHANNEL}` found.\nAvailable channels: {', '.join(channels)}")


@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(
        title="📈 Sus Stock — Command List",
        description="Trade **SUS** stock and get rich (or go broke).",
        color=0x57F287,
    )
    embed.add_field(name="!price", value="Check the current SUS stock price", inline=False)
    embed.add_field(name="!balance", value="See your cash and shares", inline=False)
    embed.add_field(name="!buy <shares>", value="Buy shares of SUS stock", inline=False)
    embed.add_field(name="!sell <shares>", value="Sell shares of SUS stock", inline=False)
    embed.add_field(name="!portfolio", value="View your full portfolio and net worth", inline=False)
    embed.add_field(name="!leaderboard", value="Top traders by net worth", inline=False)
    embed.set_footer(text="Everyone starts with $1,000. Good luck. 📉📈")
    await ctx.send(embed=embed)


@bot.command(name="price")
async def price_cmd(ctx):
    data = load_data()
    price = data["stock_price"]
    history = data.get("price_history", [price])
    prev = history[-2] if len(history) >= 2 else price
    change = price - prev
    pct = (change / prev) * 100 if prev else 0
    arrow = "📈" if change >= 0 else "📉"
    color = 0x57F287 if change >= 0 else 0xED4245

    embed = discord.Embed(title=f"{arrow} SUS Stock Price", color=color)
    embed.add_field(name="Current Price", value=format_price(price), inline=True)
    change_str = f"{'+' if change >= 0 else ''}{format_price(change)} ({pct:+.2f}%)"
    embed.add_field(name="Change (last tick)", value=change_str, inline=True)
    embed.set_footer(text="Price fluctuates every 30s")
    await ctx.send(embed=embed)


@bot.command(name="balance")
async def balance_cmd(ctx):
    data = load_data()
    user = get_user(data, ctx.author.id)
    save_data(data)

    embed = discord.Embed(title=f"💰 {ctx.author.display_name}'s Balance", color=0xFEE75C)
    embed.add_field(name="Cash", value=format_price(user["balance"]), inline=True)
    embed.add_field(name="SUS Shares", value=str(user["shares"]), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="buy")
async def buy_cmd(ctx, amount: str = None):
    if amount is None:
        await ctx.send("Usage: `!buy <shares>` — e.g. `!buy 5`")
        return

    try:
        shares = int(amount)
        if shares <= 0:
            raise ValueError
    except ValueError:
        await ctx.send("Please enter a positive whole number of shares.")
        return

    data = load_data()
    user = get_user(data, ctx.author.id)
    price = data["stock_price"]
    total_cost = round(price * shares, 2)

    if user["balance"] < total_cost:
        max_shares = int(user["balance"] // price)
        await ctx.send(
            f"❌ Not enough cash. You need {format_price(total_cost)} but only have {format_price(user['balance'])}.\n"
            f"You can afford up to **{max_shares} shares**."
        )
        return

    user["balance"] = round(user["balance"] - total_cost, 2)
    user["shares"] += shares
    save_data(data)

    embed = discord.Embed(title="✅ Purchase Successful", color=0x57F287)
    embed.add_field(name="Bought", value=f"{shares} SUS @ {format_price(price)}", inline=True)
    embed.add_field(name="Total Cost", value=format_price(total_cost), inline=True)
    embed.add_field(name="Remaining Cash", value=format_price(user["balance"]), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="sell")
async def sell_cmd(ctx, amount: str = None):
    if amount is None:
        await ctx.send("Usage: `!sell <shares>` — e.g. `!sell 3`")
        return

    try:
        shares = int(amount)
        if shares <= 0:
            raise ValueError
    except ValueError:
        await ctx.send("Please enter a positive whole number of shares.")
        return

    data = load_data()
    user = get_user(data, ctx.author.id)
    price = data["stock_price"]

    if user["shares"] < shares:
        await ctx.send(f"❌ You only have **{user['shares']} shares** to sell.")
        return

    earnings = round(price * shares, 2)
    user["shares"] -= shares
    user["balance"] = round(user["balance"] + earnings, 2)
    save_data(data)

    embed = discord.Embed(title="✅ Sale Successful", color=0x57F287)
    embed.add_field(name="Sold", value=f"{shares} SUS @ {format_price(price)}", inline=True)
    embed.add_field(name="Earnings", value=format_price(earnings), inline=True)
    embed.add_field(name="New Cash Balance", value=format_price(user["balance"]), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="portfolio")
async def portfolio_cmd(ctx):
    data = load_data()
    user = get_user(data, ctx.author.id)
    price = data["stock_price"]
    save_data(data)

    stock_value = round(user["shares"] * price, 2)
    net_worth = round(user["balance"] + stock_value, 2)
    pnl = round(net_worth - STARTING_BALANCE, 2)
    pnl_str = f"{'+' if pnl >= 0 else ''}{format_price(pnl)}"
    color = 0x57F287 if pnl >= 0 else 0xED4245

    embed = discord.Embed(title=f"📊 {ctx.author.display_name}'s Portfolio", color=color)
    embed.add_field(name="Cash", value=format_price(user["balance"]), inline=True)
    embed.add_field(name="SUS Shares", value=str(user["shares"]), inline=True)
    embed.add_field(name="Stock Value", value=format_price(stock_value), inline=True)
    embed.add_field(name="Net Worth", value=format_price(net_worth), inline=True)
    embed.add_field(name="All-Time P&L", value=pnl_str, inline=True)
    await ctx.send(embed=embed)


@bot.command(name="leaderboard")
async def leaderboard_cmd(ctx):
    data = load_data()
    price = data["stock_price"]

    rankings = []
    for uid, u in data["users"].items():
        net_worth = round(u["balance"] + u["shares"] * price, 2)
        rankings.append((uid, net_worth))

    rankings.sort(key=lambda x: x[1], reverse=True)
    top = rankings[:10]

    embed = discord.Embed(title="🏆 Sus Stock Leaderboard", color=0xFEE75C)
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, worth) in enumerate(top):
        medal = medals[i] if i < 3 else f"**#{i+1}**"
        try:
            member = await bot.fetch_user(int(uid))
            name = member.display_name
        except Exception:
            name = f"User {uid}"
        lines.append(f"{medal} {name} — {format_price(worth)}")

    embed.description = "\n".join(lines) if lines else "No traders yet. Be the first!"
    embed.set_footer(text=f"Current SUS price: {format_price(price)}")
    await ctx.send(embed=embed)


@buy_cmd.error
@sell_cmd.error
async def command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Missing argument. Use `!help` to see usage.")


bot.run(TOKEN)
