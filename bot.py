import asyncio
import logging
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Dict, Deque, List, Set, Optional

from telegram import Update, ChatPermissions, MessageEntity
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# -------------------- تنظیمات --------------------
TOKEN = "8932564239:AAHpbG9M3Jz9QVkUWskx6tTXp3TgmSZNugw"  # توکن ربات را اینجا قرار دهید

# محدودیت‌های اسپم
SPAM_MSG_LIMIT = 5        # تعداد پیام مجاز در بازه زمانی
SPAM_TIME_WINDOW = 10     # پنجره زمانی (ثانیه)
SPAM_MUTE_DURATION = 120  # ۲ دقیقه میوت

# محدودیت گیف/استیکر
GIF_STICKER_LIMIT = 4
GIF_STICKER_WINDOW = 10   # ثانیه
GIF_MUTE_DURATION = 300   # ۵ دقیقه

# میوت برای فحش
PROFANITY_MUTE_DURATION = 3600  # ۱ ساعت

# میوت برای عکس/فیلم متخلف (بار سوم)
MEDIA_MUTE_DURATION = 10800     # ۳ ساعت

# -------------------- ذخیره‌سازی در حافظه --------------------
# اخطارهای عکس/فیلم: {(user_id, chat_id): تعداد اخطار}
media_warnings: Dict[tuple, int] = defaultdict(int)

# اخطارهای اسپم گیف/استیکر: {(user_id, chat_id): تعداد اخطار}
flood_warnings: Dict[tuple, int] = defaultdict(int)

# کاربرانی که استارت زده‌اند
start_users: Set[int] = set()

# گروه‌ها به همراه مالک
groups: Dict[int, int] = {}        # chat_id -> owner_id

# وضعیت بسته بودن گروه‌ها: chat_id -> bool
group_closed: Dict[int, bool] = {}

# اعضای گروه: chat_id -> set of user_id
group_members: Dict[int, Set[int]] = defaultdict(set)

# بنرهای تبلیغاتی: owner_id -> {'owner_chat_id': int, 'banner_message_id': int, 'active': bool}
banners: Dict[int, dict] = {}

# ذخیره‌سازی موقت برای تشخیص اسپم
general_spam: Dict[tuple, Deque[float]] = defaultdict(lambda: deque(maxlen=100))
gif_spam: Dict[tuple, Deque[float]] = defaultdict(lambda: deque(maxlen=100))

# -------------------- توابع کمکی --------------------
def clean_text(text: str) -> str:
    """حذف تمام کاراکترهای غیر فارسی/عربی و فاصله‌ها برای تشخیص فحش"""
    return re.sub(r'[^\u0600-\u06FF]', '', text)

BAD_WORDS_RAW = [
    "کص ننت", "ننت", "کص مادرت", "کیر", "کیرم", "عمت", "خالت",
    "زن", "خارتو", "خار", "کصه", "سیکتیر", "کص کش", "اب کیر",
    "ربات", "کانال", "گروه", "پیج", "سکس", "سکسی"
]
BAD_WORDS = [clean_text(w) for w in BAD_WORDS_RAW]

def contains_profanity(text: str) -> bool:
    """بررسی وجود کلمات ممنوعه در متن"""
    if not text:
        return False
    cleaned = clean_text(text)
    for bad in BAD_WORDS:
        if bad in cleaned:
            return True
    return False

def has_link(message) -> bool:
    """بررسی وجود لینک یا فروارد از کانال دیگر"""
    if message.entities:
        for entity in message.entities:
            if entity.type in [MessageEntity.URL, MessageEntity.TEXT_LINK]:
                return True
    if message.text and re.search(r'https?://\S+', message.text):
        return True
    if message.forward_origin is not None:
        return True
    return False

async def is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """بررسی مدیر یا مالک بودن کاربر"""
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ('creator', 'administrator')
    except:
        return False

async def mute_user(chat_id: int, user_id: int, duration_seconds: int, context: ContextTypes.DEFAULT_TYPE):
    """میوت کردن کاربر برای مدت مشخص"""
    until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
    permissions = ChatPermissions(can_send_messages=False)
    await context.bot.restrict_chat_member(chat_id, user_id, permissions, until_date=until)

