"""
====================================================================
 SINGLE-ADMIN TELEGRAM BANNER AUTO-POSTING BOT
====================================================================
Built with python-telegram-bot v20+ (fully async).

WHAT THIS BOT DOES
-------------------
Only ONE Telegram user (the admin, identified by ADMIN_ID) may use
this bot. Every other user's messages/callbacks are silently ignored.

The admin can:
  1. Send a banner (a photo + caption) to be stored.
  2. Provide the target group's chat id / @username so the bot can
     post the banner there (validated on entry — see below).
  3. Preview the saved banner at any time.
  4. Update the banner or the group at any time.
  5. Test the group connection and send a one-off test banner.
  6. Configure an auto-posting interval (in minutes) and turn
     auto-posting ON/OFF via JobQueue.
  7. Receive a confirmation message every time the banner is
     automatically posted, and see simple statistics
     (total sends + last send time).

REQUIREMENTS
------------
    pip install "python-telegram-bot[job-queue]"

IMPORTANT NOTE ABOUT THE "GROUP LINK"
--------------------------------------
This bot is a standard Telegram Bot API bot (no Telethon/Pyrogram/
userbot automation). Because of that:

  - It can NEVER join a group automatically via an invite link
    (https://t.me/+xxxx or https://t.me/joinchat/xxxx). The admin
    must add the bot to the group manually first.
  - Once the bot is a member, the admin gives it either the group's
    numeric chat id (e.g. -1001234567890) or its public @username.
  - The bot does NOT need to be an administrator of the group. It
    only needs "send messages" permission as a normal member. We
    only report a permission problem if Telegram actually refuses
    to let the bot send messages — we never assume admin is required.
====================================================================
"""

import os
import re
import logging
from datetime import datetime
from functools import wraps

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode, ChatMemberStatus, ChatType
from telegram.error import (
    TelegramError,
    Forbidden,
    BadRequest,
    ChatMigrated,
    TimedOut,
    NetworkError,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ====================================================================
# CONFIGURATION
# ----------------------------------------------------------------
# You can either hardcode these two values below, OR (recommended for
# deployment platforms like Railway) set them as environment variables
# named BOT_TOKEN and ADMIN_ID. Environment variables, if present,
# always take priority over the hardcoded fallback values.
# ====================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")          # <-- put your bot token here (or set env var)
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))  # <-- put your numeric Telegram user id here (or set env var)

# Fixed name used to identify the recurring auto-post job in JobQueue
AUTO_POST_JOB_NAME = "banner_auto_post_job"

# Matches Telegram invite links, e.g. https://t.me/+AbCdEf or https://t.me/joinchat/AbCdEf
INVITE_LINK_PATTERN = re.compile(r"^(https?://)?t\.me/(\+|joinchat/)", re.IGNORECASE)

# ====================================================================
# LOGGING
# ====================================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Silence overly verbose libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("banner_bot")

# ====================================================================
# IN-MEMORY STORAGE
# ----------------------------------------------------------------
# Since this bot serves exactly one admin, a single global dict is
# enough. In a multi-user bot you would key this by user_id instead.
# ====================================================================
storage = {
    "banner": {
        "file_id": None,     # Telegram file_id of the saved photo
        "caption": None,     # Caption text saved with the photo
    },
    "group_link": None,      # chat_id / @username of the target group (normalized to numeric id after validation)
    "group_title": None,     # human-readable group title, saved for display purposes
    "auto_post": {
        "enabled": False,
        "interval_minutes": None,
    },
    "stats": {
        "sent_count": 0,
        "last_sent_at": None,  # datetime of the last successful automatic send
    },
}

# ====================================================================
# CONVERSATION STATES
# ====================================================================
WAITING_BANNER, WAITING_GROUP_LINK, WAITING_INTERVAL = range(3)


