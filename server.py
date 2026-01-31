import eventlet
eventlet.monkey_patch()

import os
import json
import random
import time
import asyncio
import threading
import logging
import requests
from datetime import datetime
from flask import Flask, send_from_directory, request, jsonify
from flask_socketio import SocketIO, emit
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Flask & SocketIO Config ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET', 'aviator_pro_secure_key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL').replace('postgres://', 'postgresql://') if os.environ.get('DATABASE_URL') else 'sqlite:///game.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_recycle": 300, "pool_pre_ping": True}

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- Global Variables ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID')
DOMAIN = os.environ.get('RENDER_EXTERNAL_URL') # Render በራሱ የሚሰጠው

app_bot = None
bot_loop = None

# --- Database Models ---
class GameData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(JSONB, nullable=False)

def load_data_from_db():
    with app.app_context():
        db.create_all()
        data = {item.key: item.value for item in GameData.query.all()}
        defaults = {
            'users': {}, 
            'withdrawals': {}, 
            'deposits': {}, 
            'broadcasts': [], 
            'history': []
        }
        for k, v in defaults.items():
            if k not in data: data[k] = v
        return data

# ዳታውን መጫን
data_store = load_data_from_db()
users = data_store['users']
pending_withdrawals = data_store['withdrawals']
pending_deposits = data_store['deposits']
broadcast_history = data_store.get('broadcasts', [])
game_history = data_store.get('history', [])

def sync_db():
    try:
        with app.app_context():
            data = {
                'users': users, 
                'withdrawals': pending_withdrawals, 
                'deposits': pending_deposits, 
                'broadcasts': broadcast_history, 
                'history': game_history
            }
            for k, v in data.items():
                item = GameData.query.filter_by(key=k).first()
                if item: item.value = v
                else: db.session.add(GameData(key=k, value=v))
            db.session.commit()
    except Exception as e:
        logger.error(f"Sync DB Error: {e}")

