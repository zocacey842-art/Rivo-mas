import eventlet
eventlet.monkey_patch()

import os
import json
import hashlib
import random
import time
import asyncio
import threading
import logging
import requests
from datetime import datetime
from flask import Flask, send_from_directory, request, jsonify
from flask_socketio import SocketIO, emit
from telegram import KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET', 'aviator_pro_secure_key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL').replace('postgres://', 'postgresql://') if os.environ.get('DATABASE_URL') else None
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DOMAIN = os.environ.get('REPLIT_DEV_DOMAIN')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID')

# Global references
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
        data = {}
        for item in GameData.query.all():
            data[item.key] = item.value
        
        defaults = {
            'users': {}, 
            'withdrawals': {}, 
            'deposits': {}, 
            'notif_queue': [], 
            'broadcasts': [], 
            'history': []
        }
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        return data

def sync_db_to_postgres():
    with app.app_context():
        data = {
            'users': users, 
            'withdrawals': pending_withdrawals, 
            'deposits': pending_deposits,
            'notif_queue': notification_queue,
            'broadcasts': broadcast_history,
            'history': game_history
        }
        for k, v in data.items():
            item = GameData.query.filter_by(key=k).first()
            if item:
                item.value = v
            else:
                db.session.add(GameData(key=k, value=v))
        db.session.commit()

data_store = load_data_from_db()
users = data_store['users']
pending_withdrawals = data_store['withdrawals']
pending_deposits = data_store['deposits']
notification_queue = data_store['notif_queue']
broadcast_history = data_store.get('broadcasts', [])
game_history = data_store.get('history', [])

def sync_db():
    sync_db_to_postgres()

