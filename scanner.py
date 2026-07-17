import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from html import escape
import logging
import time

from telegram import Bot
from telegram.constants import ParseMode

import config
import db
from kamernet_autoreply import KamernetAutoReplyResult, send_kamernet_autoreply
from scrapers.base import ForbiddenResponseError
from scrapers.funda import FundaScraper
from scrapers.huurwoningen import HuurwoningenScraper
from scrapers.kamernet import KamernetScraper, normalize_kamernet_property_types
from scrapers.pararius import ParariusScraper
from scrapers.roofz import RoofzScraper
from scrapers.vva import VVAScraper

logger = logging.getLogger(__name__)

PARARIUS_SOURCE = "pararius"
FUNDA_SOURCE = "funda"
KAMERNET_SOURCE = "kamernet"
HUURWONINGEN_SOURCE = "huurwoningen"
ROOFZ_SOURCE = "roofz"
VVA_SOURCE = "vva"

PARARIUS_SOURCES = (PARARIUS_SOURCE,)
GENERAL_SOURCES = (FUNDA_SOURCE, KAMERNET_SOURCE, HUURWONINGEN_SOURCE, VVA_SOURCE)
ROOFZ_SOURCES = (ROOFZ_SOURCE,)
FAST_SOURCES = PARARIUS_SOURCES + GENERAL_SOURCES + ROOFZ_SOURCES
ALL_SOURCES = FAST_SOURCES

_SCAN_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_FORBIDDEN_CIRCUITS: dict[str, "_ForbiddenCircuitState"] = {}


@dataclass
class _ForbiddenCircuitState:
    consecutive_failures: int = 0
    cooldown_count: int = 0
    blocked_until: float = 0.0


_FILTER_MATCH_KEYS = (
    "city",
    "max_price",
    "min_bedrooms",
    "min_size_m2",
    "kamernet_property_type",
)


