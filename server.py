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
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
DOMAIN = os.environ.get('REPLIT_DEV_DOMAIN')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID')

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
        
        # Ensure default keys exist
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
        logger.info(f"Notify User: Attempting to send message to {chat_id}")
        if app_bot and bot_loop:
            asyncio.run_coroutine_threadsafe(
                app_bot.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=reply_markup),
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
        else:
            time.sleep(0.5)

def create_broadcast(content):
    user_ids = list(users.keys())
    for tid in user_ids:
        notify_user(tid, content)
    broadcast_history.append({
        'content': content,
        'status': 'queued',
        'sent_at': datetime.now().isoformat(),
        'target_count': len(user_ids)
    })
    sync_db()
    return len(user_ids)

def notify_admin(text, reply_markup=None):
    if ADMIN_CHAT_ID: notify_user(ADMIN_CHAT_ID, f"<b>🔔 አድሚን ማሳሰቢያ:</b>\n{text}", reply_markup=reply_markup)

# --- Core Game Logic ---
game_state = {'phase': 'waiting', 'countdown': 7, 'multiplier': 1.00, 'crash_point': 0, 'history': game_history}

@socketio.on('place_bet')
def handle_bet(data):
    tid = str(data.get('telegram_id'))
    amount = float(data.get('amount'))
    if tid in users and not users[tid].get('is_banned'):
        if amount < 3:
            emit('bet_error', {'message': 'ዝቅተኛው ውርርድ 3 ETB ነው።'})
            return
        if amount > 500:
            emit('bet_error', {'message': 'ከፍተኛው ውርርድ 500 ETB ነው።'})
            return
        if users[tid]['balance'] >= amount:
            # Logic for placing bet... (needs to be integrated with game_loop)
            pass

def generate_crash_point():
    # House edge: ~20% overall
    # Instant crash at 1.00x: 10% chance
    # Very Low multipliers (1.01 - 1.20): 45% chance
    # Low multipliers (1.21 - 2.00): 30% chance
    # Medium multipliers (2.01 - 5.00): 10% chance
    # High multipliers (5.01 - 20.00): 4% chance
    # Ultra High multipliers (20.01 - 100.00): 1% chance
    
    chance = random.random()
    
    if chance < 0.10: # 10% Instant crash at 1.00x
        return 1.00
    elif chance < 0.55: # 45% chance for very low
        return round(random.uniform(1.01, 1.20), 2)
    elif chance < 0.85: # 30% chance for low
        return round(random.uniform(1.21, 2.00), 2)
    elif chance < 0.95: # 10% chance for medium
        return round(random.uniform(2.01, 5.00), 2)
    elif chance < 0.99: # 4% chance for high (up to 20x)
        return round(random.uniform(5.01, 20.00), 2)
    else: # 1% chance for ultra high (up to 100x)
        return round(random.uniform(20.01, 100.00), 2)

def game_loop():
    while True:
        game_state['phase'] = 'countdown'
        game_state['crash_point'] = generate_crash_point()
        for i in range(7, 0, -1):
            socketio.emit('game_state', {'phase': 'countdown', 'countdown': i, 'multiplier': 1.00})
            eventlet.sleep(1)
        game_state['phase'] = 'running'
        start_time = time.time()
        while True:
            elapsed = time.time() - start_time
            multiplier = round(pow(2.71828, 0.05 * elapsed), 2)
            socketio.emit('game_state', {'phase': 'running', 'multiplier': multiplier})
            if multiplier >= game_state['crash_point']: break
            eventlet.sleep(0.05)
        game_state['phase'] = 'crashed'
        game_state['history'].insert(0, game_state['crash_point'])
        game_state['history'] = game_state['history'][:20]
        global game_history
        game_history = game_state['history']
        sync_db()
        socketio.emit('game_state', {'phase': 'crashed', 'multiplier': game_state['crash_point'], 'history': game_state['history']})
        eventlet.sleep(3)

