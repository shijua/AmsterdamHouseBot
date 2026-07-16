import asyncio
import logging

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db
from kamernet_autoreply import close_browser as close_kamernet_autoreply_browser
from scanner import (
    GENERAL_SOURCES,
    PARARIUS_SOURCES,
    ROOFZ_SOURCES,
    enabled_all_sources,
    run_scan_for_user,
)
from scrapers.kamernet import (
    KAMERNET_PROPERTY_TYPE_LABELS,
    format_kamernet_property_types,
    serialize_kamernet_property_types,
)
from scrapers.http_clients import close_shared_clients
from scrapers.roofz import RoofzScraper

logger = logging.getLogger(__name__)

ASK_PROPERTY_TYPE, ASK_PRICE, ASK_BEDROOMS, ASK_SIZE = range(4)
DEFAULT_CITY = "Amsterdam"
DEFAULT_MAX_PRICE = 2000
DEFAULT_MIN_BEDROOMS = 1
DEFAULT_MIN_SIZE_M2 = 0
DEFAULT_KAMERNET_PROPERTY_TYPE = "any"
KAMERNET_PROPERTY_TYPE_DONE = "Done"
KAMERNET_PROPERTY_TYPE_CLEAR = "Clear selection"
KAMERNET_PROPERTY_TYPE_CHOICES = {
    label: key
    for key, label in KAMERNET_PROPERTY_TYPE_LABELS.items()
}


def create_application() -> Application:
    async def _post_init(app: Application) -> None:
        await db.init_db()
        logger.info("Database initialized.")

    async def _post_shutdown(app: Application) -> None:
        await RoofzScraper.close_browser()
        await close_kamernet_autoreply_browser()
        await close_shared_clients()
        await db.close_db()
        logger.info("Scraper and database resources closed.")

    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["help", "commands"], cmd_help))
    app.add_handler(CommandHandler("filters", cmd_filters))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("autoreply_on", cmd_autoreply_on))
    app.add_handler(CommandHandler("autoreply_off", cmd_autoreply_off))
    app.add_handler(CommandHandler("autoreply_status", cmd_autoreply_status))
    app.add_handler(CommandHandler("autoreply_template", cmd_autoreply_template))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("clear", cmd_clear))

    search_conversation = ConversationHandler(
        entry_points=[CommandHandler("search", cmd_search)],
        states={
            ASK_PROPERTY_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_property_type)],
            ASK_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_price)],
            ASK_BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_bedrooms)],
            ASK_SIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_size)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    app.add_handler(search_conversation)

    for callback, first, name in _fast_scan_jobs():
        _schedule_fast_scan(app, callback, first=first, name=name)

    return app


def _fast_scan_jobs() -> tuple[tuple[object, int, str], ...]:
    jobs = (
        (scheduled_pararius_scan, 5, "pararius-fast-scan"),
        (scheduled_general_scan, 10, "general-fast-scan"),
    )
    if config.ROOFZ_ENABLED:
        jobs += ((scheduled_roofz_scan, 15, "roofz-fast-scan"),)
    return jobs


def _schedule_fast_scan(app: Application, callback, first: int, name: str) -> None:
    app.job_queue.run_repeating(
        callback,
        interval=config.FAST_POLL_INTERVAL_SECONDS,
        first=first,
        name=name,
        job_kwargs=_scan_job_kwargs(config.FAST_POLL_INTERVAL_SECONDS),
    )


async def scheduled_pararius_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _scheduled_scan_for_sources(context, "Pararius fast", PARARIUS_SOURCES)


async def scheduled_general_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _scheduled_scan_for_sources(context, "General fast", GENERAL_SOURCES)


async def scheduled_roofz_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _scheduled_scan_for_sources(context, "Roofz fast", ROOFZ_SOURCES)


