import os, json, time, hmac, hashlib, random, sqlite3, threading
from urllib.parse import parse_qsl
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, LabeledPrice
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, PreCheckoutQueryHandler, ContextTypes, filters

TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://example.com/index.html")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "zeroluck.db")
DEV_MODE = os.getenv("DEV_MODE", "1") == "1"

app = Flask(__name__)

def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY, coins INTEGER DEFAULT 1000, wins INTEGER DEFAULT 0, games INTEGER DEFAULT 0, bonus_total INTEGER DEFAULT 0, last_daily TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS rocket_sessions(id TEXT PRIMARY KEY, user_id TEXT, bet INTEGER, crash REAL, start_ts REAL, active INTEGER DEFAULT 1)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS mines_sessions(id TEXT PRIMARY KEY, user_id TEXT, bet INTEGER, mine_count INTEGER, bombs TEXT, opened TEXT, active INTEGER DEFAULT 1)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS gifts(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, cost INTEGER, status TEXT DEFAULT 'pending', created_ts REAL)""")
    con.commit(); con.close()

def ensure_user(user_id):
    con=db(); cur=con.cursor()
    cur.execute("INSERT OR IGNORE INTO users(user_id, coins) VALUES(?,1000)", (str(user_id),))
    con.commit()
    cur.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),))
    row=dict(cur.fetchone()); con.close(); return row

def user_public(user_id):
    u=ensure_user(user_id)
    return {"id":str(user_id),"coins":u["coins"],"wins":u["wins"],"games":u["games"],"bonus_total":u["bonus_total"],"level":u["coins"]//1000+1}

def add_coins(user_id, amount):
    ensure_user(user_id); con=db()
    con.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (amount,str(user_id)))
    con.commit(); con.close()

def spend_coins(user_id, amount):
    ensure_user(user_id); con=db(); cur=con.cursor()
    cur.execute("SELECT coins FROM users WHERE user_id=?", (str(user_id),))
    coins=cur.fetchone()["coins"]
    if coins < amount: con.close(); return False
    cur.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (amount,str(user_id)))
    con.commit(); con.close(); return True

def inc_game(user_id, win=False):
    ensure_user(user_id); con=db()
    if win: con.execute("UPDATE users SET wins=wins+1,games=games+1 WHERE user_id=?", (str(user_id),))
    else: con.execute("UPDATE users SET games=games+1 WHERE user_id=?", (str(user_id),))
    con.commit(); con.close()

def validate_init_data(init_data):
    if not init_data or not TOKEN: return None
    parsed=dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash=parsed.pop("hash", None)
    if not received_hash: return None
    data_check_string="\n".join(f"{k}={v}" for k,v in sorted(parsed.items()))
    secret_key=hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash=hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash): return None
    user_json=parsed.get("user")
    if not user_json: return None
    return str(json.loads(user_json)["id"])

def get_user_id():
    data=request.get_json(force=True, silent=True) or {}
    user_id=validate_init_data(data.get("initData",""))
    if user_id: return user_id, data
    if DEV_MODE: return str(data.get("dev_user_id","dev_user")), data
    return None, data

def ok(payload): return jsonify({"ok":True, **payload})
def err(msg, code=400): return jsonify({"ok":False,"error":msg}), code

def rocket_multiplier(start_ts):
    elapsed=max(0,time.time()-start_ts); mult=1.0
    ticks=int(elapsed/0.09)
    for _ in range(ticks): mult += 0.035 + mult*0.006
    return round(mult,4)

def gen_crash():
    r=random.random()
    if r<0.65: return round(1.10+random.random()*1.35,4)
    if r<0.93: return round(2.45+random.random()*2.55,4)
    return round(5.0+random.random()*8.0,4)

@app.route("/")
def home(): return "ZeroLuck v10 server running"

@app.route("/api/me", methods=["POST"])
def api_me():
    uid,_=get_user_id()
    if not uid: return err("auth failed",401)
    return ok({"user":user_public(uid)})

@app.route("/api/leaderboard", methods=["POST"])
def leaderboard():
    uid,_=get_user_id()
    if not uid: return err("auth failed",401)
    con=db()
    rows=con.execute("SELECT user_id, coins, wins FROM users ORDER BY coins DESC LIMIT 20").fetchall()
    con.close()
    return ok({"items":[dict(r) for r in rows], "user":user_public(uid)})

@app.route("/api/daily", methods=["POST"])
def daily():
    uid,_=get_user_id()
    if not uid: return err("auth failed",401)
    today=time.strftime("%Y-%m-%d"); u=ensure_user(uid)
    if u["last_daily"]==today: return err("Бонус уже получен сегодня")
    reward=random.randint(300,600)
    con=db()
    con.execute("UPDATE users SET coins=coins+?, bonus_total=bonus_total+?, last_daily=? WHERE user_id=?", (reward,reward,today,uid))
    con.commit(); con.close()
    return ok({"reward":reward,"user":user_public(uid)})

@app.route("/api/rocket/start", methods=["POST"])
def rocket_start():
    uid,data=get_user_id()
    if not uid: return err("auth failed",401)
    bet=int(data.get("bet",0))
    if bet<=0: return err("bad bet")
    if not spend_coins(uid,bet): return err("Недостаточно очков")
    sid=hashlib.sha256(f"{uid}:{time.time()}:{random.random()}".encode()).hexdigest()[:24]
    con=db()
    con.execute("INSERT INTO rocket_sessions(id,user_id,bet,crash,start_ts,active) VALUES(?,?,?,?,?,1)", (sid,uid,bet,gen_crash(),time.time()))
    con.commit(); con.close()
    return ok({"session_id":sid,"user":user_public(uid)})

@app.route("/api/rocket/status", methods=["POST"])
def rocket_status():
    uid,data=get_user_id()
    if not uid: return err("auth failed",401)
    sid=data.get("session_id")
    con=db(); cur=con.cursor()
    cur.execute("SELECT * FROM rocket_sessions WHERE id=? AND user_id=?", (sid,uid))
    row=cur.fetchone()
    if not row: con.close(); return err("session not found")
    row=dict(row)
    if not row["active"]: con.close(); return ok({"status":"ended","user":user_public(uid)})
    mult=rocket_multiplier(row["start_ts"])
    if mult>=row["crash"]:
        cur.execute("UPDATE rocket_sessions SET active=0 WHERE id=?", (sid,))
        con.commit(); con.close(); inc_game(uid,False)
        return ok({"status":"lost","crash":row["crash"],"user":user_public(uid)})
    con.close(); return ok({"status":"active","multiplier":mult,"user":user_public(uid)})

@app.route("/api/rocket/cashout", methods=["POST"])
def rocket_cashout():
    uid,data=get_user_id()
    if not uid: return err("auth failed",401)
    sid=data.get("session_id")
    con=db(); cur=con.cursor()
    cur.execute("SELECT * FROM rocket_sessions WHERE id=? AND user_id=?", (sid,uid))
    row=cur.fetchone()
    if not row: con.close(); return err("session not found")
    row=dict(row)
    if not row["active"]: con.close(); return err("session ended")
    mult=rocket_multiplier(row["start_ts"])
    cur.execute("UPDATE rocket_sessions SET active=0 WHERE id=?", (sid,))
    con.commit(); con.close()
    if mult>=row["crash"]:
        inc_game(uid,False)
        return ok({"status":"lost","crash":row["crash"],"user":user_public(uid)})
    win=int(row["bet"]*mult); add_coins(uid,win); inc_game(uid,True)
    return ok({"status":"win","win":win,"multiplier":round(mult,2),"user":user_public(uid)})

@app.route("/api/mines/start", methods=["POST"])
def mines_start():
    uid,data=get_user_id()
    if not uid: return err("auth failed",401)
    bet=int(data.get("bet",0)); count=int(data.get("mines",5))
    if bet<=0: return err("bad bet")
    if count not in (3,5,7): return err("bad mines count")
    if not spend_coins(uid,bet): return err("Недостаточно очков")
    bombs=[]
    while len(bombs)<count:
        n=random.randrange(25)
        if n not in bombs: bombs.append(n)
    sid=hashlib.sha256(f"m:{uid}:{time.time()}:{random.random()}".encode()).hexdigest()[:24]
    con=db()
    con.execute("INSERT INTO mines_sessions(id,user_id,bet,mine_count,bombs,opened,active) VALUES(?,?,?,?,?,?,1)", (sid,uid,bet,count,json.dumps(bombs),json.dumps([])))
    con.commit(); con.close()
    return ok({"session_id":sid,"user":user_public(uid)})

def mines_mult(opened_count, mine_count):
    return round(1 + opened_count*(0.28+(mine_count/5)*0.22),4)

@app.route("/api/mines/open", methods=["POST"])
def mines_open():
    uid,data=get_user_id()
    if not uid: return err("auth failed",401)
    sid=data.get("session_id"); cell=int(data.get("cell",-1))
    if cell<0 or cell>24: return err("bad cell")
    con=db(); cur=con.cursor()
    cur.execute("SELECT * FROM mines_sessions WHERE id=? AND user_id=?", (sid,uid))
    row=cur.fetchone()
    if not row: con.close(); return err("session not found")
    row=dict(row)
    if not row["active"]: con.close(); return err("session ended")
    bombs=json.loads(row["bombs"]); opened=json.loads(row["opened"])
    if cell in opened: con.close(); return err("cell already opened")
    if cell in bombs:
        cur.execute("UPDATE mines_sessions SET active=0 WHERE id=?", (sid,))
        con.commit(); con.close(); inc_game(uid,False)
        return ok({"status":"lost","bombs":bombs,"user":user_public(uid)})
    opened.append(cell)
    cur.execute("UPDATE mines_sessions SET opened=? WHERE id=?", (json.dumps(opened),sid))
    con.commit(); con.close()
    return ok({"status":"safe","opened":opened,"multiplier":mines_mult(len(opened),row["mine_count"]),"user":user_public(uid)})

@app.route("/api/mines/cashout", methods=["POST"])
def mines_cashout():
    uid,data=get_user_id()
    if not uid: return err("auth failed",401)
    sid=data.get("session_id")
    con=db(); cur=con.cursor()
    cur.execute("SELECT * FROM mines_sessions WHERE id=? AND user_id=?", (sid,uid))
    row=cur.fetchone()
    if not row: con.close(); return err("session not found")
    row=dict(row)
    if not row["active"]: con.close(); return err("session ended")
    opened=json.loads(row["opened"])
    if len(opened)==0: con.close(); return err("Открой хотя бы одну клетку")
    mult=mines_mult(len(opened),row["mine_count"]); win=int(row["bet"]*mult)
    cur.execute("UPDATE mines_sessions SET active=0 WHERE id=?", (sid,))
    con.commit(); con.close()
    add_coins(uid,win); inc_game(uid,True)
    return ok({"win":win,"multiplier":round(mult,2),"user":user_public(uid)})

@app.route("/api/gift/request", methods=["POST"])
def gift_request():
    uid,_=get_user_id()
    if not uid: return err("auth failed",401)
    cost=5000
    if not spend_coins(uid,cost): return err("Нужно 5000 очков")
    con=db(); cur=con.cursor()
    cur.execute("INSERT INTO gifts(user_id,cost,status,created_ts) VALUES(?,?,?,?)", (uid,cost,"pending",time.time()))
    gift_id=cur.lastrowid
    con.commit(); con.close()
    return ok({"gift_id":gift_id,"user":user_public(uid)})

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb=[[InlineKeyboardButton("🎲 Открыть ZeroLuck", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text("🎲 ZeroLuck v10\n\nОткрой Mini App 👇", reply_markup=InlineKeyboardMarkup(kb))

async def webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: data=json.loads(update.message.web_app_data.data)
    except Exception:
        await update.message.reply_text("Ошибка данных"); return
    uid=str(update.effective_user.id); ensure_user(uid)
    if data.get("action")=="buy":
        stars=int(data.get("stars",1))
        if stars not in (1,5,10): stars=1
        await context.bot.send_invoice(chat_id=update.effective_chat.id,title="ZeroLuck Coins",description=f"Покупка за {stars} ⭐",payload=f"buy:{stars}",provider_token="",currency="XTR",prices=[LabeledPrice(f"{stars} Stars", stars)])
    elif data.get("action")=="gift":
        await update.message.reply_text("🎁 Заявка на подарок отправлена.")

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    payload=update.message.successful_payment.invoice_payload
    stars=int(payload.split(":")[1]) if payload.startswith("buy:") else 1
    reward={1:1000,5:7000,10:16000}.get(stars,1000)
    add_coins(uid,reward)
    await update.message.reply_text(f"✅ Оплата прошла! Начислено +{reward} очков.")

async def admin_gifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID: return
    con=db(); rows=con.execute("SELECT * FROM gifts WHERE status='pending' ORDER BY id DESC LIMIT 20").fetchall(); con.close()
    if not rows: await update.message.reply_text("Нет заявок."); return
    kb=[]
    text="🎁 Заявки:\n\n"
    for r in rows:
        text += f"#{r['id']} | user {r['user_id']} | cost {r['cost']}\n"
        kb.append([InlineKeyboardButton(f"✅ Закрыть #{r['id']}", callback_data=f"gift_done:{r['id']}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    if ADMIN_ID and q.from_user.id != ADMIN_ID: return
    if q.data.startswith("gift_done:"):
        gid=int(q.data.split(":")[1])
        con=db(); con.execute("UPDATE gifts SET status='done' WHERE id=?", (gid,)); con.commit(); con.close()
        await q.message.reply_text(f"✅ Заявка #{gid} закрыта.")

def run_bot():
    print("🤖 BOT THREAD STARTING...", flush=True)

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("gifts", admin_gifts))
    application.add_handler(CallbackQueryHandler(admin_button))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, webapp_data))
    application.add_handler(PreCheckoutQueryHandler(precheckout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))

    print("✅ BOT POLLING STARTED", flush=True)
    application.run_polling(close_loop=False)


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не найден")

    init_db()

    # бот запускаем отдельным потоком
    threading.Thread(target=run_bot, daemon=True).start()

    print("🌐 FLASK SERVER STARTING...", flush=True)

    # Flask держит Render живым
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )


if __name__ == "__main__":
    main()
