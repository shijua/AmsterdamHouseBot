import asyncio
import json
import logging
import re
from collections.abc import Iterable
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

import config
from .base import BaseScraper, Listing, parse_euro_amount, parse_first_int
from .http_clients import get_httpx_client

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://kamernet.nl/",
}

KAMERNET_SEARCH_RADIUS_KM = 5
KAMERNET_DEFAULT_SEARCH_CATEGORIES = "1,2,4,8"
KAMERNET_PROPERTY_TYPE_LABELS = {
    "any": "Any property type",
    "room": "Room",
    "apartment": "Apartment",
    "studio": "Studio",
    "anti_squat": "Anti-squat",
    "student_housing": "Student Housing",
    "furnished": "Furnished",
    "short_term": "Short Term",
    "long_term": "Long Term",
}

_SEARCH_CATEGORIES_BY_PROPERTY_TYPE = {
    "room": "1",
    "apartment": "2",
    "studio": "4",
    "anti_squat": "8",
    "student_housing": "16",
    "furnished": "17",
    "short_term": "18",
    "long_term": "19",
}

_DETAIL_TYPE_BY_LISTING_TYPE = {
    1: "room",
    2: "apartment",
    4: "studio",
    8: "anti_squat",
}

_LOCAL_PROPERTY_TYPES = frozenset(_DETAIL_TYPE_BY_LISTING_TYPE.values())
_FURNISHED_ID = 4