# ====================================================================
# ACCESS CONTROL
# ----------------------------------------------------------------
# Every handler is protected with this decorator so that non-admin
# users are completely ignored (no reply is sent to them at all).
# ====================================================================
def admin_only(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if user is None or user.id != ADMIN_ID:
            logger.warning("Ignored update from unauthorized user: %s", user.id if user else "unknown")
            # Acknowledge callback queries silently so Telegram doesn't
            # show a loading spinner forever on the unauthorized user's client.
            if update.callback_query:
                await update.callback_query.answer()
            return  # Simply do nothing further.
        return await handler(update, context, *args, **kwargs)
    return wrapper


# ====================================================================
# GENERAL HELPERS
# ====================================================================
def is_fully_configured() -> bool:
    """Returns True once both banner and group link have been saved."""
    return storage["banner"]["file_id"] is not None and storage["group_link"] is not None


def build_main_menu() -> InlineKeyboardMarkup:
    """Builds the main inline keyboard, reflecting the current bot state."""
    auto = storage["auto_post"]
    toggle_label = "⏸ غیرفعال‌سازی ارسال خودکار" if auto["enabled"] else "▶️ فعال‌سازی ارسال خودکار"

    keyboard = [
        [InlineKeyboardButton("🖼 پیش‌نمایش بنر", callback_data="menu:preview")],
        [
            InlineKeyboardButton("✏️ تغییر بنر", callback_data="menu:edit_banner"),
            InlineKeyboardButton("🔗 تغییر گروه", callback_data="menu:edit_group"),
        ],
        [
            InlineKeyboardButton("🔍 بررسی اتصال گروه", callback_data="menu:test_connection"),
            InlineKeyboardButton("📤 ارسال آزمایشی", callback_data="menu:test_send"),
        ],
        [InlineKeyboardButton("⏱ تنظیم فاصله زمانی", callback_data="menu:set_interval")],
        [InlineKeyboardButton(toggle_label, callback_data="menu:toggle_autopost")],
        [InlineKeyboardButton("📊 آمار ارسال", callback_data="menu:stats")],
    ]
    return InlineKeyboardMarkup(keyboard)


def menu_status_text() -> str:
    """Text shown above the main menu, summarizing current configuration."""
    auto = storage["auto_post"]
    status_line = "🟢 فعال" if auto["enabled"] else "🔴 غیرفعال"
    interval_line = f"{auto['interval_minutes']} دقیقه" if auto["interval_minutes"] else "تنظیم نشده"
    group_line = storage["group_title"] or storage["group_link"] or "تنظیم نشده"
    return (
        "⚙️ *پنل مدیریت بنر*\n\n"
        f"وضعیت ارسال خودکار: {status_line}\n"
        f"فاصله زمانی: {interval_line}\n"
        f"گروه هدف: `{group_line}`\n\n"
        "یکی از گزینه‌های زیر را انتخاب کنید:"
    )


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, new_message: bool = False):
    """
    Sends (or edits) the main menu message.
    `new_message=True` forces sending a fresh message instead of editing
    the existing one (used right after a conversation step finishes).
    """
    text = menu_status_text()
    markup = build_main_menu()

    if update.callback_query and not new_message:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    else:
        chat_id = update.effective_chat.id
        await context.bot.send_message(chat_id, text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)


def is_invite_link(text: str) -> bool:
    """Detects Telegram invite links, which bots can never auto-join through."""
    return bool(INVITE_LINK_PATTERN.match(text.strip()))


