import json
import unittest
from unittest.mock import AsyncMock, patch

from scrapers.kamernet import KamernetScraper


def _next_data_html(items: list[dict]) -> str:
    data = {
        "props": {
            "pageProps": {
                "targetPageProps": {
                    "findListingsResponse": {
                        "listings": items,
                    }
                }
            }
        }
    }
    return (
        "<html><body>"
        f"<script id=\"__NEXT_DATA__\" type=\"application/json\">{json.dumps(data)}</script>"
        "</body></html>"
    )


def _listing_item(listing_id: str, street: str, price: int = 1650, **overrides) -> dict:
    item = {
        "id": listing_id,
        "street": street,
        "city": "Amsterdam",
        "listingType": 4,
        "totalRentalPrice": price,
        "surfaceArea": 32,
        "url": f"/en/for-rent/studio-amsterdam/{street.lower().replace(' ', '-')}/studio-{listing_id}",
    }
    item.update(overrides)
    return item


class KamernetScraperTests(unittest.IsolatedAsyncioTestCase):
    def test_build_url_supports_page_number(self):
        url = KamernetScraper(city="Amsterdam")._build_url(page_no=2)

        self.assertIn("pageNo=2", url)

    def test_build_url_combines_apartment_furnished_and_long_term(self):
        url = KamernetScraper(
            city="Amsterdam",
            property_type="apartment,furnished,long_term",
        )._build_url()

        self.assertIn("searchCategories=2%2C17%2C19", url)

    async def test_fetch_pages_uses_configured_page_cap(self):
        scraper = KamernetScraper(city="Amsterdam")
        scraper._fetch_page = AsyncMock(return_value="<html></html>")

        with (
            patch("scrapers.kamernet.config.KAMERNET_MAX_PAGES_PER_SCAN", 2),
            patch("scrapers.kamernet.get_httpx_client", AsyncMock(return_value=object())),
        ):
            pages = await scraper._fetch_pages()

        self.assertEqual(len(pages), 2)
        requested_urls = [call.args[1] for call in scraper._fetch_page.await_args_list]
        self.assertTrue(any("pageNo=1" in url for url in requested_urls))
        self.assertTrue(any("pageNo=2" in url for url in requested_urls))

    async def test_scrape_reads_multiple_pages_and_deduplicates_top_ads(self):
        pinned = _listing_item("2380000", "Pinned Studio", 1400)
        target = _listing_item("2385264", "De Wittenkade", 1650)
        page_one = _next_data_html([pinned, _listing_item("2380001", "Page One", 1300)])
        page_two = _next_data_html([pinned, target])

        scraper = KamernetScraper(
            city="Amsterdam",
            max_price=2000,
            min_bedrooms=0,
            min_size_m2=0,
            property_type="studio",
        )
        scraper._fetch_pages = AsyncMock(
            return_value=[
                ("https://kamernet.test/pageNo=1", page_one),
                ("https://kamernet.test/pageNo=2", page_two),
            ]
        )

        listings = await scraper.scrape()

        self.assertEqual(
            [listing.id for listing in listings],
            ["2380000", "2380001", "2385264"],
        )
        self.assertEqual(listings[-1].title, "De Wittenkade")
        self.assertEqual(listings[-1].price_eur, 1650)

    async def test_scrape_locally_enforces_apartment_and_furnished(self):
        apartment_furnished = _listing_item(
            "2381000",
            "Matching Apartment",
            listingType=2,
            furnishingId=4,
        )
        room_furnished = _listing_item(
            "2381001",
            "Wrong Room",
            listingType=1,
            furnishingId=4,
        )
        apartment_unfurnished = _listing_item(
            "2381002",
            "Wrong Furnishing",
            listingType=2,
            furnishingId=2,
        )
        scraper = KamernetScraper(
            city="Amsterdam",
            max_price=2000,
            min_bedrooms=0,
            property_type="apartment,furnished,long_term",
        )
        scraper._fetch_pages = AsyncMock(
            return_value=[
                (
                    "https://kamernet.test/pageNo=1",
                    _next_data_html(
                        [apartment_furnished, room_furnished, apartment_unfurnished]
                    ),
                )
            ]
        )

        listings = await scraper.scrape()

        self.assertEqual([listing.id for listing in listings], ["2381000"])
        self.assertEqual(listings[0].property_type, "apartment")
        self.assertEqual(listings[0].furnishing, "furnished")


if __name__ == "__main__":
    unittest.main()