class KamernetScraper(BaseScraper):
    SOURCE = "kamernet"
    BASE_URL = "https://kamernet.nl"

    def __init__(
        self,
        city: str = "Amsterdam",
        max_price: int = 2000,
        min_bedrooms: int = 1,
        min_size_m2: int = 0,
        property_type: str | Iterable[str] = "any",
    ):
        self.search_preferences = normalize_kamernet_property_types(property_type)
        local_property_types = tuple(
            preference
            for preference in self.search_preferences
            if preference in _LOCAL_PROPERTY_TYPES
        )
        super().__init__(
            city,
            max_price,
            min_bedrooms,
            min_size_m2,
            property_types=local_property_types,
            furnished="furnished" in self.search_preferences,
        )

    def _build_url(self, page_no: int = 1) -> str:
        city_slug = self.city.lower().replace(" ", "-")
        params = {
            "radius": KAMERNET_SEARCH_RADIUS_KM,
            "pageNo": max(1, int(page_no)),
        }
        search_categories = _search_categories_for_property_types(self.search_preferences)
        if search_categories:
            params["searchCategories"] = search_categories
        if self.max_price:
            params["maxRent"] = self.max_price
        if self.min_size_m2:
            params["minSize"] = self.min_size_m2
        return f"{self.BASE_URL}/en/for-rent/properties-{city_slug}?{urlencode(params)}"

    async def scrape(self) -> list[Listing]:
        try:
            pages = await self._fetch_pages()
            listings = self._parse_pages(pages)
            raw_count = len(listings)
            listings = [listing for listing in listings if self._matches_filters(listing)]
            logger.info(
                "Kamernet: %d raw listings, %d matching listings from %d page(s); first page %s",
                raw_count,
                len(listings),
                len(pages),
                self._build_url(),
            )
            return listings
        except Exception as exc:
            logger.error("Kamernet scrape error: %s", exc)
            return []

    async def _fetch_pages(self) -> list[tuple[str, str]]:
        max_pages = max(1, config.KAMERNET_MAX_PAGES_PER_SCAN)
        urls = [self._build_url(page_no) for page_no in range(1, max_pages + 1)]
        client = await get_httpx_client(self.SOURCE, timeout=30, follow_redirects=True)

        first_html = await self._fetch_page(client, urls[0])
        rest_urls = urls[1:]
        tasks = [
            asyncio.create_task(self._fetch_page(client, url))
            for url in rest_urls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        pages = [(urls[0], first_html)]
        for url, result in zip(rest_urls, results, strict=True):
            if isinstance(result, Exception):
                logger.warning("Kamernet page failed from %s: %s", url, result)
                continue
            pages.append((url, result))
        return pages

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> str:
        response = await client.get(url, headers=_HEADERS)
        response.raise_for_status()
        logger.info("Kamernet page fetched from %s", url)
        return response.text

    def _parse_pages(self, pages: list[tuple[str, str]]) -> list[Listing]:
        listings: list[Listing] = []
        seen_ids: set[str] = set()
        raw_counts: dict[str, int] = {}

        for url, html in pages:
            page_listings = self._parse_html(html)
            raw_counts[url] = len(page_listings)
            for listing in page_listings:
                if listing.id in seen_ids:
                    continue
                seen_ids.add(listing.id)
                listings.append(listing)

        logger.info("Kamernet raw listings by page: %s", raw_counts)
        return listings

    def _parse_html(self, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "lxml")
        listings = []
        next_data = soup.select_one("script#__NEXT_DATA__")
        if next_data and next_data.string:
            listings = self._parse_next_data(json.loads(next_data.string))
        if not listings:
            listings = self._parse_html_fallback(soup)
        return listings

    def _parse_next_data(self, data: dict) -> list[Listing]:
        page_props = data.get("props", {}).get("pageProps", {})
        results = _find_listing_items(page_props)
        listings = []
        seen_ids: set[str] = set()
        for item in results:
            listing = self._parse_item(item)
            if not listing or listing.id in seen_ids:
                continue
            seen_ids.add(listing.id)
            listings.append(listing)
        return listings

    def _parse_item(self, item: dict) -> Listing | None:
        try:
            listing_id = str(item.get("id") or item.get("listingId") or "")
            if not listing_id:
                return None

            url_path = (
                item.get("url")
                or item.get("urlKey")
                or item.get("detailUrl")
                or _build_detail_url_path(item, self.city, listing_id)
            )
            full_url = f"{self.BASE_URL}{url_path}" if url_path.startswith("/") else url_path

            price_value = (
                item.get("totalRentalPrice")
                or item.get("rentalPrice")
                or item.get("price")
                or item.get("rent")
            )
            price_eur = int(price_value) if isinstance(price_value, (int, float)) else parse_euro_amount(str(price_value))
            price = f"EUR {price_eur}/month" if price_eur else "Price unavailable"

            bedrooms = _first_present_int(item, "roomCount", "numberOfRooms", "rooms")
            size_value = _first_present_int(item, "surfaceArea", "area", "surface")
            street = item.get("street") or item.get("address") or ""
            city = item.get("city") or self.city
            title = item.get("title") or street or f"Kamernet listing {listing_id}"
            address = f"{street}, {city}" if street else city
            image_url = _pick_image_url(item)
            listing_type = _first_present_int(item, "listingType")
            furnishing_id = _first_present_int(item, "furnishingId")

            return Listing(
                id=listing_id,
                source=self.SOURCE,
                title=title,
                price=price,
                address=address,
                url=full_url,
                image_url=image_url,
                rooms=f"{bedrooms} rooms" if bedrooms else None,
                size_m2=f"{size_value} m2" if size_value else None,
                price_eur=price_eur,
                bedrooms=bedrooms,
                size_m2_value=size_value,
                property_type=_DETAIL_TYPE_BY_LISTING_TYPE.get(listing_type),
                furnishing="furnished" if furnishing_id == _FURNISHED_ID else None,
            )
        except Exception as exc:
            logger.warning("Kamernet item parse error: %s", exc)
            return None

    def _parse_html_fallback(self, soup: BeautifulSoup) -> list[Listing]:
        listings: list[Listing] = []
        seen_ids: set[str] = set()
        for link in soup.select("a[href*='/en/for-rent/']"):
            href = link.get("href", "")
            match = re.search(r"-(\d{5,})/?$", href)
            if not match:
                continue

            listing_id = match.group(1)
            if listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)

            full_url = f"{self.BASE_URL}{href}" if href.startswith("/") else href
            text = link.get_text(" ", strip=True)
            price_eur = parse_euro_amount(text)
            size_value = parse_first_int(text.split("m2", 1)[0]) if "m2" in text.lower() else None
            listings.append(
                Listing(
                    id=listing_id,
                    source=self.SOURCE,
                    title=text[:80] or f"Kamernet listing {listing_id}",
                    price=f"EUR {price_eur}/month" if price_eur else "",
                    address=self.city,
                    url=full_url,
                    price_eur=price_eur,
                    size_m2=f"{size_value} m2" if size_value else None,
                    size_m2_value=size_value,
                    property_type=_property_type_from_url(href),
                )
            )
        return listings