def classify_telegram_error(exc: Exception) -> str:
    """
    Translates a Telegram exception into a clear, plain Persian sentence
    (no leading emoji — callers add their own icon/header).
    Never exposes raw tracebacks or exception class names to the user.
    """
    if isinstance(exc, Forbidden):
        return "ربات اجازه ارسال پیام در این گروه را ندارد، عضو گروه نیست یا از گروه حذف/مسدود شده است."
    if isinstance(exc, BadRequest):
        msg = str(exc).lower()
        if "chat not found" in msg:
            return "گروه پیدا نشد. لطفاً آیدی عددی یا یوزرنیم گروه را بررسی کنید."
        if "not enough rights" in msg:
            return "ربات دسترسی کافی برای ارسال پیام در این گروه را ندارد."
        return "درخواست نامعتبر بود. لطفاً آیدی یا یوزرنیم گروه را بررسی کنید."
    if isinstance(exc, TimedOut):
        return "ارتباط با سرور تلگرام با مشکل مواجه شد. لطفاً دوباره تلاش کنید."
    if isinstance(exc, NetworkError):
        return "خطای شبکه هنگام ارتباط با تلگرام رخ داد. لطفاً دوباره تلاش کنید."
    if isinstance(exc, TelegramError):
        return "خطای نامشخصی در ارتباط با تلگرام رخ داد."
    return "خطای غیرمنتظره‌ای رخ داد."


def chat_member_status_fa(status: str) -> str:
    """Human-readable Persian label for a ChatMember status value."""
    mapping = {
        ChatMemberStatus.MEMBER: "عضو عادی",
        ChatMemberStatus.ADMINISTRATOR: "مدیر گروه",
        ChatMemberStatus.OWNER: "سازنده گروه",
        ChatMemberStatus.RESTRICTED: "محدودشده",
        ChatMemberStatus.LEFT: "خارج‌شده از گروه",
        ChatMemberStatus.BANNED: "مسدودشده در گروه",
    }
    return mapping.get(status, str(status))


async def check_group_access(bot, chat_identifier: str):
    """
    Checks whether the bot can access the given group and whether it is
    allowed to send messages there — WITHOUT requiring administrator rights.

    Returns a dict:
        {
            "ok": bool,
            "message": str (Persian, no leading emoji),
            "chat_id": int | None,
            "title": str | None,
            "status": str | None,       # e.g. "member", "administrator"
            "can_send": bool | None,
        }
    """
    # Step 1 — resolve the chat itself.
    try:
        chat = await bot.get_chat(chat_identifier)
    except Forbidden:
        return {"ok": False, "message": "ربات به این گروه دسترسی ندارد. ابتدا ربات را به گروه اضافه کنید.",
                "chat_id": None, "title": None, "status": None, "can_send": None}
    except BadRequest as exc:
        return {"ok": False, "message": classify_telegram_error(exc),
                "chat_id": None, "title": None, "status": None, "can_send": None}
    except (TimedOut, NetworkError) as exc:
        return {"ok": False, "message": classify_telegram_error(exc),
                "chat_id": None, "title": None, "status": None, "can_send": None}
    except TelegramError as exc:
        logger.error("Unexpected error while resolving chat %s: %s", chat_identifier, exc)
        return {"ok": False, "message": classify_telegram_error(exc),
                "chat_id": None, "title": None, "status": None, "can_send": None}

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return {"ok": False, "message": "این شناسه متعلق به یک گروه نیست. لطفاً آیدی یا یوزرنیم یک گروه معتبر وارد کنید.",
                "chat_id": chat.id, "title": chat.title, "status": None, "can_send": None}

    # Step 2 — check the bot's own membership/status in that group.
    try:
        bot_member = await bot.get_chat_member(chat.id, bot.id)
    except Forbidden:
        return {"ok": False, "message": "ربات عضو این گروه نیست. ابتدا ربات را به گروه اضافه کنید.",
                "chat_id": chat.id, "title": chat.title, "status": None, "can_send": None}
    except TelegramError as exc:
        logger.error("Unexpected error while checking bot membership in %s: %s", chat.id, exc)
        return {"ok": False, "message": classify_telegram_error(exc),
                "chat_id": chat.id, "title": chat.title, "status": None, "can_send": None}

    status = bot_member.status

    if status == ChatMemberStatus.LEFT:
        return {"ok": False, "message": "ربات عضو این گروه نیست. ابتدا ربات را به گروه اضافه کنید.",
                "chat_id": chat.id, "title": chat.title, "status": status, "can_send": False}

    if status == ChatMemberStatus.BANNED:
        return {"ok": False, "message": "ربات از این گروه حذف یا مسدود شده است.",
                "chat_id": chat.id, "title": chat.title, "status": status, "can_send": False}

    can_send = True
    if status == ChatMemberStatus.RESTRICTED:
        can_send = bool(getattr(bot_member, "can_send_messages", True))
        if not can_send:
            return {"ok": False, "message": "ربات در گروه عضو است، اما اجازه ارسال پیام ندارد.",
                    "chat_id": chat.id, "title": chat.title, "status": status, "can_send": False}

    # Member, administrator, owner, or restricted-but-allowed-to-send: all OK.
    # NOTE: we deliberately do NOT require administrator/owner status here.
    return {"ok": True, "message": "اتصال به گروه برقرار است.",
            "chat_id": chat.id, "title": chat.title, "status": status, "can_send": can_send}


