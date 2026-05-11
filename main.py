import os
import re
import csv
import json
import gspread
import time
import threading
from datetime import datetime, timedelta
from oauth2client.service_account import ServiceAccountCredentials
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- USER CONFIGURATION ---
TOKEN = '8679862520:AAHVb7-IP7LVQJSEy8LyfYeUlo9Qwr8Dx8k'
LOG_FILE = "parallax_submissions.csv"
MANUAL_REVIEW_FILE = "manual_review.csv" 
PENDING_REVIEWS_FILE = "pending_reviews.json" 

ADMIN_CHAT_ID = 5830563280  # Your Admin Panel ID

# 🚨 TARGET CHAT CONFIGURED BY LINK: t.me/c/3720126614/171
TARGET_CHAT_ID = -1003720126614 
TARGET_THREAD_ID = 171

# --- GOOGLE SHEETS CONFIGURATION ---
SPREADSHEET_NAME = "Ambassador_Rewards"

bot = telebot.TeleBot(TOKEN)
processing_lock = threading.Lock() 

# --- PERSISTENT MEMORY SYSTEM FOR MANUAL REVIEW ---
def load_reviews():
    if os.path.exists(PENDING_REVIEWS_FILE):
        try:
            with open(PENDING_REVIEWS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading pending reviews: {e}")
    return {}

def save_reviews():
    try:
        with open(PENDING_REVIEWS_FILE, 'w', encoding='utf-8') as f:
            json.dump(review_sessions, f, indent=4)
    except Exception as e:
        print(f"Error saving reviews: {e}")

review_sessions = load_reviews()

# --- SECURITY & LIMIT CONTROL FUNCTIONS ---

def escape_html(text):
    """Escapes HTML characters to avoid Telegram error 400 with parse_mode='HTML'."""
    if not text: return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def send_to_target_chat(username, text):
    """Sends the bot's response directly to the Target Thread, mentioning the user."""
    safe_user = escape_html(username)
    formatted_text = f"👤 <b>Submission from:</b> @{safe_user}\n\n{text}"
    
    max_retries = 3
    for i in range(max_retries):
        try:
            bot.send_message(
                TARGET_CHAT_ID, 
                formatted_text, 
                parse_mode="HTML",
                message_thread_id=TARGET_THREAD_ID,
                disable_web_page_preview=True
            )
            break
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 429:
                wait_time = int(re.search(r'after (\d+)', str(e)).group(1)) + 1
                time.sleep(wait_time)
            else:
                print(f"Telegram error in target msg: {e}")
                break
        except Exception as e:
            print(f"Critical error in target: {e}")
            break

def safe_answer_callback(call_id, text=None, show_alert=False):
    try:
        if text:
            bot.answer_callback_query(call_id, text, show_alert=show_alert)
        else:
            bot.answer_callback_query(call_id)
    except Exception:
        pass

def cleanup_old_logs():
    """Removes local database links older than 7 days."""
    if not os.path.exists(LOG_FILE): return
    try:
        valid_rows = []
        now = datetime.now()
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 3: continue
                try:
                    log_datetime = datetime.strptime(row[0].split('.')[0], '%Y-%m-%d %H:%M:%S')
                    if (now - log_datetime).days <= 7:
                        valid_rows.append(row)
                except ValueError:
                    pass
        
        with open(LOG_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(valid_rows)
    except Exception as e:
        print(f"Error cleaning 7-day logs: {e}")

def remove_log_entry(username, url):
    """Removes a specific link from the log so the user can submit another one."""
    if not os.path.exists(LOG_FILE): return
    try:
        valid_rows = []
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 3: continue
                if not (row[1] == username and row[2] == url):
                    valid_rows.append(row)
                    
        with open(LOG_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(valid_rows)
    except Exception as e:
        print(f"Error removing log entry: {e}")

def validate_submission_rules(username, url):
    with processing_lock:
        cleanup_old_logs() 
        
        if not os.path.exists(LOG_FILE): return True, ""
        
        today = datetime.now().date()
        now = datetime.now()
        
        user_today_urls = set()
        last_submission_time = None
        
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or len(row) < 3: continue
                try:
                    log_datetime = datetime.strptime(row[0].split('.')[0], '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue 
                
                log_date = log_datetime.date()
                log_user = row[1]
                log_url = row[2]
                log_status = row[3] if len(row) > 3 else ""

                if url == log_url:
                    return False, "❌ This link has already been validated recently (within 7 days)."
                
                # Only count confirmed/scored entries toward the daily limit and cooldown,
                # ignoring submissions still awaiting manual review.
                if log_user == username and log_date == today and log_status != "PENDING_MANUAL":
                    user_today_urls.add(log_url) 
                    if last_submission_time is None or log_datetime > last_submission_time:
                        last_submission_time = log_datetime
                
        if len(user_today_urls) >= 2:
            return False, "❌ You have already reached your limit of 2 scored posts for today."
        
        if last_submission_time:
            time_diff_seconds = (now - last_submission_time).total_seconds()
            if time_diff_seconds < 3600: 
                minutes_left = int((3600 - time_diff_seconds) / 60)
                return False, f"⏳ Please wait {minutes_left} minutes before submitting another link to prevent spam."
                    
        return True, ""

# --- GOOGLE SHEETS FUNCTION ---

def update_sheets_points(username, score):
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        
        # Puxa o conteúdo do JSON da variável de ambiente do Railway
        sheets_credentials_json = os.environ.get("GOOGLE_SHEETS_JSON_CONTENT")
        
        if not sheets_credentials_json:
            print("Error: GOOGLE_SHEETS_JSON_CONTENT not found in environment!")
            return False, "⚠️ Credentials missing in Railway environment."

        # Converte a string JSON para um dicionário Python e autentica
        creds_dict = json.loads(sheets_credentials_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        # Conecta exatamente na aba "May"
        sheet = client.open(SPREADSHEET_NAME).worksheet("May")
        
        formatted_username = f"@{username}" if not username.startswith("@") else username
        col_c_values = sheet.col_values(3)
        row_index = None
        
        for i, val in enumerate(col_c_values):
            if val.strip().lower() == formatted_username.lower():
                row_index = i + 1 
                break
        
        if row_index:
            coluna_pontos = 6 
            current_points_str = sheet.cell(row_index, coluna_pontos).value
            current_points = int(current_points_str) if current_points_str else 0
            new_total = current_points + score
            sheet.update_cell(row_index, coluna_pontos, new_total)
            return True, "✅ Points updated in the Leaderboard."
        else:
            return False, "⚠️ Score logged locally, but username was not found in the Official Leaderboard."
            
    except Exception as e:
        print(f"Google Sheets Error: {e}")
        return False, "⚠️ Score logged locally, but failed to connect to the Leaderboard."

# --- MANUAL REVIEW ROUTER ---

def route_to_manual_review(username, url):
    """Forwards ANY AND ALL LINKS to structured manual review."""
    try:
        with processing_lock:
            with open(MANUAL_REVIEW_FILE, 'a', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([f"@{username}", url, datetime.now().strftime('%Y-%m-%d %H:%M:%S')])
            
            with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), username, url, "PENDING_MANUAL"])
    except PermissionError:
        send_to_target_chat(username, "⚠️ The system is currently updating (File locked). Please try submitting again in a few moments!")
        return
        
    send_to_target_chat(username, "✅ <b>Link submitted for manual review!</b> You will be notified here when the evaluation is complete.")
    
    safe_user = escape_html(username)
    safe_url = escape_html(url)
    
    admin_text = f"🚨 <b>Manual Review Required</b> 🚨\n👤 User: @{safe_user}\n🔗 <a href=\"{safe_url}\">Access Link</a>"
    try:
        msg = bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode="HTML", disable_web_page_preview=True)
        msg_id_str = str(msg.message_id) 
        
        review_sessions[msg_id_str] = {
            'user': username,
            'url': url,
            'pts_valid': False,
            'pts_hash': False,
            'pts_key': False,
            'pts_code': False,
            'pts_text': False,
            'pts_image': False
        }
        save_reviews() 
        bot.edit_message_reply_markup(ADMIN_CHAT_ID, msg.message_id, reply_markup=build_review_keyboard(msg_id_str))
    except Exception as e:
        print(f"Error sending to admin panel: {e}")

# --- ADMIN PANEL INTERFACE ---

def build_review_keyboard(msg_id_str):
    state = review_sessions.get(msg_id_str, None)
    if not state: return None
    
    markup = InlineKeyboardMarkup()
    btn_valid = InlineKeyboardButton(f"{'✅' if state['pts_valid'] else '⬜️'} Valid Link (+1)", callback_data=f"rev_valid_{msg_id_str}")
    btn_hash = InlineKeyboardButton(f"{'✅' if state['pts_hash'] else '⬜️'} Hashtag (+1)", callback_data=f"rev_hash_{msg_id_str}")
    btn_key = InlineKeyboardButton(f"{'✅' if state['pts_key'] else '⬜️'} PAX Word (+1)", callback_data=f"rev_key_{msg_id_str}")
    btn_code = InlineKeyboardButton(f"{'✅' if state['pts_code'] else '⬜️'} Code (+2)", callback_data=f"rev_code_{msg_id_str}")
    btn_text = InlineKeyboardButton(f"{'✍️' if state['pts_text'] else '⬜️'} Text Quality (+2)", callback_data=f"rev_text_{msg_id_str}")
    btn_image = InlineKeyboardButton(f"{'🖼' if state['pts_image'] else '⬜️'} Image Quality (+3)", callback_data=f"rev_image_{msg_id_str}")

    btn_confirm = InlineKeyboardButton("🚀 CONFIRM AND ADD", callback_data=f"rev_confirm_{msg_id_str}")
    btn_invalid = InlineKeyboardButton("⚠️ INVALID LINK", callback_data=f"rev_invalid_{msg_id_str}")
    btn_duplicate = InlineKeyboardButton("🔁 DUPLICATE POST", callback_data=f"rev_duplicate_{msg_id_str}")
    btn_reject = InlineKeyboardButton("❌ REJECT (0 pts)", callback_data=f"rev_reject_{msg_id_str}")

    markup.row(btn_valid, btn_hash)
    markup.row(btn_key, btn_code)
    markup.row(btn_text, btn_image)
    markup.row(btn_confirm)
    markup.row(btn_invalid, btn_duplicate)
    markup.row(btn_reject)
    return markup

@bot.callback_query_handler(func=lambda call: call.data.startswith('rev_'))
def handle_review_buttons(call):
    action = call.data.split('_')[1]
    msg_id_str = call.data.split('_')[2] 
    
    if msg_id_str not in review_sessions:
        safe_answer_callback(call.id, "Session expired or already evaluated.")
        return

    state = review_sessions[msg_id_str]
    safe_user = escape_html(state['user'])
    safe_url = escape_html(state['url'])

    if action in ['valid', 'hash', 'key', 'code', 'text', 'image']:
        state[f'pts_{action}'] = not state[f'pts_{action}']
        save_reviews()
        try:
            bot.edit_message_reply_markup(call.message.chat.id, int(msg_id_str), reply_markup=build_review_keyboard(msg_id_str))
        except Exception: pass
        safe_answer_callback(call.id)
        
    elif action == 'confirm':
        score = 0
        if state['pts_valid']: score += 1
        if state['pts_hash']: score += 1
        if state['pts_key']: score += 1
        if state['pts_code']: score += 2
        if state['pts_text']: score += 2
        if state['pts_image']: score += 3

        if score == 0:
            safe_answer_callback(call.id, "Attention: The score is zero. Use the REJECT button if applicable.", show_alert=True)
            return

        try:
            with processing_lock:
                with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                    csv.writer(f).writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), state['user'], state['url'], score])
        except PermissionError:
            safe_answer_callback(call.id, "Error: Close the CSV in Excel before confirming!", show_alert=True)
            return
            
        sheets_success, sheets_message = update_sheets_points(state['user'], score)
        
        try:
            bot.edit_message_text(
                f"✅ <b>EVALUATED!</b>\n👤 @{safe_user} received <b>{score} points</b>.\n🔗 <a href=\"{safe_url}\">Access Link</a>", 
                chat_id=call.message.chat.id, 
                message_id=int(msg_id_str),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception: pass
            
        user_msg = "📊 <b>Validation Report (Manual)</b>\n\n"
        if state['pts_valid']: user_msg += "• Valid link (+1)\n"
        if state['pts_hash']: user_msg += "• Hashtag #parallaxnetwork detected (+1)\n"
        if state['pts_key']: user_msg += "• Keywords 'PAX/Parallax' detected (+1)\n"
        if state['pts_code']: user_msg += "• Invite code detected (+2)\n"
        if state['pts_text']: user_msg += "• High text quality (+2)\n"
        if state['pts_image']: user_msg += "• High image/video quality (+3)\n"
        
        user_msg += f"\n🏆 <b>Final Score: {score} points</b>\n{sheets_message}\n🔗 <a href=\"{safe_url}\">Access Post</a>"
        
        send_to_target_chat(state['user'], user_msg)

        del review_sessions[msg_id_str]
        save_reviews()
        safe_answer_callback(call.id)

    elif action == 'invalid':
        with processing_lock:
            remove_log_entry(state['user'], state['url'])

        try:
            bot.edit_message_text(
                f"⚠️ <b>INVALID LINK</b>\n👤 @{safe_user} (0 points).\n🔗 <a href=\"{safe_url}\">Access Link</a>", 
                chat_id=call.message.chat.id, 
                message_id=int(msg_id_str),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception: pass
            
        reject_msg = f"⚠️ <b>Link could not be verified.</b>\nWe were unable to access or verify the content of your link. It has been removed from our system, so you are free to submit a new, functional link to replace it.\n🔗 <a href=\"{safe_url}\">Access Post</a>"
        send_to_target_chat(state['user'], reject_msg)

        del review_sessions[msg_id_str]
        save_reviews()
        safe_answer_callback(call.id)

    elif action == 'duplicate':
        with processing_lock:
            remove_log_entry(state['user'], state['url'])

        try:
            bot.edit_message_text(
                f"🔁 <b>DUPLICATE POST</b>\n👤 @{safe_user} (0 points).\n🔗 <a href=\"{safe_url}\">Access Link</a>", 
                chat_id=call.message.chat.id, 
                message_id=int(msg_id_str),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception: pass
            
        reject_msg = f"🔁 <b>Duplicate Content.</b>\nThis post seems to be a duplicate or highly similar to another recent submission. It has been removed from your daily limit, please submit a different post.\n🔗 <a href=\"{safe_url}\">Access Post</a>"
        send_to_target_chat(state['user'], reject_msg)

        del review_sessions[msg_id_str]
        save_reviews()
        safe_answer_callback(call.id)

    elif action == 'reject':
        try:
            bot.edit_message_text(
                f"❌ <b>REJECTED!</b>\n👤 @{safe_user} (0 points).\n🔗 <a href=\"{safe_url}\">Access Link</a>", 
                chat_id=call.message.chat.id, 
                message_id=int(msg_id_str),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception: pass
            
        reject_msg = f"❌ <b>Validation failed.</b>\nYour link was rejected by the moderation team (0 points).\n🔗 <a href=\"{safe_url}\">Access Post</a>"
        send_to_target_chat(state['user'], reject_msg)

        del review_sessions[msg_id_str]
        save_reviews()
        safe_answer_callback(call.id)

# --- MANUAL POINTS COMMANDS ---

@bot.message_handler(commands=['addpoints', 'removepoints'])
def handle_manual_points(message):
    if message.chat.id != ADMIN_CHAT_ID:
        return

    parts = message.text.split()
    if len(parts) != 3:
        bot.reply_to(message, "Usage: /addpoints @username 10\nOr: /removepoints @username 10")
        return

    command = parts[0].lower()
    username = parts[1].replace("@", "")
    
    try:
        points = int(parts[2])
    except ValueError:
        bot.reply_to(message, "Points must be an integer.")
        return

    if command == "/removepoints":
        points = -abs(points)
        action_word = "removed from"
    else:
        points = abs(points)
        action_word = "added to"

    success, msg = update_sheets_points(username, points)
    
    if success:
        bot.reply_to(message, f"✅ Successfully {action_word} @{username}: {abs(points)} points.")
        send_to_target_chat(username, f"🛠 <b>Admin Action:</b> {abs(points)} points have been {action_word} your account.")
    else:
        bot.reply_to(message, f"❌ Failed to update points for @{username}.\nReason: {msg}")

# --- TELEGRAM INTERFACE ---

@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'video', 'document', 'animation'])
def handle_submission(message):
    text = message.text or message.caption
    if not text: return

    is_private = message.chat.type == 'private'
    is_command = text.lower().startswith('/validate')

    if not is_private and not is_command:
        return

    raw_text = re.sub(r'^/validate(?:@[a-zA-Z0-9_]+)?', '', text, flags=re.IGNORECASE).strip()
    username = message.from_user.username
    
    if not username:
        send_to_target_chat("UnknownUser", "❌ Your Telegram account must have a public @username to use this bot.")
        return

    if len(raw_text.split()) > 1 or raw_text.count("http") > 1:
        send_to_target_chat(username, "❌ Only one link per submission, please.")
        return

    url = raw_text

    if not (url.lower().startswith("http") or url.lower().startswith("www.")):
        send_to_target_chat(username, "❌ Please provide a valid link starting with http, https or www.")
        return

    try:
        allowed, reason = validate_submission_rules(username, url)
    except PermissionError:
        send_to_target_chat(username, "⚠️ The database is open on the Admin's computer. Please try again in 1 minute.")
        return

    if not allowed:
        send_to_target_chat(username, f"❌ {reason}")
        return

    route_to_manual_review(username, url)

if __name__ == "__main__":
    if not os.path.exists(MANUAL_REVIEW_FILE):
        with open(MANUAL_REVIEW_FILE, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(['Username', 'Link', 'Submission_Date'])
            
    print(f"🚀 Parallax Auditor System Online (100% Manual Review Enabled, Secure HTML Parse)...")
    
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"Polling Error: {e}. Restarting in 5 seconds...")
            time.sleep(5)