async def kick_user(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """کیک کردن کاربر (اخراج بدون بن دائم)"""
    await context.bot.ban_chat_member(chat_id, user_id)
    await context.bot.unban_chat_member(chat_id, user_id)  # امکان ورود مجدد

# -------------------- مدیریت بنر --------------------
def schedule_banner_job(owner_id: int, chat_id: int, interval: int, job_queue, banner_callback):
    """ایجاد یا به‌روزرسانی جاب ارسال بنر برای یک گروه"""
    job_name = f"banner_{owner_id}_{chat_id}"
    for job in job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    next_send = datetime.now(timezone.utc) + timedelta(seconds=interval)
    job_queue.run_repeating(
        banner_callback,
        interval=interval,
        first=next_send,
        name=job_name,
        chat_id=chat_id,
        data={'owner_id': owner_id}
    )

async def banner_sender(context: ContextTypes.DEFAULT_TYPE):
    """ارسال بنر به گروه هدف"""
    job_data = context.job.data
    owner_id = job_data['owner_id']
    chat_id = context.job.chat_id

    banner = banners.get(owner_id)
    if not banner or not banner.get('active'):
        context.job.schedule_removal()
        return

    try:
        await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=banner['owner_chat_id'],
            message_id=banner['banner_message_id']
        )
    except Exception as e:
        logging.error(f"خطا در ارسال بنر: {e}")
        # اگر خطا باشد (مثل حذف پیام اصلی)، بنر غیرفعال شود
        banners[owner_id]['active'] = False
        context.job.schedule_removal()

async def update_banner(owner_id: int, message, context: ContextTypes.DEFAULT_TYPE):
    """ذخیره بنر جدید برای مالک و زمان‌بندی در گروه‌هایش"""
    banners[owner_id] = {
        'owner_chat_id': message.chat_id,
        'banner_message_id': message.message_id,
        'active': True
    }
    interval = 3600
    for chat_id, oid in groups.items():
        if oid == owner_id:
            schedule_banner_job(owner_id, chat_id, interval, context.application.job_queue, banner_sender)
    await message.reply_text("✅ بنر شما ذخیره و زمان‌بندی ارسال آغاز شد.")

# -------------------- handler ها --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in start_users:
        start_users.add(user.id)
        await update.message.reply_text("سلام منو به گروهت اضافه کن تا کارت رو راحت کنم 😃")
    else:
        await update.message.reply_text("شما قبلاً هم استارت زده‌اید.")

async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for member in update.message.new_chat_members:
        user_id = member.id
        mention = f"@{member.username}" if member.username else member.full_name
        welcome_text = f"سلام خوش اومدی {mention} به گروه 🖐️♥️"
        await context.bot.send_message(chat_id, welcome_text)
        group_members[chat_id].add(user_id)
    await update.message.delete()