def normalize_kamernet_property_types(property_types: str | Iterable[str] | None) -> tuple[str, ...]:
    if property_types is None:
        return ("any",)

    values: Iterable[str]
    if isinstance(property_types, str):
        stripped = property_types.strip()
        if not stripped:
            return ("any",)
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                values = parsed if isinstance(parsed, list) else [stripped]
            except json.JSONDecodeError:
                values = [stripped]
        else:
            values = re.split(r"[,;\n]+", stripped)
    else:
        values = property_types

    normalized: list[str] = []
    for value in values:
        key = str(value).strip()
        if not key:
            continue
        if key in KAMERNET_PROPERTY_TYPE_LABELS and key not in normalized:
            normalized.append(key)

    if not normalized or "any" in normalized:
        return ("any",)
    return tuple(normalized)


def serialize_kamernet_property_types(property_types: str | Iterable[str] | None) -> str:
    return ",".join(normalize_kamernet_property_types(property_types))


def format_kamernet_property_types(property_types: str | Iterable[str] | None) -> str:
    return ", ".join(
        KAMERNET_PROPERTY_TYPE_LABELS[property_type]
        for property_type in normalize_kamernet_property_types(property_types)
    )


def _search_categories_for_property_types(property_types: str | Iterable[str] | None) -> str:
    normalized = normalize_kamernet_property_types(property_types)
    if normalized == ("any",):
        return KAMERNET_DEFAULT_SEARCH_CATEGORIES

    categories = [
        _SEARCH_CATEGORIES_BY_PROPERTY_TYPE[property_type]
        for property_type in normalized
        if property_type in _SEARCH_CATEGORIES_BY_PROPERTY_TYPE
    ]
    return ",".join(categories) or KAMERNET_DEFAULT_SEARCH_CATEGORIES


def _first_present_int(item: dict, *keys: str) -> int | None:
    for key in keys:
        value = item.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                parsed = parse_first_int(str(value))
                if parsed is not None:
                    return parsed
    return None


def _pick_image_url(item: dict) -> str | None:
    image = (
        item.get("imageUrl")
        or item.get("mainImageUrl")
        or item.get("resizedFullPreviewImageUrl")
        or item.get("fullPreviewImageUrl")
        or item.get("thumbnailUrl")
        or item.get("image")
    )
    if isinstance(image, dict):
        image = image.get("url") or image.get("src")
    if image:
        return str(image)

    images = item.get("images") or []
    if not images:
        return None
    first = images[0]
    if isinstance(first, dict):
        return first.get("url") or first.get("src")
    return str(first)


def _find_listing_items(page_props: dict) -> list[dict]:
    legacy_results = (
        page_props.get("tiles")
        or page_props.get("listings")
        or page_props.get("searchResult", {}).get("results", [])
        or page_props.get("results")
        or []
    )
    if legacy_results:
        return legacy_results

    response = page_props.get("targetPageProps", {}).get("findListingsResponse", {})
    results: list[dict] = []
    for key in ("topAdListings", "listings"):
        items = response.get(key) or []
        if isinstance(items, list):
            results.extend(items)
    return results


def _build_detail_url_path(item: dict, city: str, listing_id: str) -> str:
    listing_type = _first_present_int(item, "listingType")
    type_slug = _DETAIL_TYPE_BY_LISTING_TYPE.get(listing_type, "apartment").replace("_", "-")
    city_slug = item.get("citySlug") or city.lower().replace(" ", "-")
    street_slug = item.get("streetSlug") or city_slug
    return f"/en/for-rent/{type_slug}-{city_slug}/{street_slug}/{type_slug}-{listing_id}"


def _property_type_from_url(url: str) -> str | None:
    match = re.search(r"/for-rent/(room|apartment|studio|anti-squat)-", url, re.I)
    return match.group(1).lower().replace("-", "_") if match else None