async def _scheduled_scan_for_sources(
    context: ContextTypes.DEFAULT_TYPE,
    label: str,
    sources: tuple[str, ...],
) -> None:
    users = await db.get_all_active_users()
    if config.TELEGRAM_ALLOWED_CHAT_IDS:
        users = [user for user in users if user["chat_id"] in config.TELEGRAM_ALLOWED_CHAT_IDS]
    max_concurrent = max(1, config.MAX_CONCURRENT_USERS_PER_JOB)
    logger.info(
        "%s scheduled scan: %d active users, max_concurrent_users=%d, sources=%s",
        label,
        len(users),
        max_concurrent,
        ",".join(sources),
    )

    semaphore = asyncio.Semaphore(max_concurrent)

    async def scan_user(user: dict) -> None:
        async with semaphore:
            await _run_scheduled_scan_for_user(context, label, sources, user)

    await asyncio.gather(*(scan_user(user) for user in users))


async def _run_scheduled_scan_for_user(
    context: ContextTypes.DEFAULT_TYPE,
    label: str,
    sources: tuple[str, ...],
    user: dict,
) -> None:
    try:
        await run_scan_for_user(context.bot, user, sources=sources)
    except Exception as exc:
        logger.error("%s scan error for user %s: %s", label, user["chat_id"], exc)