async def send_banner_to_group(bot):
    """
    Attempts to send the saved banner to the saved group.
    Returns (success: bool, message: str) — message is Persian, no leading emoji.
    Used by both the recurring auto-post job and the manual "test send" button.
    """
    banner = storage["banner"]
    group_link = storage["group_link"]

    if not banner["file_id"] or not group_link:
        return False, "ابتدا بنر و گروه را تنظیم کنید."

    try:
        await bot.send_photo(chat_id=group_link, photo=banner["file_id"], caption=banner["caption"])
        return True, "ارسال با موفقیت انجام شد."
    except ChatMigrated as exc:
        # The group was upgraded to a supergroup; Telegram gives us the new id.
        storage["group_link"] = str(exc.new_chat_id)
        logger.info("Group migrated to supergroup. New chat id saved: %s", exc.new_chat_id)
        return False, "آیدی گروه تغییر کرده است. آیدی جدید ذخیره شد؛ لطفاً دوباره تلاش کنید."
    except TelegramError as exc:
        logger.error("Failed to send banner to group %s: %s", group_link, exc)
        return False, classify_telegram_error(exc)


# ====================================================================
# CONVERSATION: INITIAL SETUP / EDIT BANNER / EDIT GROUP / SET INTERVAL
# ====================================================================

@admin_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /start handler.
    - If nothing is configured yet, kicks off the setup conversation.
    - If already configured, just shows the main menu.
    """
    if is_fully_configured():
        await update.message.reply_text("خوش آمدید! تنظیمات قبلی شما موجود است.")
        await show_main_menu(update, context, new_message=True)
        return ConversationHandler.END

    await update.message.reply_text("بنر خود را ارسال کنید.")
    return WAITING_BANNER


@admin_only
async def receive_banner_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handles the photo (with caption) sent by the admin, whether this is
    the initial setup or an update to an existing banner.
    """
    message = update.message

    if not message.photo:
        await message.reply_text("لطفاً یک عکس (همراه با کپشن) ارسال کنید.")
        return WAITING_BANNER

    if not message.caption:
        await message.reply_text("لطفاً عکس را همراه با یک کپشن (متن) ارسال کنید.")
        return WAITING_BANNER

    # Telegram sends several resolutions; the last one is the highest quality.
    photo_file_id = message.photo[-1].file_id
    storage["banner"]["file_id"] = photo_file_id
    storage["banner"]["caption"] = message.caption

    await message.reply_text("بنر ثبت شد.")

    # If the group link hasn't been set yet, this is the first-time setup flow.
    if storage["group_link"] is None:
        await message.reply_text(
            "آیدی عددی گروه یا یوزرنیم عمومی گروه را ارسال کنید (مثال: -1001234567890 یا @groupusername).\n\n"
            "توجه: ابتدا باید ربات را به‌صورت دستی به گروه اضافه کرده باشید."
        )
        return WAITING_GROUP_LINK

    # Otherwise this was just an update — go straight back to the menu.
    await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END


