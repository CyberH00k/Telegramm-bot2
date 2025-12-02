import os
import re
import threading
import time
from datetime import datetime, date, timedelta
from telebot import TeleBot, types, apihelper
import sqlite3

# === НАСТРОЙКИ ===
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("❌ Переменная BOT_TOKEN не задана.")

# 🔒 Список доверенных пользователей (оставьте пустым для публичного бота)
ALLOWED_USER_IDS = set()

DB_PATH = 'walk_private.db'
REMINDER_CHECK_INTERVAL = 30  # секунд

bot = TeleBot(BOT_TOKEN)

# === КОНСТАНТЫ ===
MONTH_NAMES = {
    1: 'января', 2: 'февраля', 3: 'марта', 4: 'апреля',
    5: 'мая', 6: 'июня', 7: 'июля', 8: 'августа',
    9: 'сентября', 10: 'октября', 11: 'ноября', 12: 'декабря'
}

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

def check_allowed(user_id):
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return False
    return True

def allowed_only(func):
    def wrapper(message):
        if ALLOWED_USER_IDS and message.from_user.id not in ALLOWED_USER_IDS:
            bot.reply_to(message, "🔒 Этот бот доступен только по приглашению.")
            return
        return func(message)
    return wrapper

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposer_id INTEGER NOT NULL,
                proposer_name TEXT NOT NULL,
                time_str TEXT NOT NULL,
                walk_datetime DATETIME NOT NULL,
                location TEXT DEFAULT '',
                comment TEXT DEFAULT '',
                editable BOOLEAN DEFAULT 1,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                processed BOOLEAN DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS votes (
                proposal_id INTEGER,
                voter_id INTEGER,
                voter_name TEXT,
                vote_type TEXT DEFAULT 'yes',
                PRIMARY KEY (proposal_id, voter_id),
                FOREIGN KEY (proposal_id) REFERENCES proposals (id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_proposal_messages (
                user_id INTEGER,
                proposal_id INTEGER,
                message_id INTEGER,
                PRIMARY KEY (user_id, proposal_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_proposal_counts (
                user_id INTEGER,
                date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS comments (
                proposal_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                comment TEXT NOT NULL,
                PRIMARY KEY (proposal_id, user_id),
                FOREIGN KEY (proposal_id) REFERENCES proposals (id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                reminder_minutes INTEGER DEFAULT 10
            )
        ''')

def cleanup_old_counts():
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM daily_proposal_counts WHERE date < ?", (today,))
        conn.commit()

def add_user(user_id, first_name, username):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, first_name, username) VALUES (?, ?, ?)",
            (user_id, first_name, username)
        )

def get_all_users():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, first_name, username FROM users")
        return cursor.fetchall()

def can_propose(user_id):
    cleanup_old_counts()
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT count FROM daily_proposal_counts WHERE user_id = ? AND date = ?",
            (user_id, today)
        )
        row = cursor.fetchone()
        count = row[0] if row else 0
        return count < 3

def increment_proposal_count(user_id):
    cleanup_old_counts()
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO daily_proposal_counts (user_id, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1",
            (user_id, today)
        )

def parse_proposal_datetime(input_str):
    now = datetime.now()
    input_clean = input_str.strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}$', input_clean):
        try:
            return datetime.strptime(input_clean, "%Y-%m-%d %H:%M")
        except ValueError:
            return None
    if re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', input_clean):
        try:
            t = datetime.strptime(input_clean, "%H:%M")
            proposed = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if proposed <= now:
                proposed += timedelta(days=1)
            return proposed
        except:
            return None
    return None

def get_all_message_ids_for_proposal(proposal_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id, message_id FROM user_proposal_messages WHERE proposal_id = ?",
            (proposal_id,)
        )
        return cursor.fetchall()

def save_message_id(user_id, proposal_id, message_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO user_proposal_messages (user_id, proposal_id, message_id) VALUES (?, ?, ?)",
            (user_id, proposal_id, message_id)
        )

def get_message_id(user_id, proposal_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT message_id FROM user_proposal_messages WHERE user_id = ? AND proposal_id = ?",
            (user_id, proposal_id)
        )
        row = cursor.fetchone()
        return row[0] if row else None

def save_comment(proposal_id, user_id, user_name, comment):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO comments (proposal_id, user_id, user_name, comment)
            VALUES (?, ?, ?, ?)
        """, (proposal_id, user_id, user_name, comment))

def get_comments(proposal_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_name, comment FROM comments WHERE proposal_id = ?
        """, (proposal_id,))
        return {user_name: comment for user_name, comment in cursor.fetchall()}

def add_proposal(proposer_id, proposer_name, time_str, walk_datetime, location="", comment=""):
    walk_dt_str = walk_datetime.strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO proposals 
               (proposer_id, proposer_name, time_str, walk_datetime, location, comment, editable) 
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (proposer_id, proposer_name, time_str, walk_dt_str, location, comment)
        )
        return cursor.lastrowid

def get_proposal_author(proposal_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT proposer_id, proposer_name, time_str, walk_datetime, location, comment 
            FROM proposals WHERE id = ?
        """, (proposal_id,))
        return cursor.fetchone()

def add_vote(proposal_id, voter_id, voter_name, vote_type='yes'):
    if vote_type not in ('yes', 'later', 'no'):
        vote_type = 'yes'
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO votes (proposal_id, voter_id, voter_name, vote_type) VALUES (?, ?, ?, ?)",
                (proposal_id, voter_id, voter_name, vote_type)
            )
        except sqlite3.IntegrityError:
            cursor.execute(
                "UPDATE votes SET vote_type = ?, voter_name = ? WHERE proposal_id = ? AND voter_id = ?",
                (vote_type, voter_name, proposal_id, voter_id)
            )

def get_votes(proposal_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT voter_name, vote_type FROM votes WHERE proposal_id = ?",
            (proposal_id,)
        )
        rows = cursor.fetchall()
    result = {'yes': [], 'later': [], 'no': []}
    for name, vtype in rows:
        if vtype in result:
            result[vtype].append(name)
    return result

def auto_delete_old_proposals_by_walk_time():
    six_hours_ago = datetime.now() - timedelta(hours=6)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id FROM proposals
            WHERE walk_datetime < ? AND processed = 0
        """, (six_hours_ago.strftime('%Y-%m-%d %H:%M:%S'),))
        candidate_ids = [row[0] for row in cursor.fetchall()]
        deleted_count = 0
        for pid in candidate_ids:
            cursor.execute("SELECT COUNT(*) FROM votes WHERE proposal_id = ? AND vote_type = 'yes'", (pid,))
            yes_votes = cursor.fetchone()[0]
            if yes_votes == 0:
                cursor.execute("DELETE FROM proposals WHERE id = ?", (pid,))
                deleted_count += 1
        if deleted_count > 0:
            print(f"🗑️ Удалено {deleted_count} безответных предложений")

def cleanup_old_proposals():
    now = datetime.now()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM proposals 
            WHERE walk_datetime < ? AND walk_datetime > datetime('now', '-7 days')
        """, ((now - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S'),))
        deleted_24h = cursor.rowcount
        seven_days_ago = now - timedelta(days=7)
        cursor.execute("DELETE FROM proposals WHERE timestamp < ?", (seven_days_ago.strftime('%Y-%m-%d %H:%M:%S'),))
        deleted_7d = cursor.rowcount - deleted_24h
        if deleted_24h:
            print(f"🧹 Удалено {deleted_24h} предложений (прошло 24ч после прогулки)")
        if deleted_7d:
            print(f"🧹 Удалено {deleted_7d} очень старых предложений")

def set_reminder_minutes(user_id, minutes):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO user_settings (user_id, reminder_minutes) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET reminder_minutes = ?",
            (user_id, minutes, minutes)
        )

def get_reminder_minutes(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT reminder_minutes FROM user_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else 10

def format_walk_date(walk_dt: datetime) -> str:
    now = datetime.now()
    day = walk_dt.day
    month = MONTH_NAMES.get(walk_dt.month, str(walk_dt.month))
    if walk_dt.date() == now.date():
        return "сегодня"
    elif walk_dt.date() == (now + timedelta(days=1)).date():
        return "завтра"
    else:
        return f"{day} {month}"

# === ФУНКЦИЯ: ТЕКУЩИЕ ПРОГУЛКИ ===

def get_current_proposals():
    """Возвращает все предложения, время которых ещё не прошло."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, proposer_name, time_str, walk_datetime, location, comment
            FROM proposals
            WHERE walk_datetime > ?
            ORDER BY walk_datetime ASC
        """, (now,))
        return cursor.fetchall()

# === КЛАВИАТУРЫ ===

def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("Прогулки", "Настройки")
    markup.add("Помощь")
    return markup

def walks_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("Предложить время")
    markup.add("Мои предложения")
    markup.add("Текущие прогулки")
    markup.add("Назад")
    return markup

def settings_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add("Напоминания")
    markup.add("Очистить старые")
    markup.add("Назад")
    return markup

def update_all_messages_with_details(proposal_id, proposer_name, time_str, location="", base_comment=""):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT walk_datetime FROM proposals WHERE id = ?", (proposal_id,))
        row = cursor.fetchone()
        if not row:
            return
        walk_dt_str = row[0]
    walk_datetime = datetime.strptime(walk_dt_str, '%Y-%m-%d %H:%M:%S')
    date_str = format_walk_date(walk_datetime)
    full_time_display = f"{time_str}, {date_str}"
    votes = get_votes(proposal_id)
    user_comments = get_comments(proposal_id)

    def format_name_with_comment(name):
        comment = user_comments.get(name, "")
        return f"{name} — {comment}" if comment else name

    yes_list = "\n".join([f"• {format_name_with_comment(name)}" for name in votes['yes']]) or "Пока никто"
    later_list = "\n".join([f"• {format_name_with_comment(name)}" for name in votes['later']]) or "Никто не отметил"
    no_list = "\n".join([f"• {name}" for name in votes['no']]) or "Все ещё в раздумьях"

    text = f"📅 <b>Прогулка: {full_time_display}</b>\n"
    if location:
        text += f"📍 <b>Место:</b> {location}\n"
    if base_comment:
        text += f"💬 <b>От автора:</b> {base_comment}\n"
    text += f"\nОт: {proposer_name}\n"
    text += f"✅ <b>Выйду гулять:</b>\n{yes_list}\n"
    text += f"🕗 <b>Выйду позже:</b>\n{later_list}\n"
    text += f"❌ <b>Не пойду:</b>\n{no_list}"

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Выйду гулять", callback_data=f"vote_yes_{proposal_id}"),
        types.InlineKeyboardButton("🕗 Выйду позже", callback_data=f"vote_later_{proposal_id}")
    )
    markup.add(
        types.InlineKeyboardButton("❌ Не пойду", callback_data=f"vote_no_{proposal_id}")
    )

    users = get_all_users()
    for user_id, first_name, username in users:
        try:
            msg_id = get_message_id(user_id, proposal_id)
            if msg_id:
                try:
                    bot.edit_message_text(
                        chat_id=user_id,
                        message_id=msg_id,
                        text=text,
                        reply_markup=markup,
                        parse_mode='HTML'
                    )
                except apihelper.ApiTelegramException as e:
                    if "message is not modified" in str(e):
                        pass
                    else:
                        print(f"Ошибка при редактировании для {user_id}: {e}")
            else:
                try:
                    sent = bot.send_message(user_id, text, reply_markup=markup, parse_mode='HTML')
                    save_message_id(user_id, proposal_id, sent.message_id)
                except Exception as e:
                    print(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
        except Exception as e:
            print(f"Не удалось обработать сообщение для {user_id}: {e}")

# === ВВОД ДАННЫХ ===

def process_time_input_from_button(message):
    # Защита от стикеров, гифок и т.д.
    if not message.text:
        bot.send_message(message.chat.id, "❌ Я принимаю только текст. Пожалуйста, введите время в формате ЧЧ:ММ.")
        return

    if message.text.startswith('/') or message.text in [
        "Предложить время",
        "Мои предложения",
        "Текущие прогулки",
        "Назад",
        "Напоминания",
        "Очистить старые",
        "Помощь",
        "Прогулки",
        "Настройки"
    ]:
        bot.send_message(message.chat.id, "❌ Ожидание времени отменено.", reply_markup=main_menu())
        return

    time_str = message.text.strip()
    if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', time_str):
        bot.send_message(message.chat.id, "❌ Неверный формат. Напишите ЧЧ:ММ (например, 18:30):")
        bot.register_next_step_handler(message, process_time_input_from_button)
        return

    user_id = message.from_user.id
    if not can_propose(user_id):
        bot.send_message(message.chat.id, "❌ Лимит исчерпан: можно предлагать не более 3 раз в день.")
        return

    walk_time = parse_proposal_datetime(time_str)
    if walk_time is None:
        bot.send_message(message.chat.id, "❌ Не удалось распознать время.")
        return

    if walk_time <= datetime.now():
        bot.send_message(message.chat.id, "❌ Время уже прошло. Предложите прогулку в будущем.")
        return

    user_name = message.from_user.first_name or message.from_user.username or "Аноним"
    bot.send_message(message.chat.id, "📍 Укажите место встречи:")
    bot.register_next_step_handler(
        message, ask_for_location,
        time_str=time_str, walk_time=walk_time, user_name=user_name, user_id=user_id
    )

def ask_for_location(message, time_str, walk_time, user_name, user_id):
    if not message.text:
        bot.send_message(message.chat.id, "❌ Я понимаю только текст. Пожалуйста, введите текстовое сообщение.")
        return
    if message.text in [
        "Предложить время", "Мои предложения", "Текущие прогулки", "Назад",
        "Напоминания", "Очистить старые", "Помощь", "Прогулки", "Настройки"
    ] or message.text.startswith('/'):
        bot.send_message(message.chat.id, "❌ Ожидание отменено.", reply_markup=main_menu())
        return
    location = message.text.strip()
    bot.send_message(message.chat.id, "🗨️ Напишите комментарий (или '-' для пропуска):")
    bot.register_next_step_handler(
        message, ask_for_comment,
        time_str=time_str, walk_time=walk_time, user_name=user_name, user_id=user_id, location=location
    )

def ask_for_comment(message, time_str, walk_time, user_name, user_id, location):
    if not message.text:
        bot.send_message(message.chat.id, "❌ Я понимаю только текст. Пожалуйста, введите текстовое сообщение.")
        return

    if message.text in [
        "Предложить время", "Мои предложения", "Текущие прогулки", "Назад",
        "Напоминания", "Очистить старые", "Помощь", "Прогулки", "Настройки"
    ] or message.text.startswith('/'):
        bot.send_message(message.chat.id, "❌ Ожидание отменено.", reply_markup=main_menu())
        return
    comment = message.text.strip()
    if comment in [".", "-", ""]:
        comment = ""
    proposal_id = add_proposal(user_id, user_name, time_str, walk_time, location, comment)
    increment_proposal_count(user_id)
    date_part = walk_time.strftime('%d.%m в %H:%M')
    bot.send_message(
        message.chat.id,
        f"✅ Предложение на {date_part}\n"
        f"📍 Место: {location}\n"
        f"💬 Комментарий: {comment or '—'}\n"
        f"Отправлено всем!",
        reply_markup=main_menu()
    )
    update_all_messages_with_details(proposal_id, user_name, time_str, location, comment)

def ask_for_location_after_propose(message, time_str, walk_time, user_name, user_id):
    if not message.text:
        bot.send_message(message.chat.id, "❌ Я понимаю только текст. Пожалуйста, введите текстовое сообщение.")
        return
    if message.text in [
        "Предложить время", "Мои предложения", "Текущие прогулки", "Назад",
        "Напоминания", "Очистить старые", "Помощь", "Прогулки", "Настройки"
    ] or message.text.startswith('/'):
        bot.send_message(message.chat.id, "❌ Ожидание отменено.", reply_markup=main_menu())
        return
    location = message.text.strip()
    bot.send_message(message.chat.id, "🗨️ Напишите комментарий (или '-' для пропуска):")
    bot.register_next_step_handler(
        message, ask_for_comment_after_propose,
        time_str=time_str, walk_time=walk_time, user_name=user_name, user_id=user_id, location=location
    )

def ask_for_comment_after_propose(message, time_str, walk_time, user_name, user_id, location):
    if not message.text:
        bot.send_message(message.chat.id, "❌ Я понимаю только текст. Пожалуйста, введите текстовое сообщение.")
        return
    
    if message.text in [
        "Предложить время", "Мои предложения", "Текущие прогулки", "Назад",
        "Напоминания", "Очистить старые", "Помощь", "Прогулки", "Настройки"
    ] or message.text.startswith('/'):
        bot.send_message(message.chat.id, "❌ Ожидание отменено.", reply_markup=main_menu())
        return
    comment = message.text.strip()
    if comment in [".", "-", ""]:
        comment = ""
    proposal_id = add_proposal(user_id, user_name, time_str, walk_time, location, comment)
    increment_proposal_count(user_id)
    bot.reply_to(
        message,
        f"✅ Предложение на {walk_time.strftime('%d.%m в %H:%M')}\n"
        f"📍 Место: {location}\n"
        f"💬 Комментарий: {comment or '—'}\n"
        f"Отправлено всем!"
    )
    update_all_messages_with_details(proposal_id, user_name, time_str, location, comment)

def process_comment_input(message, proposal_id, user_id, user_name):
    if not message.text:
        bot.send_message(message.chat.id, "❌ Я понимаю только текст. Пожалуйста, введите текстовое сообщение.")
        return

    if message.text in [
        "Предложить время", "Мои предложения", "Текущие прогулки", "Назад",
        "Напоминания", "Очистить старые", "Помощь", "Прогулки", "Настройки"
    ] or message.text.startswith('/'):
        bot.send_message(message.chat.id, "❌ Ввод комментария отменён.", reply_markup=main_menu())
        return
    comment = message.text.strip()
    if comment == "-" or len(comment) <= 1:
        comment = ""
    if comment:
        save_comment(proposal_id, user_id, user_name, comment)
    author_info = get_proposal_author(proposal_id)
    if author_info:
        _, proposer_name, time_str, _, location, base_comment = author_info
        update_all_messages_with_details(proposal_id, proposer_name, time_str, location, base_comment)

# === ОБРАБОТЧИКИ МЕНЮ ===

@bot.message_handler(func=lambda m: m.text == "Назад")
@allowed_only
def handle_back(message):
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "Прогулки")
@allowed_only
def handle_walks_menu(message):
    bot.send_message(message.chat.id, "Выберите действие:", reply_markup=walks_menu())

@bot.message_handler(func=lambda m: m.text == "Настройки")
@allowed_only
def handle_settings_menu(message):
    bot.send_message(message.chat.id, "Настройки:", reply_markup=settings_menu())

@bot.message_handler(func=lambda m: m.text == "Предложить время")
@allowed_only
def handle_propose_button(message):
    bot.send_message(
        message.chat.id,
        "🕗 Напишите время в формате:\n"
        "• ЧЧ:ММ (например, 18:30) — сегодня/завтра\n"
        "• ГГГГ-ММ-ДД ЧЧ:ММ (например, 2025-06-15 18:30) — на дату"
    )
    bot.register_next_step_handler(message, process_time_input_from_button)

@bot.message_handler(func=lambda m: m.text == "Мои предложения")
@allowed_only
def handle_my_proposals_button(message):
    my_proposals(message)

@bot.message_handler(func=lambda m: m.text == "Текущие прогулки")
@allowed_only
def show_current_walks(message):
    proposals = get_current_proposals()
    if not proposals:
        bot.reply_to(message, "🕗 Нет активных предложений на ближайшее время.")
        return
    for pid, proposer_name, time_str, walk_dt_str, location, comment in proposals:
        walk_dt = datetime.strptime(walk_dt_str, '%Y-%m-%d %H:%M:%S')
        date_word = format_walk_date(walk_dt)
        msg_text = f"📅 {time_str}, {date_word}"
        if location:
            msg_text += f"\n📍 {location}"
        if comment:
            msg_text += f"\n💬 {comment}"
        msg_text += f"\n👤 Автор: {proposer_name}"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🗳️ Проголосовать", callback_data=f"resend_proposal_{pid}"))
        bot.send_message(message.chat.id, msg_text, reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "Напоминания")
@allowed_only
def handle_reminder_button(message):
    set_reminder(message)

@bot.message_handler(func=lambda m: m.text == "Очистить старые")
@allowed_only
def handle_cleanup_old(message):
    cleanup_old_proposals()
    auto_delete_old_proposals_by_walk_time()
    bot.reply_to(message, "✅ Старые записи очищены.")

@bot.message_handler(func=lambda m: m.text == "Помощь")
@allowed_only
def handle_help_button(message):
    help_cmd(message)

# === КОМАНДЫ ===

@bot.message_handler(commands=['start'])
@allowed_only
def start(message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "Друг"
    username = message.from_user.username
    add_user(user_id, first_name, username)
    bot.reply_to(
        message,
        "Привет! 🌤️ Ты в списке для прогулок.\n"
        "👉 Используй меню:\n"
        "— Прогулки — предлагать/просматривать\n"
        "— Настройки — напоминания, очистка\n"
        "— Помощь — справка",
        parse_mode='HTML',
        reply_markup=main_menu()
    )

@bot.message_handler(commands=['help'])
@allowed_only
def help_cmd(message):
    help_text = (
        "🧠 <b>Доступные команды:</b>\n"
        "• <b>/start</b> — открыть главное меню\n"
        "• <b>/propose ЧЧ:ММ</b> — сегодня/завтра\n"
        "• <b>/propose ГГГГ-ММ-ДД ЧЧ:ММ</b> — на дату\n"
        "• <b>/my_proposals</b> — ваши предложения\n"
        "• <b>/edit</b> — изменить последнее\n"
        "• <b>/reminder</b> — настроить напоминания\n"
        "• <b>/help</b> — эта справка\n\n"
        "💡 Используйте кнопки внизу."
    )
    bot.send_message(message.chat.id, help_text, parse_mode='HTML', reply_markup=main_menu())

@bot.message_handler(commands=['reminder'])
@allowed_only
def set_reminder(message):
    bot.send_message(
        message.chat.id,
        "🔔 <b>Настройка напоминаний</b>\n"
        "Отправьте число от <b>5 до 120</b> — за сколько минут до прогулки\n"
        "бот напомнит вам лично.\n\n"
        "Например: <code>30</code> → за 30 минут.",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(message, process_reminder_input)

def process_reminder_input(message):
    if not message.text:
        bot.send_message(message.chat.id, "❌ Я понимаю только текст. Пожалуйста, введите текстовое сообщение.")
        return
    try:
        mins = int(message.text.strip())
        if 5 <= mins <= 120:
            set_reminder_minutes(message.from_user.id, mins)
            bot.reply_to(message, f"✅ Напоминание будет приходить за {mins} минут до прогулки.")
        else:
            bot.reply_to(message, "❌ Укажите число от 5 до 120.")
    except ValueError:
        bot.reply_to(message, "❌ Введите число (например, 30).")

@bot.message_handler(commands=['my_proposals'])
@allowed_only
def my_proposals(message):
    user_id = message.from_user.id
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.time_str, p.walk_datetime, p.location, p.comment
            FROM proposals p
            WHERE p.proposer_id = ?
            ORDER BY p.walk_datetime DESC
        """, (user_id,))
        proposals = cursor.fetchall()
    if not proposals:
        bot.reply_to(message, "🕗 У вас пока нет активных предложений.")
        return
    full_response = "📁 Ваши предложения:\n"
    now = datetime.now()
    for pid, time_str, walk_dt_str, location, comment in proposals:
        walk_dt = datetime.strptime(walk_dt_str, '%Y-%m-%d %H:%M:%S')
        date_str = format_walk_date(walk_dt)
        full_time_display = f"{time_str}, {date_str}"
        votes = get_votes(pid)
        user_comments = get_comments(pid)

        def format_name_with_comment(name):
            comment = user_comments.get(name, "")
            return f"{name} — {comment}" if comment else name

        yes_list = [format_name_with_comment(name) for name in votes['yes']]
        later_list = [format_name_with_comment(name) for name in votes['later']]
        no_list = votes['no']

        proposal_text = f"📅 <b>{full_time_display}</b>\n"
        if location:
            proposal_text += f"📍 <b>Место:</b> {location}\n"
        if comment:
            proposal_text += f"💬 <b>От вас:</b> {comment}\n"
        proposal_text += "\n"
        proposal_text += f"✅ <b>Идут сейчас:</b> ({len(yes_list)})\n"
        proposal_text += "\n".join([f"• {name}" for name in yes_list]) if yes_list else "Пока никто"
        proposal_text += "\n"
        proposal_text += f"🕗 <b>Выйдут позже:</b> ({len(later_list)})\n"
        proposal_text += "\n".join([f"• {name}" for name in later_list]) if later_list else "Никто не отметил"
        proposal_text += "\n"
        proposal_text += f"❌ <b>Не пойдут:</b> ({len(no_list)})\n"
        proposal_text += "\n".join([f"• {name}" for name in no_list]) if no_list else "Все ещё в раздумьях"
        full_response += proposal_text + "\n" + ("—" * 30) + "\n"

    if len(full_response) > 4000:
        full_response = full_response[:4000] + "\n... (обрезано)"
    bot.reply_to(message, full_response, parse_mode='HTML')

@bot.message_handler(commands=['propose'])
@allowed_only
def propose(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(
            message,
            "📅 Форматы:\n• <b>/propose 18:30</b>\n• <b>/propose 2025-06-15 18:30</b>",
            parse_mode='HTML'
        )
        return
    time_str = args[1].strip()
    if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', time_str):
        bot.reply_to(message, "Формат: ЧЧ:ММ (например, 18:30)")
        return
    user_id = message.from_user.id
    if not can_propose(user_id):
        bot.reply_to(message, "❌ Лимит исчерпан: 3 раза в день.")
        return
    walk_time = parse_proposal_datetime(time_str)
    if walk_time is None:
        bot.reply_to(message, "❌ Не удалось распознать время.")
        return
    if walk_time <= datetime.now():
        bot.reply_to(message, "❌ Время уже прошло.")
        return
    user_name = message.from_user.first_name or message.from_user.username or "Аноним"
    bot.reply_to(message, "📍 Укажите место встречи:")
    bot.register_next_step_handler(
        message,
        lambda msg: ask_for_location_after_propose(msg, time_str, walk_time, user_name, user_id),
        time_str=time_str, walk_time=walk_time, user_name=user_name, user_id=user_id
    )

@bot.message_handler(commands=['edit'])
@allowed_only
def edit_proposal(message):
    user_id = message.from_user.id
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.time_str, p.location, p.comment
            FROM proposals p
            LEFT JOIN votes v ON p.id = v.proposal_id
            WHERE p.proposer_id = ? 
            AND p.walk_datetime > ?
            AND p.editable = 1
            GROUP BY p.id
            HAVING COUNT(v.proposal_id) = 0
            ORDER BY p.timestamp DESC
            LIMIT 1
        """, (user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        prop = cursor.fetchone()
    if not prop:
        bot.reply_to(message, "Нет предложений для редактирования (либо уже есть голоса).")
        return
    pid, time_str, location, comment = prop
    bot.send_message(message.chat.id, f"Редактируем предложение на {time_str}.\nНовое время (ЧЧ:ММ):")
    bot.register_next_step_handler(
        message, 
        process_edit_time, 
        proposal_id=pid, 
        old_location=location, 
        old_comment=comment
    )

def process_edit_time(message, proposal_id, old_location, old_comment):
    time_str = message.text.strip()
    if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', time_str):
        bot.reply_to(message, "Неверный формат времени. Попробуйте снова:")
        bot.register_next_step_handler(message, process_edit_time, proposal_id, old_location, old_comment)
        return
    walk_time = parse_proposal_datetime(time_str)
    if not walk_time or walk_time <= datetime.now():
        bot.reply_to(message, "Укажите время в будущем.")
        return
    bot.send_message(message.chat.id, f"Новое место (было: {old_location or '—'}):")
    bot.register_next_step_handler(
        message, 
        process_edit_location, 
        proposal_id=proposal_id,
        new_time=walk_time,
        new_time_str=time_str,
        old_comment=old_comment
    )

def process_edit_location(message, proposal_id, new_time, new_time_str, old_comment):
    location = message.text.strip()
    bot.send_message(message.chat.id, f"Новый комментарий (был: {old_comment or '—'}):")
    bot.register_next_step_handler(
        message,
        process_edit_comment,
        proposal_id=proposal_id,
        new_time=new_time,
        new_time_str=new_time_str,
        new_location=location
    )

def process_edit_comment(message, proposal_id, new_time, new_time_str, new_location):
    comment = message.text.strip()
    if comment in [".", "-", ""]:
        comment = ""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE proposals 
            SET time_str = ?, walk_datetime = ?, location = ?, comment = ?
            WHERE id = ?
        """, (new_time_str, new_time.strftime('%Y-%m-%d %H:%M:%S'), new_location, comment, proposal_id))
    bot.send_message(message.chat.id, "✅ Предложение обновлено!", reply_markup=main_menu())
    author_info = get_proposal_author(proposal_id)
    if author_info:
        _, proposer_name, _, _, loc, comm = author_info
        update_all_messages_with_details(proposal_id, proposer_name, new_time_str, loc, comm)

# === CALLBACK-ОБРАБОТЧИКИ ===

@bot.callback_query_handler(func=lambda call: call.data.startswith("vote_"))
def handle_vote(call):
    if not check_allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "🔒 Доступ запрещён.", show_alert=True)
        return
    parts = call.data.split("_")
    if len(parts) < 3:
        return
    vote_type, proposal_id = parts[1], int(parts[2])
    if vote_type not in ('yes', 'later', 'no'):
        vote_type = 'yes'
    voter_id = call.from_user.id
    voter_name = call.from_user.first_name or call.from_user.username or "Аноним"
    add_vote(proposal_id, voter_id, voter_name, vote_type)

    if vote_type == 'yes':
        votes = get_votes(proposal_id)
        current_count = len(votes['yes'])
        if current_count >= 3:
            author_info = get_proposal_author(proposal_id)
            if author_info:
                _, proposer_name, time_str, walk_dt_str, location, base_comment = author_info
                walk_dt = datetime.strptime(walk_dt_str, '%Y-%m-%d %H:%M:%S')
                date_word = format_walk_date(walk_dt)
                confirm_msg = (
                    f"✅ <b>Прогулка подтверждена!</b>\n"
                    f"📅 {time_str}, {date_word}\n"
                )
                if location:
                    confirm_msg += f"📍 {location}\n"
                confirm_msg += f"\n👥 Участники:\n" + "\n".join(f"• {name}" for name in votes['yes'])
                with sqlite3.connect(DB_PATH) as conn:
                    c = conn.cursor()
                    c.execute("SELECT voter_id FROM votes WHERE proposal_id = ? AND vote_type = 'yes'", (proposal_id,))
                    for (voter_id_to_notify,) in c.fetchall():
                        try:
                            bot.send_message(voter_id_to_notify, confirm_msg, parse_mode='HTML')
                        except Exception as e:
                            print(f"Ошибка отправки {voter_id_to_notify}: {e}")

    if vote_type in ('yes', 'later'):
        bot.send_message(
            call.message.chat.id,
            "🗨️ Хотите оставить комментарий? (Например: «С собакой»)\n"
            "Если не хотите — отправьте «-»."
        )
        bot.register_next_step_handler(
            call.message,
            process_comment_input,
            proposal_id=proposal_id,
            user_id=voter_id,
            user_name=voter_name
        )
    else:
        author_info = get_proposal_author(proposal_id)
        if author_info:
            _, proposer_name, time_str, _, location, comment = author_info
            update_all_messages_with_details(proposal_id, proposer_name, time_str, location, comment)

    msg = {
        'yes': "Отлично! Ты в списке «Выйду гулять» 👍",
        'later': "Хорошо! Отметил как «Выйду позже» ⏳",
        'no': "Понял. Ты в списке «Не пойду» ❌"
    }
    bot.answer_callback_query(call.id, msg[vote_type])

@bot.callback_query_handler(func=lambda call: call.data.startswith("resend_proposal_"))
def handle_resend_proposal(call):
    if not check_allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "🔒 Доступ запрещён.", show_alert=True)
        return
    proposal_id = int(call.data.split("_")[2])
    author_info = get_proposal_author(proposal_id)
    if not author_info:
        bot.answer_callback_query(call.id, "❌ Предложение не найдено.")
        return
    user_id = call.from_user.id
    _, proposer_name, time_str, _, location, base_comment = author_info
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT walk_datetime FROM proposals WHERE id = ?", (proposal_id,))
        row = cursor.fetchone()
        if not row:
            bot.answer_callback_query(call.id, "❌ Предложение удалено.")
            return
        walk_dt_str = row[0]

    walk_datetime = datetime.strptime(walk_dt_str, '%Y-%m-%d %H:%M:%S')
    date_str = format_walk_date(walk_datetime)
    full_time_display = f"{time_str}, {date_str}"
    votes = get_votes(proposal_id)
    user_comments = get_comments(proposal_id)

    def format_name_with_comment(name):
        comment = user_comments.get(name, "")
        return f"{name} — {comment}" if comment else name

    yes_list = "\n".join([f"• {format_name_with_comment(name)}" for name in votes['yes']]) or "Пока никто"
    later_list = "\n".join([f"• {format_name_with_comment(name)}" for name in votes['later']]) or "Никто не отметил"
    no_list = "\n".join([f"• {name}" for name in votes['no']]) or "Все ещё в раздумьях"

    text = f"📅 <b>Прогулка: {full_time_display}</b>\n"
    if location:
        text += f"📍 <b>Место:</b> {location}\n"
    if base_comment:
        text += f"💬 <b>От автора:</b> {base_comment}\n"
    text += f"\nОт: {proposer_name}\n"
    text += f"✅ <b>Выйду гулять:</b>\n{yes_list}\n"
    text += f"🕗 <b>Выйду позже:</b>\n{later_list}\n"
    text += f"❌ <b>Не пойду:</b>\n{no_list}"

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Выйду гулять", callback_data=f"vote_yes_{proposal_id}"),
        types.InlineKeyboardButton("🕗 Выйду позже", callback_data=f"vote_later_{proposal_id}")
    )
    markup.add(
        types.InlineKeyboardButton("❌ Не пойду", callback_data=f"vote_no_{proposal_id}")
    )

    try:
        sent = bot.send_message(user_id, text, reply_markup=markup, parse_mode='HTML')
        save_message_id(user_id, proposal_id, sent.message_id)
        bot.answer_callback_query(call.id, "✅ Сообщение с голосованием отправлено вам в личку!")
    except Exception as e:
        print(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
        bot.answer_callback_query(call.id, "❌ Не удалось отправить сообщение. Возможно, вы заблокировали бота.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_going_"))
def handle_confirm_going(call):
    if not check_allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "🔒 Доступ запрещён.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Отлично! Хорошей прогулки! 🌤️")

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_last_min_"))
def handle_cancel_last_minute(call):
    if not check_allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "🔒 Доступ запрещён.", show_alert=True)
        return
    proposal_id = int(call.data.split("_")[3])
    message_records = get_all_message_ids_for_proposal(proposal_id)
    for user_id, msg_id in message_records:
        try:
            bot.edit_message_text(
                chat_id=user_id,
                message_id=msg_id,
                text="❌ Прогулка отменена автором в последнюю минуту.",
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"Не удалось обновить сообщение у {user_id}: {e}")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM proposals WHERE id = ?", (proposal_id,))
        cursor.execute("DELETE FROM user_proposal_messages WHERE proposal_id = ?", (proposal_id,))
    bot.answer_callback_query(call.id, "Прогулка отменена.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("remind_later_"))
def handle_remind_later(call):
    if not check_allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "🔒 Доступ запрещён.", show_alert=True)
        return
    proposal_id = int(call.data.split("_")[2])
    new_time = datetime.now() - timedelta(hours=5)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE proposals SET timestamp = ?, processed = 0 WHERE id = ?",
            (new_time.strftime('%Y-%m-%d %H:%M:%S'), proposal_id)
        )
    bot.answer_callback_query(call.id, "Хорошо! Напомню через 1 час.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_proposal_"))
def handle_cancel_proposal(call):
    if not check_allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "🔒 Доступ запрещён.", show_alert=True)
        return
    proposal_id = int(call.data.split("_")[2])
    message_records = get_all_message_ids_for_proposal(proposal_id)
    for user_id, msg_id in message_records:
        try:
            bot.edit_message_text(
                chat_id=user_id,
                message_id=msg_id,
                text="❌ Это предложение было отменено автором.",
                reply_markup=None,
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"⚠️ Не удалось обновить сообщение у {user_id}: {e}")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM proposals WHERE id = ?", (proposal_id,))
        cursor.execute("DELETE FROM user_proposal_messages WHERE proposal_id = ?", (proposal_id,))
    bot.answer_callback_query(call.id, "Предложение отменено.", show_alert=True)

# === ФОНОВЫЙ ПОТОК ===

def background_worker():
    while True:
        try:
            now = datetime.now()
            two_hours_ago = now - timedelta(hours=2)
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT p.id, p.proposer_id, p.time_str, p.walk_datetime, COALESCE(s.reminder_minutes, 10) AS rem_mins
                    FROM proposals p
                    LEFT JOIN user_settings s ON p.proposer_id = s.user_id
                    WHERE p.walk_datetime > ? AND p.processed = 0
                """, (now.strftime('%Y-%m-%d %H:%M:%S'),))
                all_proposals = cursor.fetchall()
                for pid, proposer_id, time_str, walk_dt_str, rem_mins in all_proposals:
                    walk_dt = datetime.strptime(walk_dt_str, '%Y-%m-%d %H:%M:%S')
                    remind_time = walk_dt - timedelta(minutes=rem_mins)
                    if now <= remind_time < now + timedelta(seconds=REMINDER_CHECK_INTERVAL + 1):
                        cursor.execute("SELECT COUNT(*) FROM votes WHERE proposal_id = ? AND vote_type = 'yes'", (pid,))
                        going_count = cursor.fetchone()[0]
                        if going_count > 0:
                            try:
                                markup = types.InlineKeyboardMarkup()
                                markup.add(types.InlineKeyboardButton("✅ Уже выхожу", callback_data=f"confirm_going_{pid}"))
                                markup.add(types.InlineKeyboardButton("❌ Не получится", callback_data=f"cancel_last_min_{pid}"))
                                bot.send_message(
                                    proposer_id,
                                    f"⏰ Через {rem_mins} минут начинается прогулка на {time_str}!\n"
                                    f"Идёшь? Участников: {going_count}",
                                    reply_markup=markup
                                )
                                cursor.execute("UPDATE proposals SET processed = 1 WHERE id = ?", (pid,))
                            except Exception as e:
                                print(f"❌ Ошибка отправки напоминания автору {proposer_id}: {e}")

                cursor.execute("""
                    SELECT id, proposer_id, proposer_name, time_str, walk_datetime
                    FROM proposals
                    WHERE walk_datetime <= ? AND processed = 0
                """, (two_hours_ago.strftime('%Y-%m-%d %H:%M:%S'),))
                candidates = cursor.fetchall()
                for pid, proposer_id, proposer_name, time_str, _ in candidates:
                    cursor.execute("SELECT COUNT(*) FROM votes WHERE proposal_id = ? AND vote_type = 'yes'", (pid,))
                    yes_votes = cursor.fetchone()[0]
                    if yes_votes == 0:
                        try:
                            markup = types.InlineKeyboardMarkup()
                            markup.add(types.InlineKeyboardButton("🕒 Напомнить через 1 час", callback_data=f"remind_later_{pid}"))
                            markup.add(types.InlineKeyboardButton("🗑️ Отменить", callback_data=f"cancel_proposal_{pid}"))
                            bot.send_message(
                                proposer_id,
                                f"🕗 Никто не откликнулся на прогулку на {time_str}.\nЧто делаем?",
                                reply_markup=markup
                            )
                            cursor.execute("UPDATE proposals SET processed = 1 WHERE id = ?", (pid,))
                        except Exception as e:
                            print(f"❌ Не удалось отправить уведомление автору {proposer_id}: {e}")

            auto_delete_old_proposals_by_walk_time()
            cleanup_old_proposals()
            time.sleep(REMINDER_CHECK_INTERVAL)
        except Exception as e:
            print(f"🔥 Ошибка в фоновом потоке: {e}")
            time.sleep(REMINDER_CHECK_INTERVAL)

# === ЗАПУСК ===

if __name__ == '__main__':
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(proposals)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'walk_datetime' not in columns:
            print("🔧 Добавляю walk_datetime...")
            cursor.execute("ALTER TABLE proposals ADD COLUMN walk_datetime DATETIME NOT NULL DEFAULT '2025-01-01 00:00:00'")
        if 'editable' not in columns:
            print("🔧 Добавляю editable...")
            cursor.execute("ALTER TABLE proposals ADD COLUMN editable BOOLEAN DEFAULT 1")
        conn.commit()
        if 'walk_datetime' not in columns:
            cursor.execute("SELECT id, time_str, timestamp FROM proposals WHERE walk_datetime = '2025-01-01 00:00:00'")
            old_records = cursor.fetchall()
            for pid, time_str, ts_str in old_records:
                try:
                    ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                    proposed_time = datetime.strptime(time_str, "%H:%M").replace(
                        year=ts.year, month=ts.month, day=ts.day
                    )
                    if proposed_time <= ts:
                        proposed_time += timedelta(days=1)
                    walk_dt_str = proposed_time.strftime('%Y-%m-%d %H:%M:%S')
                    cursor.execute("UPDATE proposals SET walk_datetime = ? WHERE id = ?", (walk_dt_str, pid))
                except Exception as e:
                    print(f"⚠️ Не удалось исправить запись {pid}: {e}")
            conn.commit()
            print(f"✅ Исправлено {len(old_records)} записей.")

    threading.Thread(target=background_worker, daemon=True).start()
    privacy_status = "🔒 Приватный" if ALLOWED_USER_IDS else "🌐 Публичный"
    print(f"✅ Бот запущен. Режим: {privacy_status}")
    if ALLOWED_USER_IDS:
        print(f"   Разрешённые user_id: {sorted(ALLOWED_USER_IDS)}")
    bot.infinity_polling(timeout=10, long_polling_timeout=5, skip_pending=True)