def _scan_job_kwargs(interval_seconds: int) -> dict:
    job_kwargs = {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": max(5, interval_seconds),
    }
    jitter_seconds = max(0, config.SCAN_JITTER_SECONDS)
    if jitter_seconds:
        job_kwargs["jitter"] = jitter_seconds
    return job_kwargs


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_authorized(update):
        return

    await update.message.reply_text(
        "Amsterdam House Bot is running.\n\n"
        f"{_format_command_help()}\n\n"
        "Notifications start after you complete /search.\n"
        f"{_format_scan_schedule()}"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_authorized(update):
        return

    await update.message.reply_text(_format_command_help())


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_authorized(update):
        return

    user_filters = await db.get_filters(update.effective_chat.id)
    if not user_filters:
        await update.message.reply_text("No filters configured. Use /search.")
        return

    await update.message.reply_text(_format_filters(user_filters))


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_authorized(update):
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    previous_filters = await db.get_filters(chat_id)
    context.user_data.clear()
    context.user_data["had_filters"] = previous_filters is not None
    context.user_data["previous_active"] = (
        previous_filters["active"] if previous_filters else True
    )
    if previous_filters:
        await db.set_setup_in_progress(chat_id, True)

    await update.message.reply_text(
        "Rental preferences?\n"
        "Tap one or more options, then Done.\n"
        "Apartment applies to Kamernet, Huurwoningen, Pararius, and Funda.\n"
        "Furnished applies to Kamernet, Huurwoningen, and Pararius.\n"
        "Short/Long Term remain Kamernet-only.\n"
        "Choose Any property type to skip these preferences.",
        reply_markup=ReplyKeyboardMarkup(
            _property_type_keyboard(),
            one_time_keyboard=False,
            resize_keyboard=True,
            input_field_placeholder="Choose property types",
        ),
    )
    return ASK_PROPERTY_TYPE


async def receive_property_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_authorized(update):
        return ConversationHandler.END

    choice = (update.message.text or "").strip()
    if choice == KAMERNET_PROPERTY_TYPE_DONE:
        selected_types = context.user_data.get("kamernet_property_types", [])
        context.user_data["kamernet_property_type"] = serialize_kamernet_property_types(
            selected_types or DEFAULT_KAMERNET_PROPERTY_TYPE
        )
        return await _ask_price(update)

    if choice == KAMERNET_PROPERTY_TYPE_CLEAR:
        context.user_data["kamernet_property_types"] = []
        await update.message.reply_text(
            "Selection cleared. Choose property types, or tap Done for Any property type.",
            reply_markup=ReplyKeyboardMarkup(
                _property_type_keyboard(),
                one_time_keyboard=False,
                resize_keyboard=True,
                input_field_placeholder="Choose property types",
            ),
        )
        return ASK_PROPERTY_TYPE

    property_types, invalid_choices = _parse_property_type_choices(choice)
    if invalid_choices:
        await update.message.reply_text(
            "Please choose property types from the menu, or type labels separated by commas."
        )
        return ASK_PROPERTY_TYPE

    if "any" in property_types:
        context.user_data["kamernet_property_types"] = []
        context.user_data["kamernet_property_type"] = DEFAULT_KAMERNET_PROPERTY_TYPE
        return await _ask_price(update)

    selected_types = list(context.user_data.get("kamernet_property_types", []))
    for property_type in property_types:
        if property_type in selected_types:
            selected_types.remove(property_type)
        else:
            selected_types.append(property_type)
    context.user_data["kamernet_property_types"] = selected_types

    await update.message.reply_text(
        _format_property_type_selection(selected_types),
        reply_markup=ReplyKeyboardMarkup(
            _property_type_keyboard(),
            one_time_keyboard=False,
            resize_keyboard=True,
            input_field_placeholder="Choose property types",
        ),
    )
    return ASK_PROPERTY_TYPE


async def receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_authorized(update):
        return ConversationHandler.END

    price = _parse_non_negative_int(update.message.text)
    if price is None:
        await update.message.reply_text("Please send a valid number, for example 1500.")
        return ASK_PRICE

    context.user_data["max_price"] = price
    await update.message.reply_text(
        "Minimum bedrooms/rooms?\n"
        "Send 1, 2, 3, etc. Use 0 if you do not want this filter."
    )
    return ASK_BEDROOMS


async def receive_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_authorized(update):
        return ConversationHandler.END

    bedrooms = _parse_non_negative_int(update.message.text)
    if bedrooms is None:
        await update.message.reply_text("Please send a valid number, for example 2.")
        return ASK_BEDROOMS

    context.user_data["min_bedrooms"] = bedrooms
    await update.message.reply_text(
        "Minimum surface area in square meters?\n"
        "Send a number like 45, or 0 for no minimum size."
    )
    return ASK_SIZE


async def receive_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_authorized(update):
        return ConversationHandler.END

    min_size_m2 = _parse_non_negative_int(update.message.text)
    if min_size_m2 is None:
        await update.message.reply_text("Please send a valid number, for example 45.")
        return ASK_SIZE

    chat_id = update.effective_chat.id
    await db.save_filters(
        chat_id,
        max_price=context.user_data.get("max_price", DEFAULT_MAX_PRICE),
        min_bedrooms=context.user_data.get("min_bedrooms", DEFAULT_MIN_BEDROOMS),
        min_size_m2=min_size_m2,
        city=DEFAULT_CITY,
        kamernet_property_type=context.user_data.get(
            "kamernet_property_type",
            DEFAULT_KAMERNET_PROPERTY_TYPE,
        ),
        active=context.user_data.get("previous_active", True),
    )
    context.user_data.clear()

    saved_filters = await db.get_filters(chat_id)
    await update.message.reply_text("Filters saved.\n\n" + _format_filters(saved_filters))
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_authorized(update):
        return ConversationHandler.END

    if context.user_data.get("had_filters"):
        await db.set_setup_in_progress(update.effective_chat.id, False)
    context.user_data.clear()
    await update.message.reply_text("Search setup cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_authorized(update):
        return

    await db.set_active(update.effective_chat.id, False)
    await update.message.reply_text("Notifications paused. Use /resume to resume.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_authorized(update):
        return

    await db.set_active(update.effective_chat.id, True)
    await update.message.reply_text("Notifications resumed.")


async def cmd_autoreply_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    user_filters = await db.get_filters(chat_id)
    if not user_filters:
        await update.message.reply_text("Set your filters first with /search.")
        return
    if user_filters.get("setup_in_progress"):
        await update.message.reply_text("Finish or cancel /search before enabling auto-reply.")
        return

    await db.set_kamernet_autoreply_enabled(chat_id, True)
    await update.message.reply_text(
        "Kamernet auto-reply enabled. I will automatically reply to new Kamernet listings that match your filters."
    )


async def cmd_autoreply_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_authorized(update):
        return

    if await _require_saved_filters(update) is None:
        return

    await db.set_kamernet_autoreply_enabled(update.effective_chat.id, False)
    await update.message.reply_text("Kamernet auto-reply disabled.")


async def cmd_autoreply_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    user_filters = await db.get_filters(chat_id)
    if not user_filters:
        await update.message.reply_text("No filters configured. Use /search.")
        return

    stats = await db.get_kamernet_autoreply_stats(chat_id)
    template = _kamernet_autoreply_template(user_filters)
    status = "Enabled" if user_filters.get("kamernet_autoreply_enabled") else "Disabled"
    handled_statuses = {"sent", "sent_unconfirmed", "already_replied", "dry_run"}
    await update.message.reply_text(
        "Kamernet auto-reply:\n"
        f"Status: {status}\n"
        f"Sent: {stats.get('sent', 0)}\n"
        f"Unconfirmed: {stats.get('sent_unconfirmed', 0)}\n"
        f"Already replied: {stats.get('already_replied', 0)}\n"
        f"Failed: {sum(count for key, count in stats.items() if key not in handled_statuses)}\n"
        f"Dry runs: {stats.get('dry_run', 0)}\n\n"
        f"Template:\n{template}"
    )


async def cmd_autoreply_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_authorized(update):
        return

    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    chat_id = update.effective_chat.id
    if len(parts) == 1:
        user_filters = await db.get_filters(chat_id)
        template = _kamernet_autoreply_template(user_filters or {})
        await update.message.reply_text(
            "Current Kamernet auto-reply template:\n"
            f"{template}\n\n"
            "Set a new one with:\n"
            "/autoreply_template Hello, I am interested in this place..."
        )
        return

    template = parts[1].strip()
    if not template:
        await update.message.reply_text("Please include the template text after /autoreply_template.")
        return

    if await _require_saved_filters(update) is None:
        return

    await db.set_kamernet_autoreply_template(chat_id, template)
    await update.message.reply_text("Kamernet auto-reply template saved.")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_authorized(update):
        return

    await db.clear_seen()
    await update.message.reply_text(
        "Seen and sent listings were cleared. The next scan will treat matching listings as new."
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    user_filters = await db.get_filters(chat_id)
    if not user_filters:
        await update.message.reply_text("Set your filters first with /search.")
        return
    if user_filters.get("setup_in_progress"):
        await update.message.reply_text("Finish or cancel /search before running a test scan.")
        return

    await update.message.reply_text("Searching for listings now...")
    count = await run_scan_for_user(
        context.bot,
        user_filters,
        require_active=False,
        sources=enabled_all_sources(),
    )
    if count == 0:
        await update.message.reply_text("No new matching listings found at the moment.")
    else:
        await update.message.reply_text(f"Sent {count} new matching listings.")


def _is_authorized(update: Update) -> bool:
    if not config.TELEGRAM_ALLOWED_CHAT_IDS:
        return True
    if not update.effective_chat:
        return False
    return update.effective_chat.id in config.TELEGRAM_ALLOWED_CHAT_IDS


async def _ensure_authorized(update: Update) -> bool:
    if _is_authorized(update):
        return True

    chat_id = update.effective_chat.id if update.effective_chat else "unknown"
    logger.warning("Unauthorized Telegram chat attempted to use the bot: %s", chat_id)
    if update.message:
        await update.message.reply_text(
            "This is a private bot.\n"
            f"Your chat ID is {chat_id}."
        )
    return False


async def _require_saved_filters(update: Update) -> dict | None:
    user_filters = await db.get_filters(update.effective_chat.id)
    if not user_filters:
        await update.message.reply_text("Set your filters first with /search.")
        return None
    return user_filters


def _parse_non_negative_int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        value = int(text.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


async def _ask_price(update: Update) -> int:
    await update.message.reply_text(
        "Maximum monthly rent in EUR?\n"
        "Send a number like 1800, or 0 for no limit.\n\n"
        "Use /cancel to stop.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_PRICE


def _property_type_keyboard() -> list[list[str]]:
    return [
        [KAMERNET_PROPERTY_TYPE_DONE, KAMERNET_PROPERTY_TYPE_CLEAR],
        ["Any property type"],
        ["Room", "Apartment"],
        ["Studio", "Anti-squat"],
        ["Student Housing", "Furnished"],
        ["Short Term", "Long Term"],
    ]


def _parse_property_type_choices(text: str | None) -> tuple[list[str], list[str]]:
    if not text:
        return [], [""]

    label_lookup = {
        " ".join(label.split()).lower(): key
        for label, key in KAMERNET_PROPERTY_TYPE_CHOICES.items()
    }
    parts = []
    for value in text.replace(";", ",").replace("\n", ",").split(","):
        label = " ".join(value.strip().split())
        if label:
            parts.append(label)

    property_types: list[str] = []
    invalid_choices: list[str] = []
    for label in parts:
        property_type = label_lookup.get(label.lower())
        if property_type is None:
            invalid_choices.append(label)
        elif property_type not in property_types:
            property_types.append(property_type)

    return property_types, invalid_choices


def _format_property_type_selection(property_types: list[str]) -> str:
    if not property_types:
        return "No preferences selected. Tap Done for Any property type."
    selected = format_kamernet_property_types(property_types)
    return (
        f"Selected: {selected}\n"
        "Choose another preference, tap a selected option again to remove it, or tap Done."
    )


def _format_filters(user_filters: dict) -> str:
    max_price = user_filters["max_price"]
    min_size = user_filters["min_size_m2"]
    price_text = f"EUR {max_price}/month" if max_price else "No limit"
    bedrooms_text = user_filters["min_bedrooms"] or "No minimum"
    size_text = f"{min_size} m2" if min_size else "No minimum"
    property_preferences = format_kamernet_property_types(
        user_filters.get("kamernet_property_type", DEFAULT_KAMERNET_PROPERTY_TYPE),
    )
    status_text = "Setup in progress"
    if not user_filters.get("setup_in_progress"):
        status_text = "Active" if user_filters["active"] else "Paused"
    autoreply_status = "Enabled" if user_filters.get("kamernet_autoreply_enabled") else "Disabled"
    return (
        "Active filters:\n"
        f"City: {user_filters['city']}\n"
        f"Rental preferences: {property_preferences}\n"
        "Apartment sources: Kamernet, Huurwoningen, Pararius, Funda\n"
        "Furnished sources: Kamernet, Huurwoningen, Pararius\n"
        "Kamernet search radius: 5 km\n"
        f"Max rent: {price_text}\n"
        f"Minimum bedrooms/rooms: {bedrooms_text}\n"
        f"Minimum size: {size_text}\n"
        f"Status: {status_text}\n"
        f"Kamernet auto-reply: {autoreply_status}"
    )


def _format_command_help() -> str:
    property_types = ", ".join(KAMERNET_PROPERTY_TYPE_LABELS.values())
    return (
        "Available commands:\n"
        "/start - start the bot and show this help\n"
        "/help - show available commands and options\n"
        "/commands - same as /help\n"
        "/search - set or update your filters\n"
        "/filters - show active filters\n"
        "/test - scan now using saved filters\n"
        "/autoreply_on - automatically reply to new matching Kamernet listings\n"
        "/autoreply_off - disable Kamernet auto-reply\n"
        "/autoreply_status - show Kamernet auto-reply state\n"
        "/autoreply_template - show or set the Kamernet reply text\n"
        "/pause - pause notifications\n"
        "/resume - resume notifications\n"
        "/clear - clear sent/seen listings\n"
        "/cancel - cancel the /search setup flow\n\n"
        "/search options:\n"
        f"Rental preferences: {property_types}\n"
        "Apartment: Kamernet, Huurwoningen, Pararius, Funda\n"
        "Furnished: Kamernet, Huurwoningen, Pararius\n"
        "Short/Long Term: Kamernet only\n"
        "Max rent: number in EUR, or 0 for no limit\n"
        "Minimum bedrooms/rooms: number, or 0 for no minimum\n"
        "Minimum size: number in m2, or 0 for no minimum"
    )


def _kamernet_autoreply_template(user_filters: dict) -> str:
    return (
        user_filters.get("kamernet_autoreply_template")
        or config.KAMERNET_AUTOREPLY_DEFAULT_TEMPLATE
    ).strip()


def _format_interval(seconds: int) -> str:
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def _format_scan_schedule() -> str:
    sources = "Pararius, Funda, Kamernet, Huurwoningen, and VVA"
    if config.ROOFZ_ENABLED:
        sources = "Pararius, Funda, Kamernet, Huurwoningen, VVA, and Roofz"
    return f"I scan {sources} every {_format_interval(config.FAST_POLL_INTERVAL_SECONDS)}."
