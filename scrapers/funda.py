import asyncio
import logging
import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlparse

import config
from .base import BaseScraper, Listing, parse_first_int

logger = logging.getLogger(__name__)

FUNDA_BASE_URL = "https://www.funda.nl"
_FUNDA_THREAD_SEMAPHORE = asyncio.BoundedSemaphore(
    max(1, config.FUNDA_MAX_BACKGROUND_THREADS)
)


class FundaScraper(BaseScraper):
    SOURCE = "funda"

    async def scrape(self) -> list[Listing]:
        try:
            from funda import Funda
        except ImportError:
            logger.error("Funda: pyfunda is not installed. Run: uv sync --locked")
            return []

        try:
            listings = await self._scrape_in_background(Funda)
            listings = [listing for listing in listings if self._matches_filters(listing)]
            logger.info("Funda: found %d matching listings", len(listings))
            return listings
        except Exception as exc:
            logger.error("Funda scrape error: %s", exc)
            return []

    async def _scrape_in_background(self, client_cls) -> list[Listing]:
        try:
            await asyncio.wait_for(
                _FUNDA_THREAD_SEMAPHORE.acquire(),
                timeout=config.FUNDA_SCRAPER_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("Funda skipped because a previous pyfunda call is still running.")
            return []

        thread_task = asyncio.create_task(asyncio.to_thread(self._scrape_sync, client_cls))
        try:
            return await asyncio.wait_for(
                asyncio.shield(thread_task),
                timeout=config.FUNDA_PYFUNDA_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.error(
                "Funda pyfunda call timed out after %ss; worker will finish in background.",
                config.FUNDA_PYFUNDA_TIMEOUT_SECONDS,
            )
            return []
        finally:
            if thread_task.done():
                _release_funda_thread_slot()
            else:
                thread_task.add_done_callback(lambda _: _release_funda_thread_slot())

    def _scrape_sync(self, client_cls) -> list[Listing]:
        filters: dict[str, object] = {
            "category": "rent",
            "sort": "newest",
        }
        if self.max_price:
            filters["max_price"] = self.max_price
        if self.min_bedrooms:
            filters["min_bedrooms"] = self.min_bedrooms
        if self.min_size_m2:
            filters["min_area"] = self.min_size_m2
        if self.property_types:
            filters["object_type"] = (
                self.property_types[0]
                if len(self.property_types) == 1
                else list(self.property_types)
            )

        max_pages = max(1, config.FUNDA_MAX_PAGES_PER_SCAN)
        with client_cls(
            timeout=config.FUNDA_PYFUNDA_TIMEOUT_SECONDS,
            max_retries=max(0, config.FUNDA_PYFUNDA_MAX_RETRIES),
            retry_backoff=max(0, config.FUNDA_PYFUNDA_RETRY_BACKOFF_SECONDS),
        ) as client:
            raw_listings = list(
                client.iter_search(self.city.lower(), max_pages=max_pages, **filters)
            )

        listings: list[Listing] = []
        seen_ids: set[str] = set()
        for raw_listing in raw_listings:
            listing = self._convert_listing(raw_listing)
            if not listing or listing.id in seen_ids:
                continue
            seen_ids.add(listing.id)
            listings.append(listing)
        return listings

    def _convert_listing(self, raw_listing) -> Listing | None:
        url = _listing_url(raw_listing)
        listing_id = _listing_id(raw_listing, url)
        if not listing_id:
            return None

        title = _first_text(getattr(raw_listing, "title", None)) or f"Funda listing {listing_id}"
        city = _first_text(getattr(raw_listing, "city", None))
        address = _address(title, city)

        price_obj = getattr(raw_listing, "price", None)
        price_eur = _as_int(getattr(price_obj, "amount", None))
        price = _first_text(getattr(price_obj, "formatted", None))
        if not price and price_eur:
            price = f"EUR {price_eur}"

        rooms_count = _as_int(getattr(raw_listing, "rooms_count", None))
        bedrooms_count = _as_int(getattr(raw_listing, "bedrooms", None))
        rooms_label = _rooms_label(rooms_count, bedrooms_count)

        size_value = _as_int(getattr(raw_listing, "living_area", None))
        size_label = f"{size_value} m2" if size_value else None
        property_details = getattr(raw_listing, "property_details", None)
        property_type = _normalize_funda_property_type(
            getattr(property_details, "object_type", None)
        )

        return Listing(
            id=listing_id,
            source=self.SOURCE,
            title=title,
            price=price,
            address=address,
            url=url or _fallback_listing_url(listing_id),
            image_url=_first_photo_url(getattr(raw_listing, "media", None)),
            rooms=rooms_label,
            size_m2=size_label,
            price_eur=price_eur,
            bedrooms=bedrooms_count,
            size_m2_value=size_value,
            property_type=property_type,
        )


def _listing_url(raw_listing) -> str:
    urls = getattr(raw_listing, "urls", None)
    full_url = _first_text(
        getattr(raw_listing, "url", None),
        getattr(urls, "full", None),
        getattr(urls, "share", None),
    )
    if full_url:
        return _absolute_funda_url(full_url)

    path = _first_text(getattr(raw_listing, "detail_url", None), getattr(urls, "path", None))
    return _absolute_funda_url(path)


def _absolute_funda_url(url: str) -> str:
    text = _first_text(url)
    if not text:
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    if text.startswith("www.funda.nl"):
        return f"https://{text}"

    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        return text
    return urljoin(f"{FUNDA_BASE_URL}/", text.lstrip("/"))


def _fallback_listing_url(listing_id: str) -> str:
    return f"{FUNDA_BASE_URL}/detail/{listing_id}/"


def _listing_id(raw_listing, url: str) -> str:
    url_id = _id_from_url(url)
    if url_id:
        return url_id

    for value in (
        getattr(raw_listing, "tiny_id", None),
        getattr(raw_listing, "global_id", None),
        getattr(raw_listing, "id", None),
    ):
        text = _first_text(value)
        if text:
            return text
    return ""


def _id_from_url(url: str) -> str:
    if not url:
        return ""
    matches = re.findall(r"\d{7,9}", url)
    return matches[-1] if matches else ""


def _first_text(*values) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _as_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return parse_first_int(str(value))


def _address(title: str, city: str) -> str:
    if city and city.lower() not in title.lower():
        return f"{title}, {city}"
    return title


def _rooms_label(rooms_count: int | None, bedrooms_count: int | None) -> str | None:
    if rooms_count and bedrooms_count and rooms_count != bedrooms_count:
        return f"{_count_label(rooms_count, 'room')}, {_count_label(bedrooms_count, 'bedroom')}"
    if rooms_count:
        return _count_label(rooms_count, "room")
    if bedrooms_count:
        return _count_label(bedrooms_count, "bedroom")
    return None


def _count_label(count: int, noun: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {noun}{suffix}"


def _first_photo_url(media) -> str | None:
    if not media:
        return None

    photo_urls = getattr(media, "photo_urls", None)
    if isinstance(photo_urls, str):
        return photo_urls or None
    if isinstance(photo_urls, Iterable):
        for photo_url in photo_urls:
            text = _first_text(photo_url)
            if text:
                return text
    return None


def _release_funda_thread_slot() -> None:
    try:
        _FUNDA_THREAD_SEMAPHORE.release()
    except ValueError:
        logger.warning("Funda thread slot release was ignored because no slot was held.")


def _normalize_funda_property_type(value) -> str | None:
    text = _first_text(value).lower()
    return {
        "apartment": "apartment",
        "appartement": "apartment",
        "house": "house",
        "huis": "house",
    }.get(text)
