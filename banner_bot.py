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
     post the banner there.
  3. Preview the saved banner at any time.
  4. Update the banner or the group at any time.
  5. Configure an auto-posting interval (in minutes) and turn
     auto-posting ON/OFF via JobQueue.
  6. Receive a confirmation message every time the banner is
     automatically posted, and see simple statistics
     (total sends + last send time).

REQUIREMENTS
------------
    pip install "python-telegram-bot[job-queue]"

IMPORTANT NOTE ABOUT THE "GROUP LINK"
--------------------------------------
Telegram's Bot API cannot send messages using an invite link
(e.g. https://t.me/joinchat/xxxx). To post messages, the bot needs
either:
  - the numeric chat id of the group (e.g. -1001234567890), or
  - the group's public @username (e.g. @my_group)
The bot must already be an ADMIN of that group with permission to
post messages. We still store whatever the admin sends under the
key "group_link" for simplicity, but it is used directly as the
`chat_id` parameter when sending messages.
====================================================================
"""

import os
import logging
from datetime import datetime
from functools import wraps

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
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
    "group_link": None,      # chat_id / @username of the target group
    "auto_post": {
        "enabled": False,
        "interval_minutes": None,
    },
    "stats": {
        "sent_count": 0,
        "last_sent_at": None,  # datetime of the last successful send
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
# HELPERS
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
    return (
        "⚙️ *پنل مدیریت بنر*\n\n"
        f"وضعیت ارسال خودکار: {status_line}\n"
        f"فاصله زمانی: {interval_line}\n"
        f"گروه هدف: `{storage['group_link'] or 'تنظیم نشده'}`\n\n"
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
        await message.reply_text("لینک یا آیدی عددی گروه را ارسال کنید (مثال: -1001234567890 یا @groupusername).")
        return WAITING_GROUP_LINK

    # Otherwise this was just an update — go straight back to the menu.
    await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END


@admin_only
async def receive_group_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the group chat_id / @username sent by the admin."""
    text = (update.message.text or "").strip()

    if not text:
        await update.message.reply_text("متن نامعتبر است. لطفاً آیدی گروه را دوباره ارسال کنید.")
        return WAITING_GROUP_LINK

    storage["group_link"] = text
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
    await query.edit_message_text("آیدی یا لینک جدید گروه را ارسال کنید.")
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
    and notifies the admin with a confirmation message.
    """
    banner = storage["banner"]
    group_link = storage["group_link"]

    if not banner["file_id"] or not group_link:
        logger.warning("Auto-post job fired but banner/group is missing. Skipping.")
        return

    try:
        await context.bot.send_photo(
            chat_id=group_link,
            photo=banner["file_id"],
            caption=banner["caption"],
        )
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

    except Exception as exc:  # noqa: BLE001 - we want to catch and report ANY send failure
        logger.error("Failed to send banner: %s", exc)
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"❌ ارسال بنر با خطا مواجه شد:\n`{exc}`",
            parse_mode=ParseMode.MARKDOWN,
        )


# ====================================================================
# GLOBAL ERROR HANDLER
# ====================================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs unhandled exceptions and, if possible, notifies the admin."""
    logger.error("Unhandled exception while processing update: %s", context.error, exc_info=context.error)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ خطای غیرمنتظره در ربات:\n`{context.error}`",
            parse_mode=ParseMode.MARKDOWN,
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
    application.add_handler(CallbackQueryHandler(menu_stats, pattern="^menu:stats$"))
    application.add_handler(CallbackQueryHandler(menu_back, pattern="^menu:back$"))
    application.add_handler(CallbackQueryHandler(menu_toggle_autopost, pattern="^menu:toggle_autopost$"))

    # --- Global error handler ---
    application.add_error_handler(error_handler)

    return application


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Please set it at the top of this file before running.")

    application = build_application()
    logger.info("Bot is starting (admin_id=%s)...", ADMIN_ID)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