@admin_only
async def receive_group_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handles the group chat_id / @username sent by the admin.
    Validates that the bot can access the group and can send messages
    there BEFORE saving anything. Administrator status is never required.
    """
    text = (update.message.text or "").strip()

    if not text:
        await update.message.reply_text("متن نامعتبر است. لطفاً آیدی گروه را دوباره ارسال کنید.")
        return WAITING_GROUP_LINK

    # Detect invite links early — a bot can never auto-join through one.
    if is_invite_link(text):
        await update.message.reply_text(
            "⚠️ ربات‌های تلگرام نمی‌توانند به‌صورت خودکار از طریق لینک دعوت وارد گروه شوند.\n\n"
            "ابتدا ربات را به‌صورت دستی به گروه اضافه کنید، سپس آیدی عددی گروه یا یوزرنیم عمومی گروه را وارد کنید."
        )
        return WAITING_GROUP_LINK

    await update.message.reply_text("⏳ در حال بررسی دسترسی ربات به گروه...")
    result = await check_group_access(context.bot, text)

    if not result["ok"]:
        await update.message.reply_text(
            f"❌ ربات به این گروه دسترسی ندارد.\n\n{result['message']}\n\n"
            "لطفاً یک آیدی عددی یا یوزرنیم معتبر دیگر ارسال کنید."
        )
        return WAITING_GROUP_LINK

    # Save the numeric chat id (more reliable long-term than a @username,
    # which can be changed or removed later).
    storage["group_link"] = str(result["chat_id"])
    storage["group_title"] = result["title"]

    await update.message.reply_text("✅ گروه با موفقیت شناسایی شد.\n🤖 ربات به گروه دسترسی دارد.")
    await update.message.reply_text("اطلاعات ذخیره شد.")
    await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END


@admin_only
async def entry_edit_banner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point from the menu button: '✏️ تغییر بنر'."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("بنر جدید را همراه با کپشن ارسال کنید.")
    return WAITING_BANNER


@admin_only
async def entry_edit_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point from the menu button: '🔗 تغییر گروه'."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "آیدی عددی یا یوزرنیم عمومی گروه جدید را ارسال کنید (مثال: -1001234567890 یا @groupusername).\n\n"
        "توجه: ابتدا باید ربات را به‌صورت دستی به گروه اضافه کرده باشید."
    )
    return WAITING_GROUP_LINK


@admin_only
async def entry_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point from the menu button: '⏱ تنظیم فاصله زمانی'."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("فاصله زمانی ارسال خودکار را به دقیقه وارد کنید (فقط عدد):")
    return WAITING_INTERVAL


@admin_only
async def receive_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validates and stores the auto-post interval, rescheduling the job if needed."""
    text = (update.message.text or "").strip()

    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("لطفاً یک عدد صحیح و مثبت برای دقیقه وارد کنید.")
        return WAITING_INTERVAL

    minutes = int(text)
    storage["auto_post"]["interval_minutes"] = minutes
    await update.message.reply_text(f"فاصله زمانی روی {minutes} دقیقه تنظیم شد.")

    # If auto-posting is already running, reschedule it with the new interval.
    if storage["auto_post"]["enabled"]:
        schedule_auto_post_job(context, minutes)
        await update.message.reply_text("زمان‌بندی ارسال خودکار به‌روزرسانی شد.")

    await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END


@admin_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel — aborts whatever step of the conversation the admin is in."""
    await update.message.reply_text("عملیات لغو شد.")
    if is_fully_configured():
        await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END


# ====================================================================
# MENU CALLBACKS (actions that do NOT require further text/photo input)
# ====================================================================

@admin_only
async def menu_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a preview of the currently saved banner."""
    query = update.callback_query
    await query.answer()

    banner = storage["banner"]
    if not banner["file_id"]:
        await query.answer("هنوز هیچ بنری ذخیره نشده است.", show_alert=True)
        return

    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=banner["file_id"],
        caption=f"🖼 پیش‌نمایش بنر ذخیره‌شده:\n\n{banner['caption']}",
    )