async def run_scan_for_user(
    bot: Bot,
    user_filters: dict,
    require_active: bool = True,
    sources: Iterable[str] | None = None,
) -> int:
    chat_id = user_filters["chat_id"]
    source_names = _normalize_sources(sources)
    if not source_names:
        return 0

    started_at = time.perf_counter()
    logger.info("Scan started for user %s with sources=%s", chat_id, ",".join(source_names))

    if not await _scan_is_current(chat_id, user_filters, require_active):
        logger.info("Scan skipped for user %s because filters changed, setup is open, or user is paused.", chat_id)
        return 0

    tasks = [
        asyncio.create_task(_scan_source_for_user(bot, chat_id, user_filters, source, require_active))
        for source in source_names
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    new_count = 0
    for source, result in zip(source_names, results, strict=True):
        if isinstance(result, Exception):
            logger.error("Scan task failed for source %s and user %s: %s", source, chat_id, result)
            continue
        new_count += result

    logger.info(
        "Scan completed for user %s with sources=%s: %d new in %.2fs",
        chat_id,
        ",".join(source_names),
        new_count,
        time.perf_counter() - started_at,
    )
    return new_count


def enabled_all_sources() -> tuple[str, ...]:
    if config.ROOFZ_ENABLED:
        return FAST_SOURCES
    return tuple(source for source in FAST_SOURCES if source != ROOFZ_SOURCE)


async def _scan_source_for_user(
    bot: Bot,
    chat_id: int,
    user_filters: dict,
    source: str,
    require_active: bool,
) -> int:
    lock = _scan_lock(chat_id, source)
    async with lock:
        source_started_at = time.perf_counter()
        try:
            cooldown_remaining = _forbidden_circuit_remaining(source)
            if cooldown_remaining > 0:
                logger.info(
                    "%s scan skipped for user %s: HTTP 403 cooldown has %d minute(s) remaining",
                    source,
                    chat_id,
                    int(cooldown_remaining // 60) + 1,
                )
                return 0

            if not await _scan_is_current(chat_id, user_filters, require_active):
                logger.info("Scan stopped for user %s/%s before scraping stale filters.", chat_id, source)
                return 0

            scraper = _build_scraper(source, user_filters)
            try:
                listings = await _scrape_with_timing(scraper, chat_id)
            except ForbiddenResponseError:
                failure_count, cooldown_seconds = _record_forbidden_response(source)
                if cooldown_seconds:
                    logger.warning(
                        "%s paused for %.0f hour(s) after %d consecutive HTTP 403 responses",
                        source,
                        cooldown_seconds / 3600,
                        failure_count,
                    )
                else:
                    logger.warning(
                        "%s HTTP 403 response %d/%d before cooldown",
                        source,
                        failure_count,
                        config.FORBIDDEN_FAILURE_THRESHOLD,
                    )
                return 0

            if _record_source_success(source):
                logger.info("%s recovered from HTTP 403 responses; cooldown state cleared", source)

            if not listings:
                logger.info(
                    "%s: 0 listings found, 0 new for user %s in %.2fs",
                    source,
                    chat_id,
                    time.perf_counter() - source_started_at,
                )
                return 0

            if not await _scan_is_current(chat_id, user_filters, require_active):
                logger.info("Scan stopped for user %s/%s before sending stale results.", chat_id, source)
                return 0

            new_listing_ids = await db.get_unsent_listing_ids_and_mark_seen(
                chat_id,
                source,
                (
                    (listing.source, listing.id, listing.url, listing.title, listing.price)
                    for listing in listings
                ),
            )
            new_listings = [listing for listing in listings if listing.id in new_listing_ids]

            delivered_listing_ids: list[str] = []
            delivered_listings: list = []
            for listing in new_listings:
                if await _send_notification(bot, chat_id, listing):
                    delivered_listing_ids.append(listing.id)
                    delivered_listings.append(listing)
            await db.mark_sent_many(chat_id, source, delivered_listing_ids)
            await _run_kamernet_autoreplies(bot, chat_id, user_filters, source, delivered_listings)

            logger.info(
                "%s: %d listings found, %d new, %d sent for user %s in %.2fs",
                source,
                len(listings),
                len(new_listings),
                len(delivered_listing_ids),
                chat_id,
                time.perf_counter() - source_started_at,
            )
            return len(delivered_listing_ids)
        except Exception as exc:
            logger.error("Scraper %s failed for user %s: %s", source, chat_id, exc)
            return 0


async def _scrape_with_timing(scraper, chat_id: int) -> list:
    started_at = time.perf_counter()
    timeout = _timeout_for_source(scraper.SOURCE)
    try:
        listings = await asyncio.wait_for(scraper.scrape(), timeout=timeout)
    except TimeoutError:
        logger.error("%s scrape timed out for user %s after %ss", scraper.SOURCE, chat_id, timeout)
        return []

    logger.info(
        "%s scrape finished for user %s: %d matching listings in %.2fs",
        scraper.SOURCE,
        chat_id,
        len(listings),
        time.perf_counter() - started_at,
    )
    return listings


def _normalize_sources(sources: Iterable[str] | None) -> tuple[str, ...]:
    raw_sources = enabled_all_sources() if sources is None else sources
    normalized: list[str] = []
    for source in raw_sources:
        source_name = str(source).strip().lower()
        if source_name == ROOFZ_SOURCE and not config.ROOFZ_ENABLED:
            continue
        if source_name not in ALL_SOURCES:
            raise ValueError(f"Unknown scraper source: {source}")
        if source_name not in normalized:
            normalized.append(source_name)
    return tuple(normalized)


def _forbidden_circuit_remaining(source: str, *, now: float | None = None) -> float:
    state = _FORBIDDEN_CIRCUITS.get(source)
    if state is None:
        return 0.0
    current = time.monotonic() if now is None else now
    return max(0.0, state.blocked_until - current)


def _record_forbidden_response(source: str, *, now: float | None = None) -> tuple[int, int]:
    current = time.monotonic() if now is None else now
    state = _FORBIDDEN_CIRCUITS.setdefault(source, _ForbiddenCircuitState())
    state.consecutive_failures += 1
    if state.consecutive_failures < config.FORBIDDEN_FAILURE_THRESHOLD:
        return state.consecutive_failures, 0

    backoff_index = min(state.cooldown_count, len(config.FORBIDDEN_BACKOFF_SECONDS) - 1)
    cooldown_seconds = config.FORBIDDEN_BACKOFF_SECONDS[backoff_index]
    state.cooldown_count += 1
    state.blocked_until = current + cooldown_seconds
    return state.consecutive_failures, cooldown_seconds


def _record_source_success(source: str) -> bool:
    return _FORBIDDEN_CIRCUITS.pop(source, None) is not None


def _build_scraper(source: str, user_filters: dict):
    preferences = normalize_kamernet_property_types(
        user_filters.get("kamernet_property_type", "any")
    )
    shared_property_types = tuple(
        preference
        for preference in preferences
        if preference == "apartment"
    )
    furnished = "furnished" in preferences
    common_kwargs = {
        "city": user_filters["city"],
        "max_price": user_filters["max_price"],
        "min_bedrooms": user_filters["min_bedrooms"],
        "min_size_m2": user_filters["min_size_m2"],
    }
    if source == PARARIUS_SOURCE:
        return ParariusScraper(
            **common_kwargs,
            property_types=shared_property_types,
            furnished=furnished,
        )
    if source == FUNDA_SOURCE:
        return FundaScraper(
            **common_kwargs,
            property_types=shared_property_types,
        )
    if source == KAMERNET_SOURCE:
        return KamernetScraper(
            **common_kwargs,
            property_type=user_filters.get("kamernet_property_type", "any"),
        )
    if source == HUURWONINGEN_SOURCE:
        return HuurwoningenScraper(
            **common_kwargs,
            property_types=shared_property_types,
            furnished=furnished,
        )
    if source == VVA_SOURCE:
        return VVAScraper(**common_kwargs)
    if source == ROOFZ_SOURCE:
        return RoofzScraper(**common_kwargs)
    raise ValueError(f"Unknown scraper source: {source}")


async def _run_kamernet_autoreplies(
    bot: Bot,
    chat_id: int,
    user_filters: dict,
    source: str,
    new_listings: list,
) -> None:
    if source != KAMERNET_SOURCE or not new_listings:
        return
    if not user_filters.get("kamernet_autoreply_enabled"):
        return

    attempts = 0
    max_attempts = max(0, config.KAMERNET_AUTOREPLY_MAX_PER_SCAN)
    if max_attempts == 0:
        return

    for listing in new_listings:
        latest_filters = await db.get_filters(chat_id)
        if not _kamernet_autoreply_is_enabled(latest_filters):
            logger.info("Kamernet auto-reply disabled before listing %s for user %s.", listing.id, chat_id)
            return

        if attempts >= max_attempts:
            logger.info("Kamernet auto-reply max per scan reached for user %s.", chat_id)
            return

        message = _format_kamernet_autoreply_message(latest_filters, listing)
        reserved = await db.reserve_kamernet_auto_reply(
            chat_id,
            listing.id,
            listing.url,
            listing.title,
        )
        if not reserved:
            continue

        attempts += 1
        result = await send_kamernet_autoreply(listing, message)
        await db.update_kamernet_auto_reply(chat_id, listing.id, result.status, result.detail)
        await _send_kamernet_autoreply_status(bot, chat_id, listing, result)


def _kamernet_autoreply_is_enabled(user_filters: dict | None) -> bool:
    if not user_filters:
        return False
    if user_filters.get("setup_in_progress") or not user_filters.get("active"):
        return False
    return bool(user_filters.get("kamernet_autoreply_enabled"))


def _format_kamernet_autoreply_message(user_filters: dict, listing) -> str:
    template = (
        user_filters.get("kamernet_autoreply_template")
        or config.KAMERNET_AUTOREPLY_DEFAULT_TEMPLATE
    ).strip()
    values = _FormatValues(
        title=listing.title,
        price=listing.price,
        address=listing.address,
        url=listing.url,
        city=user_filters.get("city", ""),
        rooms=listing.rooms or "",
        size=listing.size_m2 or "",
    )
    try:
        return template.format_map(values).strip()
    except ValueError:
        logger.warning("Kamernet auto-reply template has invalid braces; using it literally.")
        return template


class _FormatValues(dict):
    def __missing__(self, key):
        return "{" + key + "}"


async def _send_kamernet_autoreply_status(
    bot: Bot,
    chat_id: int,
    listing,
    result: KamernetAutoReplyResult,
) -> None:
    if result.sent:
        logger.info("Kamernet auto-reply %s for listing %s.", result.status, listing.id)
        return
    if result.status == "dry_run":
        logger.info("Kamernet auto-reply dry run for listing %s.", listing.id)
        return

    logger.warning(
        "Kamernet auto-reply failed for listing %s with status=%s detail=%s",
        listing.id,
        result.status,
        result.detail,
    )
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "<b>Kamernet auto-reply failed</b>\n\n"
                f"{escape(listing.title)}\n"
                f"Status: {escape(result.status)}\n"
                f"{escape(result.detail)}\n\n"
                f'<a href="{escape(listing.url)}">Open listing</a>'
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
    except Exception as exc:
        logger.error("Kamernet auto-reply status notification failed for %s: %s", chat_id, exc)


def _scan_lock(chat_id: int, source: str) -> asyncio.Lock:
    key = (chat_id, source)
    lock = _SCAN_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SCAN_LOCKS[key] = lock
    return lock


def _timeout_for_source(source: str) -> int:
    if source == PARARIUS_SOURCE:
        return config.PARARIUS_SCRAPER_TIMEOUT_SECONDS
    if source == FUNDA_SOURCE:
        return config.FUNDA_SCRAPER_TIMEOUT_SECONDS
    if source == ROOFZ_SOURCE:
        return config.ROOFZ_SCRAPER_TIMEOUT_SECONDS
    return config.SCRAPER_TIMEOUT_SECONDS


async def _scan_is_current(chat_id: int, user_filters: dict, require_active: bool) -> bool:
    latest_filters = await db.get_filters(chat_id)
    if not latest_filters or latest_filters.get("setup_in_progress"):
        return False
    if require_active and not latest_filters["active"]:
        return False
    return all(
        latest_filters.get(key) == user_filters.get(key)
        for key in _FILTER_MATCH_KEYS
    )


async def _send_notification(bot: Bot, chat_id: int, listing) -> bool:
    source = listing.source.capitalize()
    parts = [
        f"<b>{escape(listing.title)}</b>",
        f"Address: {escape(listing.address)}",
        f"Rent: {escape(listing.price)}",
    ]
    if listing.rooms:
        parts.append(f"Bedrooms/rooms: {escape(listing.rooms)}")
    if listing.size_m2:
        parts.append(f"Size: {escape(listing.size_m2)}")
    parts.append(f'\n<a href="{escape(listing.url)}">View listing</a>')

    text = f"<b>New on {escape(source)}</b>\n\n" + "\n".join(parts)

    try:
        if listing.image_url:
            await bot.send_photo(
                chat_id=chat_id,
                photo=listing.image_url,
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
        )
        return True
    except Exception as exc:
        if not listing.image_url:
            logger.error("Notification failed for %s: %s", chat_id, exc)
            return False

        logger.warning("Photo send failed (%s), retrying as text: %s", chat_id, exc)
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            return True
        except Exception as exc2:
            logger.error("Notification failed for %s: %s", chat_id, exc2)
            return False
