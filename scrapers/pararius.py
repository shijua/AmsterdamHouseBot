import asyncio
import logging
import re
from urllib.parse import urljoin

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
    logger.warning("curl_cffi is not installed; Pararius may return 403 responses.")

_HEADERS = {
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
}

_DETAIL_URL_RE = re.compile(
    r"/(?:appartement|huis|kamer|studio)-te-huur/[^/]+/|"
    r"/(?:apartment|house|room|studio)-for-rent/[^/]+/",
    re.I,
)
_LISTING_ID_RE = re.compile(r"/([0-9a-f]{8})(?:/|$)", re.I)


class ParariusScraper(BaseScraper):
    SOURCE = "pararius"
    BASE_URL = "https://www.pararius.nl"
    LATEST_RENTALS_URL = f"{BASE_URL}/huurwoningen/nederland"

    def _build_url(self) -> str:
        city_slug = self.city.lower().replace(" ", "-")
        price_segment = f"/0-{self.max_price}" if self.max_price else ""
        return f"{self.BASE_URL}/huurwoningen/{city_slug}{price_segment}"

    async def scrape(self) -> list[Listing]:
        try:
            pages = await self._fetch_pages()
            listings = self._parse_pages(pages)
            logger.info(
                "Pararius: found %d matching listings from %d public pages",
                len(listings),
                len(pages),
            )
            return listings
        except Exception as exc:
            logger.error("Pararius scrape error: %s", exc)
            raise

    async def _fetch_pages(self) -> list[tuple[str, str]]:
        page_specs = (
            ("latest", self.LATEST_RENTALS_URL),
            ("city", self._build_url()),
        )
        if _USE_CURL:
            session = await get_shared_session(
                self.SOURCE,
                lambda: CurlAsyncSession(impersonate="chrome124"),
            )
            return await self._fetch_with_session(session, page_specs)

        client = await get_httpx_client(self.SOURCE, timeout=30, follow_redirects=True)
        return await self._fetch_with_session(client, page_specs)

    async def _fetch_with_session(self, session, page_specs: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
        tasks = [
            asyncio.create_task(self._fetch_page(session, label, url))
            for label, url in page_specs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        pages: list[tuple[str, str]] = []
        errors: list[Exception] = []
        saw_forbidden = False
        for (label, url), result in zip(page_specs, results, strict=True):
            if isinstance(result, Exception):
                errors.append(result)
                saw_forbidden = saw_forbidden or _is_forbidden_error(result)
                logger.warning("Pararius %s page failed from %s: %s", label, url, result)
                continue
            pages.append((label, result))
        if saw_forbidden:
            await self._reset_shared_transport_after_forbidden()
            if not pages:
                raise ForbiddenResponseError(self.SOURCE)
        if not pages and errors:
            raise errors[0]
        return pages

    async def _fetch_page(self, session, label: str, url: str) -> str:
        if _USE_CURL:
            response = await session.get(url, headers=_HEADERS, timeout=30)
        else:
            response = await session.get(url, headers=_HEADERS)
        response.raise_for_status()
        logger.info("Pararius %s page fetched from %s", label, url)
        return response.text

    async def _reset_shared_transport_after_forbidden(self) -> None:
        if _USE_CURL:
            await close_shared_session(self.SOURCE)
        else:
            await close_httpx_client(self.SOURCE)
        logger.info("Pararius shared HTTP transport reset after a 403 response.")

    def _parse_pages(self, pages: list[tuple[str, str]]) -> list[Listing]:
        listings: list[Listing] = []
        seen_ids: set[str] = set()
        raw_counts: dict[str, int] = {}

        for label, html in pages:
            page_listings = self._parse_html(html)
            raw_counts[label] = len(page_listings)
            for listing in page_listings:
                if listing.id in seen_ids:
                    continue
                if not self._matches_city(listing):
                    continue
                if not self._matches_filters(listing):
                    continue
                seen_ids.add(listing.id)
                listings.append(listing)

        logger.info("Pararius raw listings by page: %s", raw_counts)
        return listings

    def _parse_html(self, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "lxml")
        listings: list[Listing] = []
        seen_ids: set[str] = set()

        for article in self._article_candidates(soup):
            listing = self._parse_article(article)
            if not listing or listing.id in seen_ids:
                continue
            seen_ids.add(listing.id)
            listings.append(listing)
        return listings

    def _article_candidates(self, soup: BeautifulSoup) -> list:
        candidates = soup.select(
            "section.listing-search-item, article, li[class*='listing'], div[class*='listing-search-item']"
        )
        if candidates:
            return candidates

        candidates = []
        seen_containers: set[int] = set()
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            if not _DETAIL_URL_RE.search(href):
                continue
            container = link.find_parent(["article", "section", "li"]) or link.parent
            if not container or id(container) in seen_containers:
                continue
            seen_containers.add(id(container))
            candidates.append(container)
        return candidates

    def _parse_article(self, article) -> Listing | None:
        try:
            link_tag = article.select_one("a.listing-search-item__link--title") or _find_detail_link(article)
            if not link_tag:
                return None

            relative_url = link_tag.get("href", "")
            full_url = urljoin(self.BASE_URL, relative_url)
            listing_id = _listing_id_from_url(relative_url)
            if not listing_id:
                return None

            title_tag = article.select_one(".listing-search-item__title")
            title = (title_tag or link_tag).get_text(" ", strip=True) or _title_from_url(relative_url)

            article_text = article.get_text(" ", strip=True)
            address_tag = article.select_one(".listing-search-item__sub-title")
            address = (
                address_tag.get_text(" ", strip=True)
                if address_tag
                else _extract_address(article_text, self.city)
            )

            price_tag = article.select_one(".listing-search-item__price")
            price = price_tag.get_text(" ", strip=True) if price_tag else _extract_price(article_text)

            rooms, bedrooms, size_label, size_value = None, None, None, None
            furnishing = None
            for feature in article.select(".listing-search-item__features li"):
                text = feature.get_text(" ", strip=True)
                lower = text.lower()
                if "m2" in lower or "m\u00b2" in lower or "m\u00c2\u00b2" in lower:
                    size_label = text
                    size_value = parse_first_int(text)
                elif "kamer" in lower or "slaapkamer" in lower:
                    rooms = text
                    bedrooms = parse_first_int(text)
                elif parsed_furnishing := parse_furnishing(text):
                    furnishing = parsed_furnishing

            if not size_label:
                size_value = _extract_size(article_text)
                size_label = f"{size_value} m2" if size_value else None
            if not rooms:
                bedrooms = _extract_rooms(article_text)
                rooms = f"{bedrooms} kamers" if bedrooms else None

            image = article.select_one("img")

            return Listing(
                id=listing_id,
                source=self.SOURCE,
                title=title,
                price=price,
                address=address,
                url=full_url,
                image_url=_image_url(image),
                rooms=rooms,
                size_m2=size_label,
                price_eur=parse_euro_amount(price),
                bedrooms=bedrooms,
                size_m2_value=size_value,
                property_type=_property_type_from_url(relative_url),
                furnishing=furnishing,
            )
        except Exception as exc:
            logger.warning("Failed to parse Pararius article: %s", exc)
            return None

    def _matches_city(self, listing: Listing) -> bool:
        city = self.city.lower()
        searchable_text = " ".join((listing.title, listing.address, listing.url)).lower()
        return city in searchable_text


def _find_detail_link(article):
    links = [
        link
        for link in article.select("a[href]")
        if _DETAIL_URL_RE.search(link.get("href", ""))
    ]
    if not links:
        return None
    return max(links, key=lambda link: len(link.get_text(" ", strip=True)))


def _listing_id_from_url(url: str) -> str:
    match = _LISTING_ID_RE.search(url)
    if match:
        return match.group(1)
    return url.rstrip("/").split("/")[-1]


def _property_type_from_url(url: str) -> str | None:
    match = re.search(
        r"/(?:appartement|apartment|huis|house|kamer|room|studio)-(?:te-huur|for-rent)/",
        url,
        re.I,
    )
    if not match:
        return None
    value = match.group(0).split("-", 1)[0].strip("/").lower()
    return {
        "appartement": "apartment",
        "apartment": "apartment",
        "huis": "house",
        "house": "house",
        "kamer": "room",
        "room": "room",
        "studio": "studio",
    }.get(value)


def _title_from_url(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title() if slug else "Pararius listing"


def _extract_address(text: str, city: str) -> str:
    match = re.search(
        r"\b\d{4}\s?[A-Z]{2}\s+.+?(?=\s+(?:\u20ac|EUR)|\s+\d+\s*m(?:2|\u00b2)|$)",
        text,
        re.I,
    )
    if match:
        return " ".join(match.group(0).split())
    return city


def _extract_price(text: str) -> str:
    match = re.search(r"(?:\u20ac|EUR)\s*[\d.,\s]+(?:per\s+maand|pcm|p/m|/month)?", text, re.I)
    return " ".join(match.group(0).split()) if match else ""


def _extract_size(text: str) -> int | None:
    match = re.search(r"(\d+)\s*m(?:2|\u00b2)", text, re.I)
    return int(match.group(1)) if match else None


def _extract_rooms(text: str) -> int | None:
    match = re.search(r"(\d+)\s*(?:kamers?|slaapkamers?)", text, re.I)
    return int(match.group(1)) if match else None


def _image_url(image) -> str | None:
    if not image:
        return None
    return image.get("src") or image.get("data-src") or image.get("data-lazy-src")


def _is_forbidden_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 403:
        return True
    return "HTTP Error 403" in str(exc) or "403 Forbidden" in str(exc)
