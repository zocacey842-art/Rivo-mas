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

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET', 'aviator_pro_secure_key')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Configuration
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
DOMAIN = os.environ.get('REPLIT_DEV_DOMAIN')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID')

# --- Persistent Data Management ---
DATA_FILE = 'game_data.json'

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                if 'notif_queue' not in data: data['notif_queue'] = []
                if 'broadcasts' not in data: data['broadcasts'] = []
                if 'history' not in data: data['history'] = []
                return data
        except: return {'users': {}, 'withdrawals': {}, 'deposits': {}, 'notif_queue': [], 'broadcasts': [], 'history': []}
    return {'users': {}, 'withdrawals': {}, 'deposits': {}, 'notif_queue': [], 'broadcasts': [], 'history': []}

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f)

data_store = load_data()
users = data_store['users']
pending_withdrawals = data_store['withdrawals']
pending_deposits = data_store['deposits']
notification_queue = data_store['notif_queue']
broadcast_history = data_store.get('broadcasts', [])
game_history = data_store.get('history', [])

def sync_db():
    save_data({
        'users': users, 
        'withdrawals': pending_withdrawals, 
        'deposits': pending_deposits,
        'notif_queue': notification_queue,
        'broadcasts': broadcast_history,
        'history': game_history
    })

# --- Notification Queue Service ---
def notify_user(chat_id, text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id: return
    notif = {
        'chat_id': str(chat_id),
        'text': text,
        'reply_markup': reply_markup.to_dict() if hasattr(reply_markup, 'to_dict') else reply_markup,
        'ts': time.time()
    }
    notification_queue.append(notif)
    sync_db()

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
                status = "success" if resp.ok and data.get("ok") else "failed"
                
                if status == "success":
                    notification_queue.pop(0)
                    sync_db()
                    logger.info(f"Worker: Sent notif to {notif['chat_id']}")
                    time.sleep(0.05)
                else:
                    err_msg = data.get('description', 'Unknown error')
                    logger.error(f"Worker Error: {err_msg}")
                    if "forbidden" in err_msg.lower() or "chat not found" in err_msg.lower():
                        notification_queue.pop(0)
                        sync_db()
                    else:
                        time.sleep(2)
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
    # House edge: 70%
    # Extremely aggressive profit mode
    # Instant crash at 1.00x: 40% chance
    # Very low multipliers (1.01 - 1.10): 40% chance
    # Low multipliers (1.11 - 1.30): 15% chance
    # Rare higher multipliers (> 1.30): 5% chance
    
    chance = random.random()
    
    if chance < 0.40: # 40% Instant crash at 1.00x
        return 1.00
    elif chance < 0.80: # 40% chance for extremely low (1.01 - 1.10)
        return round(random.uniform(1.01, 1.10), 2)
    elif chance < 0.95: # 15% chance for low (1.11 - 1.30)
        return round(random.uniform(1.11, 1.30), 2)
    else: # 5% chance for rare "spikes" up to 2.0x
        return round(random.uniform(1.31, 2.00), 2)

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
    chat_id = str(update.message.chat_id)
    ref = context.args[0] if context.args else None
    if chat_id not in users:
        users[chat_id] = {'phone': None, 'balance': 5.0, 'referred_by': ref, 'reg_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if ref and ref in users and ref != chat_id:
            users[ref]['balance'] += 2.0
            notify_user(ref, "<b>🎁 የሪፈራል ቦነስ!</b>\nአዲስ ሰው ስለጋበዙ 2.00 ETB ተጨምሯል።")
        sync_db()
    if not users[chat_id].get('phone'):
        btn = KeyboardButton(text="📱 ስልክ ቁጥርዎን ያጋሩ", request_contact=True)
        await update.message.reply_text("👋 እንኳን በደህና መጡ! ለመመዝገብ እባክዎ ስልክዎን ያጋሩ።", reply_markup=ReplyKeyboardMarkup([[btn]], resize_keyboard=True))
    else: await show_main_menu(update, chat_id)

async def show_main_menu(update, chat_id):
    kb = [[KeyboardButton("👤 ፕሮፋይል"), KeyboardButton("💰 ዋሌት")], [KeyboardButton("💳 ተቀማጭ"), KeyboardButton("💸 ወጪ")], [KeyboardButton("🔗 ሪፈራል"), KeyboardButton("🎮 ወደ ጨዋታው")]]
    if str(chat_id) == str(ADMIN_CHAT_ID): kb.append([KeyboardButton("📢 ብሮድካስት")])
    await update.message.reply_text("ዋና ዝርዝር:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def handle_message(update, context):
    if not update.message or not update.message.text: return
    text, chat_id = update.message.text, str(update.message.chat_id)
    if ADMIN_CHAT_ID and str(chat_id) == str(ADMIN_CHAT_ID):
        if text.startswith("/broadcast "):
            msg = text.replace("/broadcast ", "", 1).strip()
            if msg:
                count = create_broadcast(f"<b>📢 መልዕክት ከአድሚን:</b>\n\n{msg}")
                await update.message.reply_text(f"🚀 ብሮድካስት ለ {count} ተጠቃሚዎች በኩዌ (Queue) በኩል እየተላከ ነው...")
            else:
                await update.message.reply_text("⚠️ እባክዎ መልዕክት ይጻፉ።")
            return
        elif text == "📢 ብሮድካስት":
            await update.message.reply_text("እንዲህ ይጻፉ: `/broadcast መልዕክትዎ`", parse_mode='Markdown')
            return

    if chat_id not in users: return await start_command(update, context)
    user = users[chat_id]
    if text == "👤 ፕሮፋይል": 
        await update.message.reply_text(f"<b>👤 ፕሮፋይል</b>\nID: {chat_id}\nስልክ: {user['phone']}\nቀሪ ሂሳብ: {user['balance']:.2f} ETB")
    elif text == "💰 ዋሌት": 
        await update.message.reply_text(f"<b>💰 ዋሌት</b>\nቀሪ ሂሳብ: {user['balance']:.2f} ETB")
    elif text == "💳 ተቀማጭ":
        msg = (
            "<b>💳 ገንዘብ ለማስገባት (Deposit)</b>\n\n"
            "1. ወደዚ የቴሌብር ቁጥር ብር ይላኩ: <code>0975118009</code>\n"
            "2. ብር ከላኩ በኋላ ከቴሌብር የደረስዎትን ሙሉ መልዕክት ኮፒ ያድርጉ\n"
            "3. ወደ ጨዋታው ዌብሳይት በመሄድ ተቀማጭ (Deposit) የሚለውን ይጫኑ\n"
            "4. የላኩትን የብር መጠን እና የቴሌብር መልዕክቱን ያስገቡ\n\n"
            "<i>አድሚኑ መረጃውን እንዳረጋገጠ ወዲያውኑ ብሩ በዋሌትዎ ላይ ይታያል።</i>"
        )
        await update.message.reply_text(msg, parse_mode='HTML')
    elif text == "💸 ወጪ":
        msg = (
            "<b>💸 ገንዘብ ለማውጣት (Withdraw)</b>\n\n"
            "1. ወደ ጨዋታው ዌብሳይት ይግቡ\n"
            "2. ወጪ (Withdraw) የሚለውን ምርጫ ይጫኑ\n"
            "3. ማውጣት የሚፈልጉትን የብር መጠን እና የቴሌብር ስልክ ቁጥርዎን ያስገቡ\n"
            "4. ጥያቄዎን ይላኩ\n\n"
            "<i>ማሳሰቢያ: ዝቅተኛው የማውጫ መጠን 100 ETB ነው።</i>"
        )
        await update.message.reply_text(msg, parse_mode='HTML')
    elif text == "🔗 ሪፈራል":
        ref_link = f"https://t.me/revoavio_bot?start={chat_id}"
        msg = (
            "<b>🔗 የሪፈራል ሊንክ</b>\n\n"
            "ይህንን ሊንክ ለጓደኞችዎ በመላክ ይጋብዙ። አዲስ ሰው ሲጋብዙ የ2.00 ETB ቦነስ ያገኛሉ!\n\n"
            f"የእርስዎ ሊንክ: <code>{ref_link}</code>"
        )
        await update.message.reply_text(msg, parse_mode='HTML')
    elif text == "🎮 ወደ ጨዋታው":
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 ጨዋታውን ክፈት", url=f"https://{DOMAIN}/?tid={chat_id}")]])
        await update.message.reply_text("ወደ ጨዋታው ለመግባት:", reply_markup=markup)
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

def run_bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app_bot = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start_command))
    app_bot.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app_bot.add_handler(CallbackQueryHandler(button_handler))
    loop.run_until_complete(app_bot.initialize())
    loop.run_until_complete(app_bot.start())
    loop.run_until_complete(app_bot.updater.start_polling())
    loop.run_forever()

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
