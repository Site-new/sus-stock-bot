import json, os, uuid, time, random

DATA_FILE = os.environ.get("DATA_FILE", "/data/data.json" if os.path.isdir("/data") else "data.json")
COMPANY_FILE = DATA_FILE.replace("data.json", "companies.json")

COMPANY_COST = 2000.0
SHARES_ISSUED = 1000
INITIAL_STOCK_PRICE = 10.0

COMPANY_TYPES = {
    "hedge_fund":     {"name": "Hedge Fund",            "emoji": "💼", "desc": "Pool money and trade SUS together. CEO controls the treasury."},
    "day_trading":    {"name": "Day Trading LLC",        "emoji": "⚡", "desc": "Members vote every hour on buy/sell. Majority rules."},
    "index_fund":     {"name": "Index Fund",             "emoji": "📊", "desc": "Auto-buys SUS every 20min. Passive and stable."},
    "insider_ring":   {"name": "Insider Trading Ring",   "emoji": "🔍", "desc": "Members see breaking news 5 minutes before the public."},
    "short_cartel":   {"name": "Short Selling Cartel",   "emoji": "🐻", "desc": "Coordinated shorts hit 2× harder on the price."},
    "pump_dump":      {"name": "Pump & Dump Crew",       "emoji": "🚀", "desc": "Mass buys spike the price 2×. CEO can trigger the dump."},
    "lending_bank":   {"name": "Lending Bank",           "emoji": "🏦", "desc": "Lend cash to users at interest. Collect every 20min."},
    "invest_bank":    {"name": "Investment Bank",        "emoji": "💳", "desc": "Earn 3% commission on every company stock trade in the market."},
    "savings":        {"name": "Savings Account",        "emoji": "🐷", "desc": "Users deposit cash and earn 3% every 20min, guaranteed."},
    "insurance":      {"name": "Insurance Company",      "emoji": "🛡️", "desc": "Charge premiums. Pay out if a user's portfolio drops 20%+."},
    "bounty_hunter":  {"name": "Bounty Hunter",          "emoji": "🎯", "desc": "Users post bounties on players. Company shorts them and splits reward."},
    "market_maker":   {"name": "Market Maker",           "emoji": "⚖️", "desc": "Set a buy/sell spread. Users trade with you instead of the market."},
    "sus_mafia":      {"name": "Sus Mafia",              "emoji": "🤌", "desc": "Charge protection from other companies. Pay or get shorted."},
    "wolf_pack":      {"name": "Wolf Pack",              "emoji": "🐺", "desc": "Mass buy votes amplify price 3×. Profits shared equally."},
}


def load_companies():
    if not os.path.exists(COMPANY_FILE):
        return {}
    with open(COMPANY_FILE, "r") as f:
        return json.load(f)


def save_companies(companies):
    tmp = COMPANY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(companies, f, indent=2)
    os.replace(tmp, COMPANY_FILE)


def company_value(company, sus_price):
    return round(company["treasury"] + company.get("sus_shares", 0) * sus_price, 2)


def company_stock_price(company, sus_price):
    val = company_value(company, sus_price)
    issued = company.get("shares_issued", SHARES_ISSUED)
    return round(max(0.01, val / issued), 4) if issued > 0 else INITIAL_STOCK_PRICE


def get_member(company, user_id):
    uid = str(user_id)
    return company.get("members", {}).get(uid)


def is_ceo(company, user_id):
    return str(user_id) == company.get("ceo")


def create_company(founder_id, name, ticker, company_type, description="", sub_price=0.0):
    cid = str(uuid.uuid4())[:8]
    return {
        "id": cid,
        "name": name,
        "ticker": ticker.upper(),
        "type": company_type,
        "description": description,
        "founder": str(founder_id),
        "ceo": str(founder_id),
        "members": {str(founder_id): {"role": "ceo", "deposit": 0, "joined_at": int(time.time())}},
        "treasury": 0.0,
        "sus_shares": 0,
        "shares_issued": SHARES_ISSUED,
        "shareholders": {str(founder_id): SHARES_ISSUED},
        "stock_price": INITIAL_STOCK_PRICE,
        "stock_history": [INITIAL_STOCK_PRICE],
        "created_at": int(time.time()),
        # Type-specific
        "loans": {},           # lending_bank: {uid: {amount, rate, due}}
        "policies": {},        # insurance: {uid: {premium, coverage, snapshot}}
        "bounties": [],        # bounty_hunter: [{target, amount, poster, expires}]
        "deposits": {},        # savings: {uid: amount}
        "spread_buy": 0,       # market_maker: buy price
        "spread_sell": 0,      # market_maker: sell price
        "protection_targets": [],  # sus_mafia: [company_id]
        "vote": None,          # day_trading/pump_dump/wolf_pack: current vote
        "pending_news": [],    # insider_ring: queued early news
        "sub_price": round(float(sub_price), 2),  # insider_ring: $/hour subscription
        "subscribers": {},     # insider_ring: {uid: {since: ts}}
    }
