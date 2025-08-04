import logging
import sqlite3
import json
from functools import wraps
from pathlib import Path
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)

# --- KONFIGURASI ---
BOT_TOKEN = "7872111732"  # Ganti dengan token bot Anda
DB_FILE = Path("bot_database.db")
OWNER_ID = 5361605327  # Ganti dengan ID Telegram Anda

# --- LOGGING ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- STATE UNTUK CONVERSATION HANDLER ---
(GENDER, AGE, BIO, FIND_GENDER_PREF) = range(4)

# --- VARIABEL GLOBAL UNTUK STATE APLIKASI ---
# DIPERBAIKI: Variabel ini harus berada di global scope, bukan di dalam fungsi main().
chat_partners = {}
waiting_queue = []
# user_states akan menyimpan status pengguna: 'chatting', 'waiting', atau 'idle' (atau tidak ada jika idle)
user_states = {}


# --- DECORATOR & FUNGSI DATABASE ---
def owner_only(func):
    """Decorator untuk membatasi akses perintah hanya untuk Owner."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text("Maaf, perintah ini hanya untuk Owner.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def db_query(query, params=()):
    """Fungsi helper untuk menjalankan query ke database SQLite."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        return cursor.fetchall()

def setup_database():
    """Membuat tabel database jika belum ada."""
    db_query("CREATE TABLE IF NOT EXISTS user_profiles (user_id INTEGER PRIMARY KEY, username TEXT, gender TEXT, age INTEGER, bio TEXT, language TEXT DEFAULT 'id', is_pro INTEGER DEFAULT 0)")
    db_query("CREATE TABLE IF NOT EXISTS reports (report_id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER NOT NULL, reported_id INTEGER NOT NULL, timestamp DATETIME NOT NULL)")
    db_query("CREATE TABLE IF NOT EXISTS chat_data (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db_query("INSERT OR IGNORE INTO chat_data (key, value) VALUES ('chat_partners', '{}')")
    db_query("INSERT OR IGNORE INTO chat_data (key, value) VALUES ('waiting_queue', '[]')")
    logger.info(f"Database '{DB_FILE}' siap digunakan.")

def get_user_profile(user_id):
    """Mengambil profil pengguna dari database. Hanya mengembalikan jika profil sudah lengkap."""
    result = db_query("SELECT user_id, username, gender, age, bio, language, is_pro FROM user_profiles WHERE user_id = ?", (user_id,))
    if result:
        user = result[0]
        # Hanya kembalikan profil jika data inti (gender, age, bio) sudah diisi.
        if user[2] and user[3] and user[4]:
            return {"user_id": user[0], "username": user[1], "gender": user[2], "age": user[3], "bio": user[4], "language": user[5], "is_pro": bool(user[6])}
    return None # Jika data tidak lengkap atau tidak ada, anggap sebagai profil kosong.

def find_user_by_username(username):
    """Mencari user_id berdasarkan username."""
    clean_username = username.lstrip('@')
    result = db_query("SELECT user_id FROM user_profiles WHERE username = ?", (clean_username,))
    if result: return result[0][0]
    return None

def update_user_profile(user_id, username, data={}):
    """Membuat atau memperbarui profil pengguna."""
    profile = get_user_profile(user_id) or {"user_id": user_id, "gender": None, "age": None, "bio": None, "language": "id", "is_pro": False}
    if username: profile['username'] = username
    profile.update(data)
    db_query(
        "INSERT INTO user_profiles (user_id, username, gender, age, bio, language, is_pro) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, gender=excluded.gender, age=excluded.age, bio=excluded.bio, language=excluded.language, is_pro=excluded.is_pro",
        (profile["user_id"], profile["username"], profile["gender"], profile["age"], profile["bio"], profile["language"], int(profile["is_pro"]))
    )

def load_chat_data():
    """Memuat state chat dari database saat bot restart."""
    result = db_query("SELECT key, value FROM chat_data")
    data = {key: json.loads(value) for key, value in result}
    # Konversi key dari string (JSON) ke integer (Python dict)
    partners = {int(k): v for k, v in data.get('chat_partners', {}).items()}
    waiting_queue = data.get('waiting_queue', [])
    return partners, waiting_queue

def save_chat_data():
    """Menyimpan state chat saat ini ke database."""
    global chat_partners, waiting_queue
    # Pastikan key di-dump sebagai string untuk JSON
    partners_to_save = {str(k): v for k, v in chat_partners.items()}
    db_query("UPDATE chat_data SET value = ? WHERE key = 'chat_partners'", (json.dumps(partners_to_save),))
    db_query("UPDATE chat_data SET value = ? WHERE key = 'waiting_queue'", (json.dumps(waiting_queue),))

def auto_update_profile(func):
    """Decorator untuk memastikan profil dasar pengguna selalu ada dan username-nya terbaru."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user:
            user_id = update.effective_user.id
            username = update.effective_user.username
            # Hanya membuat entri baru jika belum ada sama sekali
            db_query("INSERT OR IGNORE INTO user_profiles (user_id, username) VALUES (?, ?)", (user_id, username))
            # Selalu update username jika berubah (dan jika user punya username)
            if username:
                db_query("UPDATE user_profiles SET username = ? WHERE user_id = ?", (username, user_id))
        return await func(update, context, *args, **kwargs)
    return wrapped


# --- PERINTAH OWNER & STATS ---
@owner_only
@auto_update_profile
async def grant_pro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memberikan status Pro kepada pengguna."""
    try:
        target_user_id = None
        if update.message.reply_to_message:
            target_user_id = update.message.reply_to_message.from_user.id
        else:
            arg = context.args[0]
            if arg.startswith('@'):
                target_user_id = find_user_by_username(arg)
                if not target_user_id:
                    await update.message.reply_text(f"User dengan username {arg} tidak ditemukan di database bot.")
                    return
            else:
                target_user_id = int(arg)

        if target_user_id:
            profile = get_user_profile(target_user_id)
            update_user_profile(target_user_id, profile.get('username') if profile else None, {"is_pro": True})
            await update.message.reply_text(f"✅ Berhasil! User ID {target_user_id} sekarang adalah Pro.")
            try:
                await context.bot.send_message(chat_id=target_user_id, text="✨ Selamat! Akun Anda telah di-upgrade ke versi Pro oleh Owner.")
            except Exception as e:
                logger.warning(f"Gagal mengirim notifikasi Pro ke user {target_user_id}: {e}")
    except (IndexError, ValueError):
        await update.message.reply_text("Penggunaan:\n• `/grant_pro @username`\n• `/grant_pro [USER_ID]`\n• Reply pesan user dengan `/grant_pro`")

@auto_update_profile
async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan ID Telegram pengguna."""
    user_id = update.effective_user.id
    await update.message.reply_text(f"ID Telegram Anda adalah:\n`{user_id}`\n\n(Klik untuk menyalin)", parse_mode='Markdown')

@owner_only
@auto_update_profile
async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan statistik detail untuk Owner."""
    global chat_partners, waiting_queue
    total_users = db_query("SELECT COUNT(*) FROM user_profiles")[0][0]
    pro_users = db_query("SELECT COUNT(*) FROM user_profiles WHERE is_pro = 1")[0][0]
    total_reports = db_query("SELECT COUNT(*) FROM reports")[0][0]
    users_in_chat = len(chat_partners)
    users_waiting = len(waiting_queue)
    stats_message = (
        f"📊 **Statistik Admin**\n\n"
        f"👤 Total Pengguna: **{total_users}**\n"
        f"⭐ Pengguna Pro: **{pro_users}**\n"
        f"💬 Sedang Chat: **{users_in_chat}** pengguna\n"
        f"⏳ Dalam Antrian: **{users_waiting}** pengguna\n"
        f"🚩 Total Laporan: **{total_reports}**"
    )
    await update.message.reply_text(stats_message, parse_mode='Markdown')

@auto_update_profile
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan statistik publik."""
    global chat_partners, waiting_queue
    total_users = db_query("SELECT COUNT(*) FROM user_profiles")[0][0]
    active_users = len(chat_partners) + len(waiting_queue)
    stats_message = (
        f"📈 **Statistik Saat Ini**\n\n"
        f"👥 Total Pengguna Terdaftar: **{total_users}**\n"
        f"🟢 Pengguna Aktif Saat Ini: **{active_users}**"
    )
    await update.message.reply_text(stats_message, parse_mode='Markdown')


# --- ALUR PROFIL & START ---
@auto_update_profile
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memulai bot dan memeriksa profil."""
    user_id = update.message.from_user.id
    profile = get_user_profile(user_id)
    if not profile:
        keyboard = [
            [InlineKeyboardButton("Lengkapi Profil 📝", callback_data="start_setup_profile")],
            [InlineKeyboardButton("Lanjutkan & Cari Acak ➡️", callback_data="start_random_search")]
        ]
        await update.message.reply_text(
            "👋 Selamat datang! Agar pengalaman chat lebih baik, kamu bisa melengkapi profil singkat. "
            "Atau, kamu bisa langsung mencari pasangan secara acak.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            "Selamat datang kembali! Gunakan /search untuk mencari pasangan acak.\n\n"
            "Untuk mencari berdasarkan gender, upgrade akunmu ke Pro lalu gunakan /find_by_gender."
        )

async def start_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani pilihan pengguna dari pesan /start."""
    query = update.callback_query
    await query.answer()
    if query.data == 'start_setup_profile':
        await query.message.delete()
        # Memanggil fungsi awal dari ConversationHandler
        await profile_command(update, context)
    elif query.data == 'start_random_search':
        await query.edit_message_text("Baik, mari kita cari pasangan secara acak untukmu!")
        await add_to_queue(update, context, preference="any")

@auto_update_profile
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Memulai alur pengaturan profil."""
    # update bisa berupa message atau callback_query
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id=chat_id, text="Mari kita atur profilmu! Profil ini akan ditampilkan ke pasangan chatmu.")
    keyboard = [
        [InlineKeyboardButton("Pria", callback_data="Pria"), InlineKeyboardButton("Wanita", callback_data="Wanita")],
        [InlineKeyboardButton("Rahasia", callback_data="Rahasia")]
    ]
    await context.bot.send_message(chat_id=chat_id, text="Pilih gendermu:", reply_markup=InlineKeyboardMarkup(keyboard))
    return GENDER