@admin_only
async def menu_test_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    '🔍 بررسی اتصال گروه' — checks whether the bot can access the configured
    group and whether it is allowed to send messages, WITHOUT requiring
    administrator rights.
    """
    query = update.callback_query
    await query.answer()

    if not storage["group_link"]:
        await query.answer("ابتدا یک گروه تنظیم کنید.", show_alert=True)
        return

    await context.bot.send_message(update.effective_chat.id, "⏳ در حال بررسی اتصال به گروه...")
    result = await check_group_access(context.bot, storage["group_link"])

    if result["ok"]:
        storage["group_title"] = result["title"] or storage["group_title"]
        text = (
            "🔍 نتیجه بررسی گروه\n\n"
            "✅ اتصال به گروه برقرار است.\n"
            "🤖 ربات به گروه دسترسی دارد.\n"
            f"📌 وضعیت ربات: {chat_member_status_fa(result['status'])}\n"
            "📤 ربات می‌تواند برای ارسال پیام تلاش کند."
        )
    else:
        text = f"🔍 نتیجه بررسی گروه\n\n❌ {result['message']}"

    await context.bot.send_message(update.effective_chat.id, text)


@admin_only
async def menu_test_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    '📤 ارسال آزمایشی' — sends the saved banner to the group ONCE, as a manual
    test. Does not touch the auto-post schedule, interval, or statistics.
    """
    query = update.callback_query
    await query.answer()

    if not is_fully_configured():
        await query.answer("ابتدا بنر و گروه را تنظیم کنید.", show_alert=True)
        return

    await context.bot.send_message(update.effective_chat.id, "⏳ در حال ارسال بنر آزمایشی...")
    success, message = await send_banner_to_group(context.bot)

    if success:
        await context.bot.send_message(update.effective_chat.id, "✅ بنر آزمایشی با موفقیت ارسال شد.")
    else:
        await context.bot.send_message(
            update.effective_chat.id,
            "❌ ارسال آزمایشی ناموفق بود.\n\n"
            f"{message}\n\n"
            "ممکن است ربات عضو گروه نباشد یا اجازه ارسال پیام نداشته باشد.",
        )


@admin_only
async def menu_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows simple send statistics."""
    query = update.callback_query
    await query.answer()

    stats = storage["stats"]
    last_sent = (
        stats["last_sent_at"].strftime("%Y-%m-%d %H:%M:%S")
        if stats["last_sent_at"]
        else "هنوز ارسالی انجام نشده"
    )

    text = (
        "📊 *آمار ارسال بنر*\n\n"
        f"تعداد کل ارسال‌ها: {stats['sent_count']}\n"
        f"آخرین ارسال: {last_sent}"
    )
    back_button = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="menu:back")]])
    await query.edit_message_text(text, reply_markup=back_button, parse_mode=ParseMode.MARKDOWN)


@admin_only
async def menu_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Returns to the main menu (used from sub-screens like stats)."""
    query = update.callback_query
    await query.answer()
    await show_main_menu(update, context)


@admin_only
async def menu_toggle_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enables or disables the recurring auto-post job."""
    query = update.callback_query

    auto = storage["auto_post"]

    if not auto["enabled"]:
        # --- Trying to ENABLE ---
        if not is_fully_configured():
            await query.answer("ابتدا بنر و گروه را تنظیم کنید.", show_alert=True)
            return
        if not auto["interval_minutes"]:
            await query.answer("ابتدا فاصله زمانی ارسال را تنظیم کنید.", show_alert=True)
            return

        schedule_auto_post_job(context, auto["interval_minutes"])
        auto["enabled"] = True
        await query.answer("ارسال خودکار فعال شد.")
    else:
        # --- Trying to DISABLE ---
        remove_auto_post_job(context)
        auto["enabled"] = False
        await query.answer("ارسال خودکار غیرفعال شد.")

    await show_main_menu(update, context)


# ====================================================================
# JOBQUEUE: SCHEDULING HELPERS + THE RECURRING JOB ITSELF
# ====================================================================

def remove_auto_post_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Removes any existing auto-post job, if one is scheduled."""
    current_jobs = context.job_queue.get_jobs_by_name(AUTO_POST_JOB_NAME)
    for job in current_jobs:
        job.schedule_removal()