# --- Notification Queue Service ---
def notify_user(chat_id, text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id: return
    try:
        global app_bot, bot_loop
        logger.info(f"Notify User: Attempting to send message to {chat_id}")
        if app_bot and bot_loop:
            asyncio.run_coroutine_threadsafe(
                app_bot.bot.send_message(chat_id=int(chat_id), text=text, parse_mode='HTML', reply_markup=reply_markup),
                bot_loop
            )
            logger.info(f"Notify User: Message task scheduled for {chat_id}")
        else:
            logger.warning(f"Notify User: Bot not ready, queuing message for {chat_id}")
            notif = {
                'chat_id': str(chat_id),
                'text': text,
                'reply_markup': reply_markup.to_dict() if hasattr(reply_markup, 'to_dict') else reply_markup,
                'ts': time.time()
            }
            notification_queue.append(notif)
            sync_db()
    except Exception as e:
        logger.error(f"Error in notify_user: {e}", exc_info=True)

def notify_admin(text, reply_markup=None):
    if ADMIN_CHAT_ID:
        notify_user(ADMIN_CHAT_ID, text, reply_markup)

def create_broadcast(text):
    broadcast_history.append({'text': text, 'ts': datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    sync_db()
    for tid in users:
        notify_user(tid, text)

def notification_worker():
    logger.info("Notification worker thread started")
    while True:
        if notification_queue:
            notif = notification_queue[0]
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                payload = {"chat_id": int(notif['chat_id']), "text": notif['text'], "parse_mode": "HTML"}
                if notif.get('reply_markup'):
                    payload["reply_markup"] = json.dumps(notif['reply_markup'])
                
                resp = requests.post(url, json=payload, timeout=20)
                data = resp.json()
                if resp.ok and data.get("ok"):
                    notification_queue.pop(0)
                    sync_db()
                    logger.info(f"Worker: Sent notif to {notif['chat_id']}")
                else:
                    err_msg = data.get('description', 'Unknown error')
                    logger.error(f"Worker Error: {err_msg}")
                    notification_queue.pop(0)
                    sync_db()
            except Exception as e:
                logger.error(f"Worker Exception: {e}")
                time.sleep(5)
        time.sleep(1)

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
        # Check for referrer
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
            f"<b>እንኳን ደህና መጡ {user.first_name}!</b>\n\nየእርስዎ ID: <code>{tid}</code>\nየመጀመሪያ ቦነስ: 5 ETB ተሰጥቶዎታል።\n\nለመጫወት ይህንን ID በዌብሳይቱ ላይ ይጠቀሙ።",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_html(
            f"<b>እንኳን በደህና ተመለሱ!</b>\n\nየእርስዎ ID: <code>{tid}</code>\nቀሪ ሂሳብ: {users[tid]['balance']:.2f} ETB",
            reply_markup=reply_markup
        )

async def contact_handler(update: Update, context):
    pass

async def handle_message(update: Update, context):
    text = update.message.text
    tid = str(update.effective_user.id)
    
    if text == "🎮 ጨዋታውን ክፈት":
        await update.message.reply_html(f"<b>ጨዋታውን ለመጀመር ይህንን ሊንክ ይጫኑ:</b>\n{DOMAIN}")
    elif text == "💰 ተቀማጭ / Deposit":
        await update.message.reply_html(
            "<b>💰 ብር ለማስገባት (Deposit)</b>\n\n"
            "1. በቴሌብር በዚህ ቁጥር ይላኩ: <code>0975118009</code>\n"
            "2. የላኩበትን ትክክለኛ ቴክስት ኮፒ አድርገው በዌብሳይቱ 'Deposit' ክፍል ላይ ይላኩ።\n"
            "3. አድሚን ሲያረጋግጥ በ 5-10 ደቂቃ ውስጥ ብሩ ይገባላችኋል።",
            parse_mode='HTML'
        )
    elif text == "💸 ወጪ / Withdraw":
        await update.message.reply_html(f"<b>💸 ብር ለማውጣት</b>\n\nቀሪ ሂሳብዎ: {users.get(tid, {}).get('balance', 0):.2f} ETB\n\nብሩን ለማውጣት በዌብሳይቱ ላይ ያለውን 'Withdraw' ቁልፍ ይጠቀሙ።")
    elif text == "👤 ፕሮፋይል / Profile":
        user_data = users.get(tid, {})
        await update.message.reply_html(
            f"<b>👤 የእርስዎ መረጃ</b>\n\n"
            f"መታወቂያ (ID): <code>{tid}</code>\n"
            f"ስም: {user_data.get('username', 'N/A')}\n"
            f"ቀሪ ሂሳብ: {user_data.get('balance', 0):.2f} ETB\n"
            f"የተቀመጠ ብር: {user_data.get('total_deposited', 0):.2f} ETB"
        )
    elif text == "👥 ሪፈር / Refer":
        bot_info = await context.bot.get_me()
        refer_link = f"https://t.me/{bot_info.username}?start={tid}"
        await update.message.reply_html(
            f"<b>👥 የሪፈራል ፕሮግራም</b>\n\n"
            f"ጓደኞችዎን ይጋብዙ እና በእያንዳንዱ ሰው 2.00 ETB ጉርሻ ያግኙ!\n\n"
            f"<b>የእርስዎ የመጋበዣ ሊንክ:</b>\n{refer_link}"
        )
    elif text == "📞 ድጋፍ / Support":
        await update.message.reply_html("<b>📞 የድጋፍ መስጫ</b>\n\nማንኛውም ጥያቄ ወይም እርዳታ ካስፈለገዎት አድሚኑን ያነጋግሩ:\n@revo_admin")

async def button_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "show_dep":
        await query.edit_message_text(
            "<b>💰 ብር ለማስገባት (Deposit)</b>\n\n"
            "1. በቴሌብር በዚህ ቁጥር ይላኩ: <code>0975118009</code>\n"
            "2. የላኩበትን ትክክለኛ ቴክስት ኮፒ አድርገው በዌብሳይቱ 'Deposit' ክፍል ላይ ይላኩ።\n"
            "3. አድሚን ሲያረጋግጥ በ 5-10 ደቂቃ ውስጥ ብሩ ይገባላችኋል።",
            parse_mode='HTML'
        )
    elif data.startswith("app_dep_"):
        rid = data.replace("app_dep_", "")
        # Mock admin action from server side logic
        pass

# --- Game Engine ---
game_state = {
    'phase': 'countdown',
    'multiplier': 1.0,
    'countdown': 7,
    'start_time': 0
}

def generate_crash_point():
    r = random.random()
    if r < 0.04: return 1.00 # 4% house edge instant crash
    return round(0.96 / (1 - random.random()), 2)

def start_game_loop():
    logger.info("Game loop thread started")
    while True:
        try:
            game_state['phase'] = 'countdown'
            for i in range(7, 0, -1):
                game_state['countdown'] = i
                socketio.emit('game_state', game_state)
                time.sleep(1)
            
            game_state['phase'] = 'active'
            game_state['multiplier'] = 1.0
            game_state['start_time'] = time.time()
            crash_point = generate_crash_point()
            logger.info(f"New round started. Crash point: {crash_point}")
            
            while True:
                elapsed = time.time() - game_state['start_time']
                current_mult = round(pow(1.06, elapsed * 10), 2)
                if current_mult >= crash_point:
                    game_state['multiplier'] = crash_point
                    break
                game_state['multiplier'] = current_mult
                socketio.emit('game_state', game_state)
                time.sleep(0.05)
            
            game_state['phase'] = 'crashed'
            socketio.emit('game_state', game_state)
            game_history.append(crash_point)
            if len(game_history) > 20: game_history.pop(0)
            sync_db()
            time.sleep(3)
        except Exception as e:
            logger.error(f"Error in game loop: {e}")
            time.sleep(1)

# --- API Routes ---
@app.route('/webhook', methods=['POST'])
def webhook():
    if not app_bot: return "Bot not ready", 500
    update = Update.de_json(request.get_json(force=True), app_bot.bot)
    asyncio.run_coroutine_threadsafe(app_bot.process_update(update), bot_loop)
    return "ok"

@app.route('/api/test-bot', methods=['POST'])
def test_bot():
    try:
        tid = request.json.get('telegram_id')
        if not tid: return jsonify({'success': False, 'message': 'Missing Telegram ID'}), 400
        logger.info(f"Test Bot: Sending test message to {tid}")
        notify_user(tid, "<b>🛠 Bot Test:</b> test123")
        return jsonify({'success': True, 'message': 'Test message sent'})
    except Exception as e:
        logger.error(f"Error in test_bot API: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500

def run_bot_thread():
    global bot_loop, app_bot
    try:
        bot_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(bot_loop)
        logger.info("Initializing bot application...")
        app_bot = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        app_bot.add_handler(CommandHandler("start", start_command))
        app_bot.add_handler(MessageHandler(filters.CONTACT, contact_handler))
        app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app_bot.add_handler(CallbackQueryHandler(button_handler))
        bot_loop.run_until_complete(app_bot.initialize())
        
        webhook_url = os.environ.get('REPLIT_DEV_DOMAIN') or os.environ.get('RENDER_EXTERNAL_URL')
        if webhook_url:
            if not webhook_url.startswith('http'): webhook_url = f"https://{webhook_url}"
            full_webhook_url = f"{webhook_url.rstrip('/')}/webhook"
            logger.info(f"Setting webhook to: {full_webhook_url}")
            bot_loop.run_until_complete(app_bot.bot.set_webhook(url=full_webhook_url))
        
        logger.info("Bot loop starting...")
        # Check if we are in production (webhook mode) or development (polling)
        if os.environ.get('RENDER_EXTERNAL_URL'):
            logger.info("Production mode: Webhook active")
            # In production, we strictly use webhook and ensure polling is NOT running
            try:
                bot_loop.run_until_complete(app_bot.bot.delete_webhook(drop_pending_updates=True))
                time.sleep(1)
            except: pass
            
            if webhook_url:
                if not webhook_url.startswith('http'): webhook_url = f"https://{webhook_url}"
                full_webhook_url = f"{webhook_url.rstrip('/')}/webhook"
                logger.info(f"Setting webhook to: {full_webhook_url}")
                bot_loop.run_until_complete(app_bot.bot.set_webhook(url=full_webhook_url, drop_pending_updates=True))
            bot_loop.run_forever()
        else:
            logger.info("Development mode: Polling")
            # Clear webhook and wait longer to ensure Telegram updates its state
            try:
                bot_loop.run_until_complete(app_bot.bot.delete_webhook(drop_pending_updates=True))
                logger.info("Webhook cleared, waiting for Telegram state update...")
                time.sleep(5) 
            except Exception as e:
                logger.error(f"Error clearing webhook: {e}")
            
            # Start polling with explicit clean start
            app_bot.run_polling(close_loop=False, drop_pending_updates=True, stop_signals=None)
    except Exception as e:
        logger.error(f"Fatal bot error: {e}", exc_info=True)

@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/api/check-auth', methods=['POST'])
def check_auth():
    tid = str(request.json.get('telegram_id'))
    if tid in users:
        if users[tid].get('is_banned'): return jsonify({'success': False, 'message': 'Account banned'})
        return jsonify({'success': True, 'balance': users[tid]['balance']})
    return jsonify({'success': False})

@app.route('/api/admin/data', methods=['GET'])
def admin_data():
    return jsonify({'users': users, 'deposits': pending_deposits, 'withdrawals': pending_withdrawals, 'broadcasts': broadcast_history})

@app.route('/api/deposit', methods=['POST'])
def submit_deposit():
    data = request.json
    rid = f"d_{int(time.time()*1000)}"
    pending_deposits[rid] = {'tg_id': data.get('telegram_id'), 'amount': data.get('amount'), 'telebirr_text': data.get('telebirr_text'), 'ts': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    sync_db()
    notify_admin(f"<b>💰 አዲስ ተቀማጭ</b>\n\n👤 ተጠቃሚ: {data.get('telegram_id')}\n💵 መጠን: {data.get('amount')} ETB")
    return jsonify({'success': True})

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    threading.Thread(target=run_bot_thread, daemon=True).start()
    threading.Thread(target=notification_worker, daemon=True).start()
    threading.Thread(target=start_game_loop, daemon=True).start()
    logger.info("Server starting on port 5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