# --- Telegram Bot Handlers ---
async def start_command(update, context):
    try:
        chat_id = str(update.message.chat_id)
        ref = context.args[0] if context.args else None
        
        if chat_id not in users:
            users[chat_id] = {
                'phone': None, 
                'balance': 5.0, 
                'referred_by': ref, 
                'reg_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'total_deposited': 0
            }
            if ref and ref in users and ref != chat_id:
                users[ref]['balance'] = float(users[ref].get('balance', 0)) + 2.0
                notify_user(ref, "<b>🎁 የሪፈራል ቦነስ!</b>\n\nአዲስ ሰው ስለጋበዙ <b>2.00 ETB</b> ተጨምሯል። 🎊")
            sync_db()
        else:
            # Ensure consistency and prevent crashes with old data
            user = users[chat_id]
            user['balance'] = float(user.get('balance', 0))
            user['total_deposited'] = float(user.get('total_deposited', 0))
            if 'reg_date' not in user:
                user['reg_date'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        user = users[chat_id]
        if not user.get('phone'):
            btn = KeyboardButton(text="📱 ስልክ ቁጥርዎን ያጋሩ", request_contact=True)
            await update.message.reply_text(
                "👋 **እንኳን በደህና መጡ!**\n\nለመመዝገብ እና ጨዋታውን ለመጀመር እባክዎ ስልክዎን ያጋሩ። 👇", 
                reply_markup=ReplyKeyboardMarkup([[btn]], resize_keyboard=True, one_time_keyboard=True),
                parse_mode='Markdown'
            )
        else:
            await show_main_menu(update, chat_id)
    except Exception as e:
        logger.error(f"Error in start_command: {e}")
        try:
            await update.message.reply_text("⚠️ ይቅርታ፣ ሲስተሙ ላይ ትንሽ መቆራረጥ አጋጥሟል። እባክዎ ትንሽ ቆይተው ይሞክሩ።")
        except: pass

async def contact_handler(update, context):
    chat_id = str(update.message.chat_id)
    if chat_id in users:
        users[chat_id]['phone'] = update.message.contact.phone_number
        sync_db()
        await update.message.reply_text("✅ **ምዝገባው ተሳክቷል!**\n\nአሁን ጨዋታውን መጀመር ይችላሉ። 🎮", parse_mode='Markdown')
        await show_main_menu(update, chat_id)

async def show_main_menu(update, chat_id):
    kb = [
        [KeyboardButton("👤 ፕሮፋይል"), KeyboardButton("💰 ዋሌት")], 
        [KeyboardButton("💳 ተቀማጭ"), KeyboardButton("💸 ወጪ")], 
        [KeyboardButton("🔗 ሪፈራል"), KeyboardButton("🎮 ወደ ጨዋታው")]
    ]
    if str(chat_id) == str(ADMIN_CHAT_ID): 
        kb.append([KeyboardButton("📢 ብሮድካስት")])
    await update.message.reply_text("🚀 **ዋና ዝርዝር (Main Menu):**", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode='Markdown')

async def handle_message(update, context):
    if not update.message or not update.message.text: return
    text, chat_id = update.message.text, str(update.message.chat_id)
    if ADMIN_CHAT_ID and str(chat_id) == str(ADMIN_CHAT_ID):
        if text.startswith("/broadcast "):
            msg = text.replace("/broadcast ", "", 1).strip()
            if msg:
                count = create_broadcast(f"<b>📢 መልዕክት ከአድሚን:</b>\n\n{msg}")
                await update.message.reply_text(f"🚀 ብሮድካስት ለ {count} ተጠቃሚዎች በኩዌ (Queue) በኩል እየተላከ ነው... 📤")
            else:
                await update.message.reply_text("⚠️ እባክዎ መልዕክት ይጻፉ።")
            return
        elif text == "📢 ብሮድካስት":
            await update.message.reply_text("📣 እንዲህ ይጻፉ: `/broadcast መልዕክትዎ`", parse_mode='Markdown')
            return

    if chat_id not in users: return await start_command(update, context)
    user = users[chat_id]
    if text == "👤 ፕሮፋይል": 
        await update.message.reply_text(f"<b>👤 የእርስዎ ፕሮፋይል</b>\n\n🆔 ID: <code>{chat_id}</code>\n📱 ስልክ: {user['phone']}\n💰 ቀሪ ሂሳብ: <b>{user['balance']:.2f} ETB</b>\n📅 የተመዘገቡበት: {user.get('reg_date', 'N/A')}")
    elif text == "💰 ዋሌት": 
        await update.message.reply_text(f"<b>💰 ዋሌት (Wallet)</b>\n\n💵 የአሁኑ ቀሪ ሂሳብዎ: <b>{user['balance']:.2f} ETB</b>\n📈 አጠቃላይ ያስገቡት: {user.get('total_deposited', 0):.2f} ETB")
    elif text == "💳 ተቀማጭ":
        msg = (
            "<b>💳 ገንዘብ ለማስገባት (Deposit)</b>\n\n"
            "1️⃣ ወደዚ የቴሌብር ቁጥር ብር ይላኩ: <code>0975118009</code>\n"
            "2️⃣ ብር ከላኩ በኋላ ከቴሌብር የደረስዎትን ሙሉ መልዕክት ኮፒ ያድርጉ\n"
            "3️⃣ ወደ ጨዋታው ዌብሳይት በመሄድ <b>ተቀማጭ (Deposit)</b> የሚለውን ይጫኑ\n"
            "4️⃣ የላኩትን የብር መጠን እና የቴሌብር መልዕክቱን ያስገቡ\n\n"
            "⚠️ <i>አድሚኑ መረጃውን እንዳረጋገጠ ወዲያውኑ ብሩ በዋሌትዎ ላይ ይታያል።</i> ⏳"
        )
        await update.message.reply_text(msg, parse_mode='HTML')
    elif text == "💸 ወጪ":
        msg = (
            "<b>💸 ገንዘብ ለማውጣት (Withdraw)</b>\n\n"
            "1️⃣ ወደ ጨዋታው ዌብሳይት ይግቡ\n"
            "2️⃣ <b>ወጪ (Withdraw)</b> የሚለውን ምርጫ ይጫኑ\n"
            "3️⃣ ማውጣት የሚፈልጉትን የብር መጠን እና የቴሌብር ስልክ ቁጥርዎን ያስገቡ\n"
            "4️⃣ ጥያቄዎን ይላኩ 📤\n\n"
            "ℹ️ <i>ማሳሰቢያ: ዝቅተኛው የማውጫ መጠን 100 ETB ነው።</i>"
        )
        await update.message.reply_text(msg, parse_mode='HTML')
    elif text == "🔗 ሪፈራል":
        ref_link = f"https://t.me/revoavio_bot?start={chat_id}"
        msg = (
            "<b>🔗 የሪፈራል ሊንክ (Referral)</b>\n\n"
            "🎁 ይህንን ሊንክ ለጓደኞችዎ በመላክ ይጋብዙ። አዲስ ሰው ሲጋብዙ የ <b>2.00 ETB</b> ቦነስ ያገኛሉ!\n\n"
            f"📍 የእርስዎ ሊንክ: <code>{ref_link}</code>"
        )
        await update.message.reply_text(msg, parse_mode='HTML')
    elif text == "🎮 ወደ ጨዋታው":
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 ጨዋታውን ክፈት", url=f"https://{DOMAIN}/?tid={chat_id}")]])
        await update.message.reply_text("🕹️ **መልካም ጨዋታ!**\n\nወደ ጨዋታው ለመግባት ከታች ያለውን ሊንክ ይጫኑ፡", reply_markup=markup, parse_mode='Markdown')
    else: await show_main_menu(update, chat_id)

async def contact_handler(update, context):
    chat_id = str(update.message.chat_id)
    users[chat_id]['phone'] = update.message.contact.phone_number
    sync_db()
    await update.message.reply_text("✅ ምዝገባ ተሳክቷል!")
    await show_main_menu(update, chat_id)

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("app_dep_"):
        rid = data.replace("app_dep_", "")
        if rid in pending_deposits:
            dep = pending_deposits.pop(rid)
            tid, amt = str(dep['tg_id']), float(dep['amount'])
            if tid in users:
                users[tid]['balance'] += amt
                users[tid]['total_deposited'] = users[tid].get('total_deposited', 0) + amt
                sync_db()
                notify_user(tid, f"<b>✅ ተቀማጭ ተረጋግጧል!</b>\nመጠን: {amt} ETB\nአዲስ ቀሪ ሂሳብ: {users[tid]['balance']:.2f} ETB")
                await query.edit_message_text(f"✅ ተቀማጭ ተፈቅዷል!\nተጠቃሚ: {tid}\nመጠን: {amt} ETB")
            else: await query.edit_message_text("❌ ተጠቃሚው አልተገኘም")
        else: await query.edit_message_text("❌ ጥያቄው አልተገኘም ወይም ቀደም ብሎ ተሰርቷል")
    
    elif data.startswith("rej_dep_"):
        rid = data.replace("rej_dep_", "")
        if rid in pending_deposits:
            dep = pending_deposits.pop(rid)
            sync_db()
            notify_user(dep['tg_id'], "<b>❌ ተቀማጭ ውድቅ ተደርጓል!</b>\nእባክዎ መረጃዎን በትክክል ያስገቡ።")
            await query.edit_message_text(f"❌ ተቀማጭ ውድቅ ተደርጓል!\nተጠቃሚ: {dep['tg_id']}")
        else: await query.edit_message_text("❌ ጥያቄው አልተገኘም")

    elif data.startswith("app_with_"):
        rid = data.replace("app_with_", "")
        if rid in pending_withdrawals:
            w = pending_withdrawals.pop(rid)
            sync_db()
            notify_user(w['tg_id'], f"<b>✅ እንኳን ደስ አልዎት የወጪ ጥያቄዎ ተቀባይነት አግኝቷል!</b>\nመጠን: {w['amount']} ETB\nብሩ በቴሌብር ከ30m - 1hr ውስጥ ይላክልዎታል")
            await query.edit_message_text(f"✅ የወጪ ጥያቄ ጸድቋል!\nተጠቃሚ: {w['tg_id']}\nመጠን: {w['amount']} ETB")
        else: await query.edit_message_text("❌ ጥያቄው አልተገኘም")

    elif data.startswith("rej_with_"):
        rid = data.replace("rej_with_", "")
        if rid in pending_withdrawals:
            w = pending_withdrawals.pop(rid)
            users[w['tg_id']]['balance'] += float(w['amount'])
            sync_db()
            notify_user(w['tg_id'], "<b>❌ የወጪ ጥያቄዎ ውድቅ ተደርጓል!</b>\nብሩ ወደ ዋሌትዎ ተመልሷል።")
            await query.edit_message_text(f"❌ የወጪ ጥያቄ ውድቅ ተደርጓል!\nተጠቃሚ: {w['tg_id']}")
        else: await query.edit_message_text("❌ ጥያቄው አልተገኘም")

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        if not TELEGRAM_BOT_TOKEN:
            logger.error("Webhook received but TELEGRAM_BOT_TOKEN is not set")
            return 'Bot token not set', 400
        
        json_data = request.get_json(force=True)
        logger.info(f"Webhook received data: {json_data}")
        
        # Process updates asynchronously for webhook
        update = Update.de_json(json_data, app_bot.bot)
        
        asyncio.run_coroutine_threadsafe(app_bot.process_update(update), bot_loop)
        return 'ok'
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return 'error', 500

@app.route('/api/test-bot', methods=['POST'])
def test_bot():
    try:
        data = request.get_json()
        tid = str(data.get('telegram_id'))
        if not tid:
            logger.error("Test Bot: Telegram ID missing in request")
            return jsonify({'success': False, 'message': 'Telegram ID missing'}), 400
        
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
        
        # Initialize application
        bot_loop.run_until_complete(app_bot.initialize())
        
        # Set webhook
        webhook_url = os.environ.get('REPLIT_DEV_DOMAIN') or os.environ.get('RENDER_EXTERNAL_URL')
        
        if not webhook_url:
            logger.error("CRITICAL: No domain found for webhook! Set REPLIT_DEV_DOMAIN or RENDER_EXTERNAL_URL")
            return

        if not webhook_url.startswith('http'):
            webhook_url = f"https://{webhook_url}"
        
        webhook_url = f"{webhook_url.rstrip('/')}/webhook"
        
        logger.info(f"Setting webhook to: {webhook_url}")
        bot_loop.run_until_complete(app_bot.bot.set_webhook(url=webhook_url))
        
        logger.info("Bot loop starting...")
        bot_loop.run_forever()
    except Exception as e:
        logger.error(f"Fatal error in bot thread: {e}", exc_info=True)

if __name__ == '__main__':
    # Initialize DB and data
    with app.app_context():
        db.create_all()
    
    # Global references for webhook
    bot_loop = None
    app_bot = None
    
    # Start bot thread
    threading.Thread(target=run_bot_thread, daemon=True).start()
    
    # Start notification worker
    threading.Thread(target=notification_worker, daemon=True).start()
    
    # Run Flask-SocketIO
    logger.info("Starting Flask-SocketIO server on port 5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)

@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/api/admin/toggle-ban', methods=['POST'])
def toggle_ban():
    tid = str(request.json.get('telegram_id'))
    status = request.json.get('status')
    if tid in users:
        users[tid]['is_banned'] = status
        sync_db()
        return jsonify({'success': True})
    return jsonify({'success': False})

@app.route('/api/check-auth', methods=['POST'])
def check_auth():
    tid = str(request.json.get('telegram_id'))
    if tid in users:
        if users[tid].get('is_banned'):
            return jsonify({'success': False, 'message': 'አካውንትዎ በታገደ (Banned) ሁኔታ ላይ ነው።'})
        return jsonify({'success': True, 'balance': users[tid]['balance']})
    return jsonify({'success': False})

@app.route('/api/admin/data', methods=['GET'])
def admin_data():
    return jsonify({'users': users, 'deposits': pending_deposits, 'withdrawals': pending_withdrawals, 'broadcasts': broadcast_history})

@app.route('/api/admin/broadcast', methods=['POST'])
def admin_broadcast():
    msg = request.json.get('message')
    if not msg: return jsonify({'success': False, 'message': 'Empty message'})
    create_broadcast(f"<b>📢 መልዕክት ከአድሚን:</b>\n\n{msg}")
    return jsonify({'success': True})

@app.route('/api/admin/adjust-balance', methods=['POST'])
def adjust_balance():
    tid, amt, action = str(request.json.get('telegram_id')), float(request.json.get('amount')), request.json.get('action')
    if tid in users:
        if action == 'add': users[tid]['balance'] += amt
        else: users[tid]['balance'] = max(0, users[tid]['balance'] - amt)
        sync_db()
        return jsonify({'success': True, 'new_balance': users[tid]['balance']})
    return jsonify({'success': False})

@app.route('/api/admin/approve-deposit', methods=['POST'])
def approve_dep():
    rid = request.json.get('request_id')
    if rid in pending_deposits:
        dep = pending_deposits.pop(rid)
        tid, amt = str(dep['tg_id']), float(dep['amount'])
        if tid in users:
            users[tid]['balance'] += amt
            users[tid]['total_deposited'] = users[tid].get('total_deposited', 0) + amt
            sync_db()
            notify_user(tid, f"<b>✅ ተቀማጭ ተረጋግጧል!</b>\nመጠን: {amt} ETB\nቀሪ: {users[tid]['balance']:.2f} ETB")
            return jsonify({'success': True})
    return jsonify({'success': False})

@app.route('/api/admin/reject-deposit', methods=['POST'])
def reject_dep():
    rid = request.json.get('request_id')
    if rid in pending_deposits:
        dep = pending_deposits.pop(rid)
        sync_db()
        notify_user(dep['tg_id'], "<b>❌ ተቀማጭ ውድቅ ተደርጓል!</b>")
        return jsonify({'success': True})
    return jsonify({'success': False})

@app.route('/api/admin/approve-withdraw', methods=['POST'])
def approve_with():
    rid = request.json.get('request_id')
    if rid in pending_withdrawals:
        w = pending_withdrawals.pop(rid)
        sync_db()
        notify_user(w['tg_id'], f"<b>✅ እንኳን ደስ አልዎት የወጪ ጥያቄዎ ተቀባይነት አግኝቷል!</b>\nመጠን: {w['amount']} ETB\nብሩ በቴሌብር ከ30m - 1hr ውስጥ ይላክልዎታል")
        return jsonify({'success': True})
    return jsonify({'success': False})

@app.route('/api/deposit', methods=['POST'])
def submit_deposit():
    data = request.json
    rid = f"d_{int(time.time()*1000)}"
    pending_deposits[rid] = {'tg_id': data.get('telegram_id'), 'amount': data.get('amount'), 'telebirr_text': data.get('telebirr_text'), 'ts': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    sync_db()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ ፍቀድ", callback_data=f"app_dep_{rid}"), InlineKeyboardButton("❌ ውድቅ", callback_data=f"rej_dep_{rid}")]])
    notify_admin(f"<b>💰 አዲስ ተቀማጭ</b>\n\n👤 ተጠቃሚ: {data.get('telegram_id')}\n💵 መጠን: {data.get('amount')} ETB\n\n<b>📄 ቴክስት:</b>\n{data.get('telebirr_text')}", reply_markup=kb)
    return jsonify({'success': True})

@app.route('/api/withdraw', methods=['POST'])
def submit_withdraw():
    data = request.json
    tid = str(data.get('telegram_id'))
    amount = float(data.get('amount'))
    
    # 1. Check if user exists
    if tid not in users:
        return jsonify({'success': False, 'message': 'ተጠቃሚው አልተገኘም።'})
    
    # 2. Min withdrawal limit
    if amount < 100:
        return jsonify({'success': False, 'message': 'ዝቅተኛው የማውጫ መጠን 100 ETB ነው።'})
    
    # 3. Balance check
    if users[tid]['balance'] < amount:
        return jsonify({'success': False, 'message': 'በቂ ቀሪ ሂሳብ የለዎትም።'})

    # 4. Total deposit check (Min 100 ETB history)
    total_deposits = 0
    # Check approved deposits in users history if we tracked it, 
    # but based on current server.py, we only have pending_deposits and users['balance'].
    # We need to ensure we track total deposits in user object.
    if users[tid].get('total_deposited', 0) < 100:
        return jsonify({'success': False, 'message': 'ገንዘብ ለማውጣት ቢያንስ 100 ETB ዲፖዚት ማድረግ ይኖርብዎታል።'})

    rid = f"w_{int(time.time()*1000)}"
    users[tid]['balance'] -= amount
    pending_withdrawals[rid] = {'tg_id': tid, 'amount': amount, 'phone': data.get('phone'), 'ts': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    sync_db()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ ፍቀድ", callback_data=f"app_with_{rid}"), InlineKeyboardButton("❌ ውድቅ", callback_data=f"rej_with_{rid}")]])
    notify_admin(f"<b>💸 አዲስ ወጪ</b>\n\n👤 ተጠቃሚ: {tid}\n💵 መጠን: {amount} ETB\n📱 ስልክ: {data.get('phone')}", reply_markup=kb)
    return jsonify({'success': True})

if __name__ == '__main__':
    if TELEGRAM_BOT_TOKEN: 
        threading.Thread(target=run_bot_thread, daemon=True).start()
        threading.Thread(target=notification_worker, daemon=True).start()
    socketio.start_background_task(game_loop)
    socketio.run(app, host='0.0.0.0', port=5000)