def schedule_auto_post_job(context: ContextTypes.DEFAULT_TYPE, interval_minutes: int) -> None:
    """(Re)schedules the recurring auto-post job with the given interval."""
    remove_auto_post_job(context)  # avoid duplicate jobs
    context.job_queue.run_repeating(
        send_banner_job,
        interval=interval_minutes * 60,
        first=interval_minutes * 60,  # wait one full interval before the first send
        name=AUTO_POST_JOB_NAME,
    )
    logger.info("Auto-post job scheduled every %s minute(s).", interval_minutes)


async def send_banner_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    The actual recurring task: posts the saved banner to the saved group
    and notifies the admin with a confirmation message. This is the ONLY
    place that updates the sent_count / last_sent_at statistics.
    """
    success, message = await send_banner_to_group(context.bot)

    if success:
        storage["stats"]["sent_count"] += 1
        storage["stats"]["last_sent_at"] = datetime.now()

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "✅ بنر با موفقیت به گروه ارسال شد.\n"
                f"تعداد کل ارسال‌ها: {storage['stats']['sent_count']}"
            ),
        )
        logger.info("Banner sent successfully. Total sends: %s", storage["stats"]["sent_count"])
    else:
        logger.error("Auto-post failed: %s", message)
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"❌ ارسال خودکار بنر با خطا مواجه شد.\n\n{message}",
        )


# ====================================================================
# GLOBAL ERROR HANDLER
# ====================================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Logs unhandled exceptions in full detail (visible in Railway logs) and
    sends the admin a short, non-technical Persian notice — never a raw
    traceback or exception class name.
    """
    logger.error("Unhandled exception while processing update: %s", context.error, exc_info=context.error)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text="⚠️ خطای غیرمنتظره‌ای در ربات رخ داد. جزئیات در لاگ سرور ثبت شد.",
        )
    except Exception:
        # If we can't even notify the admin, just let the logged error stand.
        pass


# ====================================================================
# APPLICATION WIRING
# ====================================================================
def build_application() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # --- The main conversation: initial setup + edit banner/group/interval ---
    setup_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            CallbackQueryHandler(entry_edit_banner, pattern="^menu:edit_banner$"),
            CallbackQueryHandler(entry_edit_group, pattern="^menu:edit_group$"),
            CallbackQueryHandler(entry_set_interval, pattern="^menu:set_interval$"),
        ],
        states={
            WAITING_BANNER: [
                MessageHandler(filters.PHOTO & ~filters.COMMAND, receive_banner_photo),
            ],
            WAITING_GROUP_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_group_link),
            ],
            WAITING_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_interval),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

    application.add_handler(setup_conversation)

    # --- Standalone menu actions (no further input required) ---
    application.add_handler(CallbackQueryHandler(menu_preview, pattern="^menu:preview$"))
    application.add_handler(CallbackQueryHandler(menu_test_connection, pattern="^menu:test_connection$"))
    application.add_handler(CallbackQueryHandler(menu_test_send, pattern="^menu:test_send$"))
    application.add_handler(CallbackQueryHandler(menu_stats, pattern="^menu:stats$"))
    application.add_handler(CallbackQueryHandler(menu_back, pattern="^menu:back$"))
    application.add_handler(CallbackQueryHandler(menu_toggle_autopost, pattern="^menu:toggle_autopost$"))

    # --- Global error handler ---
    application.add_error_handler(error_handler)

    return application


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Please set it at the top of this file (or the BOT_TOKEN env var) before running.")

    application = build_application()
    logger.info("Bot is starting (admin_id=%s)...", ADMIN_ID)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