# --- Notification Service ---
def notify_user(chat_id, text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        if hasattr(reply_markup, 'to_dict'):
            payload["reply_markup"] = reply_markup.to_dict()
        else:
            payload["reply_markup"] = reply_markup

    def send():
        try:
            requests.post(url, json=payload, timeout=10)
        except: pass
    threading.Thread(target=send).start()

# --- Bot Command Handlers ---
async def start_command(update: Update, context):
    user = update.effective_user
    tid = str(user.id)
    args = context.args
    
    keyboard = [
        [KeyboardButton("🎮 ጨዋታውን ክፈት")],
        [KeyboardButton("💰 ተቀማጭ / Deposit"), KeyboardButton("💸 ወጪ / Withdraw")],
        [KeyboardButton("👥 ሪፈር / Refer"), KeyboardButton("👤 ፕሮፋይል / Profile")],
        [KeyboardButton("📞 ድጋፍ / Support")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    if tid not in users:
        referrer_id = args[0] if args else None
        users[tid] = {
            'tg_id': tid,
            'username': user.username or user.first_name,
            'balance': 5.0,
            'total_deposited': 0,
            'is_banned': False,
            'joined_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'referred_by': referrer_id
        }
        if referrer_id and referrer_id in users and referrer_id != tid:
            users[referrer_id]['balance'] += 2.0
            notify_user(referrer_id, f"<b>👥 አዲስ ሪፈር!</b>\nበጓደኛዎ ግብዣ 2.00 ETB ወደ ሂሳብዎ ተጨምሯል።")
        
        sync_db()
        await update.message.reply_html(
            f"<b>እንኳን ደህና መጡ {user.first_name}!</b>\n\nየእርስዎ ID: <code>{tid}</code>\nየመጀመሪያ ቦነስ: 5 ETB ተሰጥቶዎታል።",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_html(
            f"<b>እንኳን በደህና ተመለሱ!</b>\n\nID: <code>{tid}</code>\nሂሳብ: {users[tid]['balance']:.2f} ETB",
            reply_markup=reply_markup
        )

async def handle_message(update: Update, context):
    text = update.message.text
    tid = str(update.effective_user.id)
    if tid not in users: return

    if text == "🎮 ጨዋታውን ክፈት":
        link = f"https://{DOMAIN}" if DOMAIN else "ሊንኩ አልተዘጋጀም"
        await update.message.reply_html(f"<b>ለመጫወት ሊንኩን ይጫኑ:</b>\n{link}")
    elif text == "💰 ተቀማጭ / Deposit":
        await update.message.reply_html("<b>💰 ብር ለማስገባት</b>\nበቴሌብር በዚህ ቁጥር ይላኩ: <code>0975118009</code>\nከዛ የደረሰኝ ቁጥሩን ዌብሳይቱ ላይ ያስገቡ።")
    elif text == "👥 ሪፈር / Refer":
        bot_info = await context.bot.get_me()
        refer_link = f"https://t.me/{bot_info.username}?start={tid}"
        await update.message.reply_html(f"<b>የእርስዎ መጋበዣ ሊንክ:</b>\n{refer_link}\n\nበእያንዳንዱ ሰው 2.00 ETB ያገኛሉ።")
    elif text == "👤 ፕሮፋይል / Profile":
        u = users[tid]
        await update.message.reply_html(f"<b>👤 ፕሮፋይል</b>\nID: <code>{tid}</code>\nሂሳብ: {u['balance']:.2f} ETB")
    elif text == "📞 ድጋፍ / Support":
        await update.message.reply_html("<b>📞 ድጋፍ</b>\nለማንኛውም ጥያቄ: @revo_admin")

async def button_handler(update: Update, context):
    query = update.callback_query
    await query.answer()

# --- Webhook Route ---
@app.route('/webhook', methods=['POST'])
def webhook():
    if not app_bot: return "Bot not ready", 500
    try:
        update = Update.de_json(request.get_json(force=True), app_bot.bot)
        asyncio.run_coroutine_threadsafe(app_bot.process_update(update), bot_loop)
        return "ok", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "error", 500

# --- Bot Background Thread ---
def run_bot_thread():
    global bot_loop, app_bot
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    
    app_bot = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start_command))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app_bot.add_handler(CallbackQueryHandler(button_handler))
    
    bot_loop.run_until_complete(app_bot.initialize())
    
    if DOMAIN:
        webhook_url = f"https://{DOMAIN.replace('https://', '')}/webhook"
        logger.info(f"Setting Webhook: {webhook_url}")
        bot_loop.run_until_complete(app_bot.bot.set_webhook(url=webhook_url, drop_pending_updates=True))
    
    bot_loop.run_forever()

# --- Game Engine ---
game_state = {'phase': 'countdown', 'multiplier': 1.0, 'countdown': 7}
def start_game_loop():
    while True:
        try:
            game_state['phase'] = 'countdown'
            for i in range(7, 0, -1):
                game_state['countdown'] = i
                socketio.emit('game_state', game_state)
                time.sleep(1)
            
            game_state['phase'] = 'active'
            game_state['multiplier'] = 1.0
            crash_point = round(0.96 / (1 - random.random()), 2) if random.random() > 0.04 else 1.0
            
            curr = 1.0
            while curr < crash_point:
                curr = round(curr * 1.05, 2) if curr < 2 else round(curr + 0.1, 2)
                if curr >= crash_point: break
                game_state['multiplier'] = curr
                socketio.emit('game_state', game_state)
                time.sleep(0.15)
            
            game_state['phase'] = 'crashed'
            game_state['multiplier'] = crash_point
            socketio.emit('game_state', game_state)
            game_history.append(crash_point)
            if len(game_history) > 15: game_history.pop(0)
            sync_db()
            time.sleep(4)
        except Exception as e:
            logger.error(f"Game Loop Error: {e}")
            time.sleep(2)

# --- API Routes ---
@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/api/check-auth', methods=['POST'])
def check_auth():
    tid = str(request.json.get('telegram_id'))
    if tid in users:
        return jsonify({'success': True, 'balance': users[tid]['balance']})
    return jsonify({'success': False, 'message': 'User not found'})

@app.route('/api/deposit', methods=['POST'])
def submit_deposit():
    data = request.json
    tid = data.get('telegram_id')
    amount = data.get('amount')
    rid = f"d_{int(time.time())}"
    pending_deposits[rid] = {'tg_id': tid, 'amount': amount, 'ts': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    sync_db()
    notify_user(ADMIN_CHAT_ID, f"<b>💰 አዲስ ተቀማጭ</b>\nID: {tid}\nመጠን: {amount} ETB")
    return jsonify({'success': True})

# --- Main Entry ---
if __name__ == '__main__':
    # ዳታቤዝ ማዘጋጀት
    with app.app_context():
        db.create_all()
    
    # የጀርባ ስራዎችን ማስጀመር
    threading.Thread(target=run_bot_thread, daemon=True).start()
    threading.Thread(target=start_game_loop, daemon=True).start()
    
    logger.info("Server is running...")
    # Render ላይ port 5000 ወይም 10000 ይጠቀማል
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
