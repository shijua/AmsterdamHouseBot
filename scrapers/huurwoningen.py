import logging
import re
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from .base import (
    BaseScraper,
    ForbiddenResponseError,
    Listing,
    parse_euro_amount,
    parse_first_int,
    parse_furnishing,
)
from .http_clients import close_httpx_client, close_shared_session, get_httpx_client, get_shared_session

logger = logging.getLogger(__name__)

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession

    _USE_CURL = True
except ImportError:
    _USE_CURL = False
    logger.warning("curl_cffi is not installed; Huurwoningen may return Cloudflare challenges.")

_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9,nl-NL;q=0.8,nl;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
}

_BEDROOM_FILTER_VALUES = (1, 2, 3, 4, 5)
_SIZE_FILTER_VALUES = (25, 50, 75, 100, 125, 150, 200)


class HuurwoningenScraper(BaseScraper):
    SOURCE = "huurwoningen"
    BASE_URL = "https://www.huurwoningen.nl"

    def _build_url(self) -> str:
        city_slug = re.sub(r"[^a-z0-9]+", "-", self.city.lower()).strip("-")
        if self.property_types == ("apartment",):
            url = f"{self.BASE_URL}/en/appartement/huren/{city_slug}/"
        else:
            url = f"{self.BASE_URL}/en/in/{city_slug}/"
        params: dict[str, str] = {}
        if self.furnished:
            params["interior"] = "gemeubileerd"
        if self.max_price:
            params["price"] = f"0-{self.max_price}"
        if self.min_bedrooms:
            bedroom_filter = _nearest_supported_min(self.min_bedrooms, _BEDROOM_FILTER_VALUES)
            if bedroom_filter:
                params["bedrooms"] = str(bedroom_filter)
        if self.min_size_m2:
            size_filter = _nearest_supported_min(self.min_size_m2, _SIZE_FILTER_VALUES)
            if size_filter:
                params["living_size"] = str(size_filter)
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    async def scrape(self) -> list[Listing]:
        try:
            url = self._build_url()
            if _USE_CURL:
                session = await get_shared_session(
                    self.SOURCE,
                    lambda: CurlAsyncSession(impersonate="chrome124"),
                )
                response = await session.get(url, headers=_HEADERS, timeout=30)
                response.raise_for_status()
                html = response.text
            else:
                client = await get_httpx_client(self.SOURCE, timeout=30, follow_redirects=True)
                response = await client.get(url, headers=_HEADERS)
                response.raise_for_status()
                html = response.text

            soup = BeautifulSoup(html, "lxml")
            listings = [
                listing
                for article in soup.select("section.listing-search-item")
                if (listing := self._parse_article(article)) and self._matches_filters(listing)
            ]
            logger.info("Huurwoningen: found %d matching listings from %s", len(listings), url)
            return listings
        except Exception as exc:
            if _is_forbidden_error(exc):
                await self._reset_shared_transport_after_forbidden()
                raise ForbiddenResponseError(self.SOURCE) from exc
            logger.error("Huurwoningen scrape error: %s", exc)
            raise

    async def _reset_shared_transport_after_forbidden(self) -> None:
        if _USE_CURL:
            await close_shared_session(self.SOURCE)
        else:
            await close_httpx_client(self.SOURCE)
        logger.info("Huurwoningen shared HTTP transport reset after a 403 response.")

    def _parse_article(self, article) -> Listing | None:
        try:
            link_tag = article.select_one("a.listing-search-item__link--title")
            if not link_tag:
                return None

            relative_url = link_tag.get("href", "")
            full_url = urljoin(self.BASE_URL, relative_url)
            listing_id = article.get("data-listing-search-item-id") or _listing_id_from_url(relative_url)
            if not listing_id:
                return None

            title_tag = article.select_one(".listing-search-item__title")
            title = (title_tag or link_tag).get_text(" ", strip=True)

            address_tag = article.select_one(".listing-search-item__sub-title")
            address = address_tag.get_text(" ", strip=True) if address_tag else self.city

            price_tag = article.select_one(".listing-search-item__price-main") or article.select_one(
                ".listing-search-item__price"
            )
            price = price_tag.get_text(" ", strip=True) if price_tag else ""

            rooms, bedrooms, size_label, size_value = None, None, None, None
            furnishing = None
            for feature in article.select(".listing-search-item__features li"):
                text = feature.get_text(" ", strip=True)
                lower = text.lower()
                if "m2" in lower or "m²" in lower:
                    size_label = text
                    size_value = parse_first_int(text)
                elif "room" in lower or "bedroom" in lower or "kamer" in lower:
                    rooms = text
                    bedrooms = parse_first_int(text)
                elif parsed_furnishing := parse_furnishing(text):
                    furnishing = parsed_furnishing

            return Listing(
                id=listing_id,
                source=self.SOURCE,
                title=title,
                price=price,
                address=address,
                url=full_url,
                image_url=_pick_image_url(article),
                rooms=rooms,
                size_m2=size_label,
                price_eur=parse_euro_amount(price),
                bedrooms=bedrooms,
                size_m2_value=size_value,
                property_type=_property_type_from_listing(title, relative_url),
                furnishing=furnishing,
            )
        except Exception as exc:
            logger.warning("Failed to parse Huurwoningen article: %s", exc)
            return None


def _nearest_supported_min(value: int, supported_values: tuple[int, ...]) -> int | None:
    matches = [candidate for candidate in supported_values if candidate <= value]
    return matches[-1] if matches else None


def _listing_id_from_url(url: str) -> str | None:
    match = re.search(r"/([0-9a-f]{8})(?:/|$)", url)
    return match.group(1) if match else None


def _property_type_from_listing(title: str, url: str) -> str | None:
    text = f"{title} {url}".lower()
    if re.search(r"\b(?:flat|apartment|appartement)\b", text):
        return "apartment"
    if re.search(r"\b(?:house|huis)\b", text):
        return "house"
    if re.search(r"\b(?:room|kamer)\b", text):
        return "room"
    if re.search(r"\bstudio\b", text):
        return "studio"
    return None


def _pick_image_url(article) -> str | None:
    image = article.select_one("img[src]")
    if image:
        return image.get("src")

    source = article.select_one("source[srcset]")
    if not source:
        return None

    first_candidate = source.get("srcset", "").split(",", 1)[0].strip()
    return first_candidate.split(" ", 1)[0] if first_candidate else None


def _is_forbidden_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 403:
        return True
    return "HTTP Error 403" in str(exc) or "403 Forbidden" in str(exc)