async def left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.message.left_chat_member.id
    await context.bot.send_message(chat_id, "🥺😞")
    await update.message.delete()
    group_members[chat_id].discard(user_id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    if not message or not chat_id:
        return

    # --------------------- دستورات Close/Open ---------------------
    if message.text and message.text.strip() in ["Close", "open"]:
        if await is_admin(chat_id, user_id, context):
            cmd = message.text.strip()
            if cmd == "Close":
                await context.bot.set_chat_permissions(
                    chat_id,
                    ChatPermissions(can_send_messages=False)
                )
                group_closed[chat_id] = True
                await message.reply_text("🔒 گروه بسته شد.")
            else:
                await context.bot.set_chat_permissions(
                    chat_id,
                    ChatPermissions(
                        can_send_messages=True,
                        can_send_media_messages=True,
                        can_send_polls=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True
                    )
                )
                group_closed[chat_id] = False
                await message.reply_text("🔓 گروه باز شد.")
            return
        else:
            await message.reply_text("⛔ فقط مدیران یا مالک می‌توانند گروه را باز/بسته کنند.")
            return

    # --------------------- فیلتر لینک / فروارد ---------------------
    if has_link(message):
        await kick_user(chat_id, user_id, context)
        await message.delete()
        return

    # --------------------- فیلتر فحش ---------------------
    if message.text and contains_profanity(message.text):
        await mute_user(chat_id, user_id, PROFANITY_MUTE_DURATION, context)
        await message.delete()
        await context.bot.send_message(chat_id, f"⛔ کاربر [{user.full_name}](tg://user?id={user_id}) به دلیل استفاده از کلمات ممنوعه ۱ ساعت میوت شد.",
                                       parse_mode=ParseMode.MARKDOWN)
        return

    # --------------------- فیلتر عکس/فیلم ---------------------
    if message.photo or message.video:
        key = (user_id, chat_id)
        media_warnings[key] += 1
        current_warnings = media_warnings[key]
        if current_warnings >= 3:
            await mute_user(chat_id, user_id, MEDIA_MUTE_DURATION, context)
            del media_warnings[key]  # ریست اخطارها
            await context.bot.send_message(chat_id, f"⛔ کاربر [{user.full_name}](tg://user?id={user_id}) به دلیل ارسال مکرر عکس/فیلم ۳ ساعت میوت شد.",
                                           parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply_text(f"⚠️ هشدار {current_warnings}/۳: ارسال عکس/فیلم ممنوع است.")
        await message.delete()
        return

    # --------------------- اسپم گیف/استیکر ---------------------
    if message.animation or message.sticker:
        key = (chat_id, user_id)
        now = datetime.now(timezone.utc).timestamp()
        dq = gif_spam[key]
        dq.append(now)
        while dq and dq[0] < now - GIF_STICKER_WINDOW:
            dq.popleft()
        if len(dq) >= GIF_STICKER_LIMIT:
            flood_key = (user_id, chat_id)
            flood_warnings[flood_key] += 1
            warns = flood_warnings[flood_key]
            if warns == 1:
                await message.reply_text("⚠️ اخطار: ارسال اسپم گیف/استیکر. در صورت تکرار میوت می‌شوید.")
            else:
                await mute_user(chat_id, user_id, GIF_MUTE_DURATION, context)
                del flood_warnings[flood_key]
                await message.reply_text("⛔ شما به دلیل اسپم گیف/استیکر ۵ دقیقه میوت شدید.")
                dq.clear()
            return

    # --------------------- اسپم عمومی ---------------------
    now = datetime.now(timezone.utc).timestamp()
    spam_key = (chat_id, user_id)
    spam_deque = general_spam[spam_key]
    spam_deque.append(now)
    while spam_deque and spam_deque[0] < now - SPAM_TIME_WINDOW:
        spam_deque.popleft()
    if len(spam_deque) >= SPAM_MSG_LIMIT:
        await mute_user(chat_id, user_id, SPAM_MUTE_DURATION, context)
        await message.reply_text("⛔ به دلیل اسپم ۲ دقیقه میوت شدید.")
        spam_deque.clear()

async def bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    chat_id = chat.id
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.status == 'creator':
                groups[chat_id] = admin.user.id
                group_closed[chat_id] = False
                break
    except:
        pass

async def bot_removed_from_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    groups.pop(chat_id, None)
    group_closed.pop(chat_id, None)
    group_members.pop(chat_id, None)
    # حذف جاب‌های بنر مربوطه
    for job in context.application.job_queue.jobs():
        if job.chat_id == chat_id and job.name.startswith("banner_"):
            job.schedule_removal()

async def private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # بررسی مالک بودن
    is_owner = any(owner_id == user.id for owner_id in groups.values())
    if is_owner:
        await update_banner(user.id, update.effective_message, context)
    else:
        await update.message.reply_text("شما مالک هیچ گروهی نیستید. ابتدا بات را به گروه خود اضافه کنید.")

# -------------------- اصلی --------------------
def main():
    logging.basicConfig(level=logging.INFO)

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, left_member))
    app.add_handler(ChatMemberHandler(bot_added_to_group, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Sticker.ALL,
                                   handle_message), group=1)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_message))

    # بازیابی بنرهای فعال (اما چون همه چیز در حافظه است و تازه شروع شده، چیزی نیست)
    # در صورت نیاز می‌توانید بنرها را دستی تنظیم کنید.

    print("✅ ربات بدون دیتابیس اجرا شد...")
    app.run_polling()

if __name__ == "__main__":
    main()
