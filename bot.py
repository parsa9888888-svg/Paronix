import re
import time
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

import db

TOKEN = "8932564239:AAHpbG9M3Jz9QVkUWskx6tTXp3TgmSZNugw"

BAD_WORDS = [
    "کص","کیر","ننت","مادرت","عمه","خاله","سیکتیر","کص کش","اب کیر",
    "ربات","کانال","گروه","پیج","سکس","سکسی"
]

# ---------- تشخیص ----------
def clean(t):
    return re.sub(r"[.\s]", "", t.lower())

def is_bad(text):
    t = clean(text)
    return any(clean(w) in t for w in BAD_WORDS)

def has_link(text):
    return bool(re.search(r"http|t.me|www", text))

def is_spam(text):
    return len(text) > 200 or text.count("😂") > 5


# ---------- ورود ----------
async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for u in update.message.new_chat_members:
        db.add_user(u.id, update.message.chat.id)

        await update.message.reply_text(
            f"سلام خوش اومدی ({u.id}) به گروه 🖐️♥️"
        )

        await update.message.delete()


# ---------- خروج ----------
async def leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🥺😞")
    await update.message.delete()


# ---------- استارت ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام 😃 منو به گروهت اضافه کن تا کارت رو راحت کنم"
    )


# ---------- کنترل گروه (فقط ادمین) ----------
async def control(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text.lower()
    gid = msg.chat.id

    member = await context.bot.get_chat_member(gid, msg.from_user.id)

    # فقط ادمین/مالک
    if member.status not in ["administrator", "creator"]:
        return

    if text == "close":
        db.close_group(gid)
        await msg.reply_text("🔒 چت بسته شد")

    if text == "open":
        db.open_group(gid)
        await msg.reply_text("🔓 چت باز شد")


# ---------- عکس و فیلم ----------
async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = msg.from_user.id
    gid = msg.chat.id

    user = db.get_user(uid, gid)
    warns = user[2]

    db.add_warn(uid, gid)
    await msg.delete()

    if warns >= 2:
        await context.bot.restrict_chat_member(
            gid,
            uid,
            until_date=int(time.time() + 10800)  # 3 ساعت
        )
        db.log(uid, gid, "mute_3h_media")


# ---------- گیف و استیکر ----------
async def sticker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = msg.from_user.id
    gid = msg.chat.id

    user = db.get_user(uid, gid)
    warns = user[2]

    db.add_warn(uid, gid)
    await msg.delete()

    if warns >= 1:
        await context.bot.restrict_chat_member(
            gid,
            uid,
            until_date=int(time.time() + 300)  # 5 دقیقه
        )
        db.log(uid, gid, "mute_5m_sticker")


# ---------- پیام‌ها ----------
async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = msg.from_user.id
    gid = msg.chat.id
    text = msg.text or ""

    db.add_user(uid, gid)

    user = db.get_user(uid, gid)
    warns = user[2]
    mute_until = user[3]

    # mute check
    if mute_until and time.time() < mute_until:
        await msg.delete()
        return

    # لینک → بن
    if has_link(text):
        await msg.delete()
        await context.bot.ban_chat_member(gid, uid)
        db.log(uid, gid, "ban_link")
        return

    # فحش → warn
    if is_bad(text):
        db.add_warn(uid, gid)
        await msg.delete()

        if warns >= 2:
            await context.bot.restrict_chat_member(
                gid,
                uid,
                until_date=int(time.time() + 3600)  # 1 ساعت
            )
            db.log(uid, gid, "mute_1h")
        return

    # اسپم → 2 دقیقه میوت
    if is_spam(text):
        await msg.delete()

        await context.bot.restrict_chat_member(
            gid,
            uid,
            until_date=int(time.time() + 120)
        )
        db.log(uid, gid, "mute_2min")
        return


# ---------- اجرا ----------
app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, join))
app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, leave))

app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, media_handler))
app.add_handler(MessageHandler(filters.ANIMATION | filters.Sticker.ALL, sticker_handler))

app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))
app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^(close|open)$"), control))

print("Bot running...")
app.run_polling()