async def gender_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menerima input gender dan meminta usia."""
    query = update.callback_query
    await query.answer()
    context.user_data['gender'] = query.data
    await query.edit_message_text(text=f"Gender dipilih: {query.data}")
    await context.bot.send_message(chat_id=query.from_user.id, text="Sekarang, berapa usiamu? (Kirim angka saja)")
    return AGE

async def age_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menerima input usia dan meminta bio."""
    try:
        age = int(update.message.text)
        if not 13 <= age <= 100:
            await update.message.reply_text("Umur tidak valid. Masukkan umur antara 13 dan 100.")
            return AGE
        context.user_data['age'] = age
        await update.message.reply_text("Terakhir, tulis bio singkat tentang dirimu (maksimal 150 karakter).")
        return BIO
    except ValueError:
        await update.message.reply_text("Harap masukkan angka yang valid untuk umur.")
        return AGE

async def bio_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menerima input bio, menyimpan profil, dan mengakhiri percakapan."""
    bio = update.message.text
    if len(bio) > 150:
        await update.message.reply_text("Bio terlalu panjang (maksimal 150 karakter). Coba lagi.")
        return BIO
    context.user_data['bio'] = bio
    update_user_profile(
        update.effective_user.id,
        update.effective_user.username,
        {
            "gender": context.user_data.get('gender'),
            "age": context.user_data.get('age'),
            "bio": context.user_data.get('bio')
        }
    )
    await update.message.reply_text("Profilmu berhasil disimpan! Sekarang kamu bisa mencari pasangan dengan /search.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Membatalkan proses pembuatan profil."""
    await update.message.reply_text("Pengaturan profil dibatalkan.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END


# --- LOGIKA CHAT ---
def is_user_busy(user_id):
    """Memeriksa apakah pengguna sedang dalam antrian atau chat."""
    global chat_partners, waiting_queue
    if user_id in chat_partners: return True
    if any(user['user_id'] == user_id for user in waiting_queue): return True
    return False

async def add_to_queue(update: Update, context: ContextTypes.DEFAULT_TYPE, preference: str):
    """Menambahkan pengguna ke antrian pencarian."""
    global waiting_queue, user_states
    user_id = update.effective_user.id

    if is_user_busy(user_id):
        await context.bot.send_message(chat_id=user_id, text="Kamu sudah dalam percakapan atau sedang mencari. Gunakan /stop untuk berhenti.")
        return

    profile = get_user_profile(user_id)
    # Jika profil tidak lengkap, gunakan nilai default agar tidak error
    user_gender = profile['gender'] if profile else 'Misteri'

    waiting_queue.append({"user_id": user_id, "gender": user_gender, "preference": preference})
    user_states[user_id] = "waiting"
    save_chat_data()
    await context.bot.send_message(chat_id=user_id, text="🔎 Mencari pasangan... Mohon tunggu.")
    await try_to_match_users(context)

@auto_update_profile
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memulai pencarian acak."""
    await add_to_queue(update, context, preference="any")

@auto_update_profile
async def find_by_gender_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Memulai pencarian berdasarkan gender (fitur Pro)."""
    user_id = update.message.from_user.id
    profile = get_user_profile(user_id)

    if not profile:
        await update.message.reply_text("Untuk menggunakan fitur ini, kamu harus melengkapi profilmu terlebih dahulu. Silakan gunakan /profile.")
        return ConversationHandler.END

    if not profile.get("is_pro"):
        try:
            owner = await context.bot.get_chat(OWNER_ID)
            owner_username = f"@{owner.username}" if owner.username else "Owner"
        except Exception:
            owner_username = "Owner"
        await update.message.reply_text(f"Fitur ini hanya untuk pengguna Pro.\n\nUntuk upgrade, silakan hubungi {owner_username} untuk info pembayaran.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("Pria", callback_data="Pria"), InlineKeyboardButton("Wanita", callback_data="Wanita")],
        [InlineKeyboardButton("Apapun", callback_data="any")]
    ]
    await update.message.reply_text("Pilih gender pasangan yang ingin kamu cari:", reply_markup=InlineKeyboardMarkup(keyboard))
    return FIND_GENDER_PREF

async def find_gender_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menerima preferensi gender dan memulai pencarian."""
    query = update.callback_query
    await query.answer()
    preference = query.data
    await query.edit_message_text(f"Baik, mencari pasangan dengan gender: {preference}...")
    await add_to_queue(query, context, preference=preference)
    return ConversationHandler.END

async def try_to_match_users(context: ContextTypes.DEFAULT_TYPE):
    """Mencoba mencocokkan pengguna di dalam antrian."""
    global waiting_queue, chat_partners, user_states
    if len(waiting_queue) < 2: return

    matched_users = []
    # Salin antrian agar aman saat melakukan iterasi dan penghapusan
    temp_queue = list(waiting_queue)

    for i in range(len(temp_queue)):
        for j in range(i + 1, len(temp_queue)):
            user_a, user_b = temp_queue[i], temp_queue[j]

            # Kondisi pencocokan: preferensi A cocok dengan gender B, DAN preferensi B cocok dengan gender A
            a_likes_b = user_a["preference"] == "any" or user_a["preference"] == user_b["gender"]
            b_likes_a = user_b["preference"] == "any" or user_b["preference"] == user_a["gender"]

            if a_likes_b and b_likes_a:
                matched_users.extend([user_a, user_b])
                break
        if matched_users: break

    if not matched_users: return

    user1_data, user2_data = matched_users[0], matched_users[1]

    # Hapus dari antrian utama
    waiting_queue.remove(user1_data)
    waiting_queue.remove(user2_data)

    user1_id, user2_id = user1_data["user_id"], user2_data["user_id"]
    chat_partners.update({user1_id: user2_id, user2_id: user1_id})
    user_states.update({user1_id: "chatting", user2_id: "chatting"})
    save_chat_data()

    profile1 = get_user_profile(user1_id) or {"gender": "Misteri", "age": "??", "bio": "-"}
    profile2 = get_user_profile(user2_id) or {"gender": "Misteri", "age": "??", "bio": "-"}

    profile1_msg = f"Gender: {profile1['gender']}\nUmur: {profile1['age']}\nBio: {profile1['bio']}"
    profile2_msg = f"Gender: {profile2['gender']}\nUmur: {profile2['age']}\nBio: {profile2['bio']}"

    try:
        await context.bot.send_message(user1_id, f"✅ Pasangan ditemukan!\n\nProfil pasanganmu:\n{profile2_msg}\n\nKetik /stop untuk mengakhiri, /next untuk cari lagi.")
        await context.bot.send_message(user2_id, f"✅ Pasangan ditemukan!\n\nProfil pasanganmu:\n{profile1_msg}\n\nKetik /stop untuk mengakhiri, /next untuk cari lagi.")
    except Exception as e:
        logger.error(f"Gagal mengirim pesan 'pasangan ditemukan' ke {user1_id} atau {user2_id}: {e}")

async def end_chat_session(initiator_id: int) -> int | None:
    """Mengakhiri sesi chat, membersihkan state, dan mengembalikan ID partner."""
    global chat_partners, user_states
    if initiator_id not in chat_partners: return None

    partner_id = chat_partners.pop(initiator_id)
    chat_partners.pop(partner_id, None) # Hapus juga entri partner

    user_states.pop(initiator_id, None)
    user_states.pop(partner_id, None)

    save_chat_data()
    return partner_id

async def post_chat_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani tombol setelah chat berakhir (Cari lagi / Stop)."""
    query = update.callback_query
    await query.answer()
    if query.data == 'post_chat_new_search':
        await query.edit_message_text("Baik, mencari pasangan baru untukmu...")
        await add_to_queue(query, context, preference="any")
    elif query.data == 'post_chat_stop':
        await query.edit_message_text("Sesi dihentikan. Ketik /search untuk memulai lagi kapan pun.")

async def handle_report_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani tombol laporan."""
    query = update.callback_query
    await query.answer()
    reporter_id = query.from_user.id
    try:
        reported_id = int(query.data.split('_')[1])
    except (IndexError, ValueError):
        await query.edit_message_text("Gagal memproses laporan. ID tidak valid.")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_query("INSERT INTO reports (reporter_id, reported_id, timestamp) VALUES (?, ?, ?)", (reporter_id, reported_id, timestamp))
    await query.edit_message_text("Laporan telah dikirim. Terima kasih.")

    # Notifikasi ke Owner
    reporter_profile = get_user_profile(reporter_id)
    reported_profile = get_user_profile(reported_id)
    reporter_info = f"@{reporter_profile['username']} (ID: {reporter_id})" if reporter_profile and reporter_profile.get('username') else f"ID: {reporter_id}"
    reported_info = f"@{reported_profile['username']} (ID: {reported_id})" if reported_profile and reported_profile.get('username') else f"ID: {reported_id}"

    await context.bot.send_message(
        chat_id=OWNER_ID,
        text=f"⚠️ **Laporan Baru Diterima** ⚠️\n\n**Pelapor:** {reporter_info}\n**Melaporkan:** {reported_info}",
        parse_mode='Markdown'
    )

@auto_update_profile
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menghentikan pencarian atau mengakhiri percakapan."""
    global waiting_queue
    user_id = update.message.from_user.id

    # Cek jika user ada di antrian
    user_in_queue = next((user for user in waiting_queue if user['user_id'] == user_id), None)
    if user_in_queue:
        waiting_queue.remove(user_in_queue)
        save_chat_data()
        await update.message.reply_text("Pencarian dibatalkan.")
    elif user_id in chat_partners:
        partner_id = await end_chat_session(user_id)
        if partner_id:
            await update.message.reply_text("❌ Percakapan telah berakhir.")
            
            # DIPERBAIKI: callback_data harus berisi ID partner, bukan ID sendiri.
            keyboard = [
                [InlineKeyboardButton("Cari Partner Baru 🔎", callback_data="post_chat_new_search"), InlineKeyboardButton("Stop ⏹️", callback_data="post_chat_stop")],
                [InlineKeyboardButton("🚩 Laporkan Partner Terakhir", callback_data=f"report_{partner_id}")]
            ]
            try:
                await context.bot.send_message(chat_id=partner_id, text="❌ Pasanganmu telah menghentikan percakapan.", reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as e:
                logger.warning(f"Gagal mengirim pesan 'stop' ke partner {partner_id}: {e}")
    else:
        await update.message.reply_text("Kamu sedang tidak dalam percakapan atau antrian.")

@auto_update_profile
async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengakhiri chat saat ini dan langsung mencari yang baru."""
    user_id = update.effective_user.id
    if user_id in chat_partners:
        partner_id = await end_chat_session(user_id)
        if partner_id:
            # DIPERBAIKI: callback_data harus berisi ID partner, bukan ID sendiri.
            keyboard = [
                [InlineKeyboardButton("Cari Partner Baru 🔎", callback_data="post_chat_new_search"), InlineKeyboardButton("Stop ⏹️", callback_data="post_chat_stop")],
                [InlineKeyboardButton("🚩 Laporkan Partner Terakhir", callback_data=f"report_{partner_id}")]
            ]
            try:
                await context.bot.send_message(chat_id=partner_id, text="🚶 Pasanganmu telah beralih ke chat lain.", reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as e:
                logger.warning(f"Gagal mengirim pesan 'next' ke partner {partner_id}: {e}")

        # Langsung cari lagi untuk user yang mengetik /next
        await add_to_queue(update, context, preference="any")
    else:
        # Jika tidak sedang chat, /next berfungsi seperti /search
        await add_to_queue(update, context, preference="any")

@auto_update_profile
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Meneruskan pesan antar partner chat."""
    user_id = update.message.from_user.id
    if user_states.get(user_id) != "chatting":
        await update.message.reply_text("Ketik /search atau /next untuk mulai mencari pasangan.")
        return

    partner_id = chat_partners.get(user_id)
    if not partner_id:
        # Membersihkan state jika ada ketidaksinkronan
        user_states.pop(user_id, None)
        await update.message.reply_text("Sepertinya pasanganmu sudah tidak terhubung. Ketik /search untuk mencari lagi.")
        return
    
    try:
        # Meneruskan berbagai jenis pesan
        if update.message.text:
            await context.bot.send_message(chat_id=partner_id, text=update.message.text)
        elif update.message.photo:
            await context.bot.send_photo(chat_id=partner_id, photo=update.message.photo[-1].file_id, caption=update.message.caption)
        elif update.message.sticker:
            await context.bot.send_sticker(chat_id=partner_id, sticker=update.message.sticker.file_id)
        elif update.message.voice:
            await context.bot.send_voice(chat_id=partner_id, voice=update.message.voice.file_id, caption=update.message.caption)
        elif update.message.video:
            await context.bot.send_video(chat_id=partner_id, video=update.message.video.file_id, caption=update.message.caption)
        # Tambahkan jenis pesan lain jika diperlukan (document, audio, etc.)
    except Exception as e:
        logger.error(f"Gagal meneruskan pesan dari {user_id} ke {partner_id}: {e}")
        await update.message.reply_text("Gagal mengirim pesan. Mungkin pasanganmu telah memblokir bot.")


def main():
    """Fungsi utama untuk menjalankan bot."""
    global chat_partners, waiting_queue, user_states
    
    setup_database()
    
    # Muat state terakhir dari DB saat bot dimulai
    chat_partners, waiting_queue = load_chat_data()
    # Inisialisasi user_states berdasarkan data yang dimuat
    user_states = {uid: "chatting" for uid in chat_partners.keys()}
    user_states.update({data["user_id"]: "waiting" for data in waiting_queue})
    
    logger.info(f"Bot dimulai. {len(chat_partners)} pengguna dalam chat, {len(waiting_queue)} dalam antrian.")

    application = Application.builder().token(BOT_TOKEN).build()
    
    # Handler untuk alur pembuatan profil
    profile_handler = ConversationHandler(
        entry_points=[
            CommandHandler("profile", profile_command),
            # Menangani kasus user menekan tombol "Lengkapi Profil" dari /start
            CallbackQueryHandler(profile_command, pattern='^start_setup_profile$')
        ],
        states={
            GENDER: [CallbackQueryHandler(gender_received, pattern='^(Pria|Wanita|Rahasia)$')],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_received)],
            BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, bio_received)]
        },
        fallbacks=[CommandHandler("cancel", cancel_profile)]
    )

    # Handler untuk alur pencarian berdasarkan gender
    find_by_gender_handler = ConversationHandler(
        entry_points=[CommandHandler("find_by_gender", find_by_gender_command)],
        states={
            FIND_GENDER_PREF: [CallbackQueryHandler(find_gender_received, pattern='^(Pria|Wanita|any)$')]
        },
        fallbacks=[CommandHandler("cancel", cancel_profile)]
    )

    # Tambahkan semua handler ke aplikasi
    application.add_handler(profile_handler)
    application.add_handler(find_by_gender_handler)
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(start_choice_callback, pattern='^start_random_search$'))
    
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("next", next_command))
    application.add_handler(CommandHandler("myid", myid_command))
    
    # Perintah Admin/Owner
    application.add_handler(CommandHandler("grant_pro", grant_pro_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("adminstats", admin_stats_command))
    
    # Handler untuk tombol inline
    application.add_handler(CallbackQueryHandler(handle_report_button, pattern='^report_'))
    application.add_handler(CallbackQueryHandler(post_chat_action_callback, pattern='^post_chat_'))
    
    # Handler pesan umum (harus terakhir)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_message))
    
    print("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
