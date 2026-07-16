import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scrapers.funda import FundaScraper


def _raw_listing(**overrides):
    values = {
        "url": "https://www.funda.nl/detail/huur/amsterdam/example/12345678/",
        "detail_url": "/detail/huur/amsterdam/example/12345678/",
        "title": "Prinsengracht 1",
        "city": "Amsterdam",
        "price": SimpleNamespace(amount=1850, formatted=""),
        "rooms_count": 2,
        "bedrooms": 1,
        "living_area": 55,
        "property_details": SimpleNamespace(object_type="apartment"),
        "media": SimpleNamespace(photo_urls=("https://images.example/funda.jpg",)),
        "tiny_id": "12345678",
        "global_id": 87654321,
        "id": "12345678",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FundaScraperTests(unittest.TestCase):
    def test_convert_listing_maps_pyfunda_fields(self):
        listing = FundaScraper()._convert_listing(_raw_listing())

        self.assertEqual(listing.id, "12345678")
        self.assertEqual(listing.source, "funda")
        self.assertEqual(listing.title, "Prinsengracht 1")
        self.assertEqual(listing.address, "Prinsengracht 1, Amsterdam")
        self.assertEqual(listing.price, "EUR 1850")
        self.assertEqual(listing.url, "https://www.funda.nl/detail/huur/amsterdam/example/12345678/")
        self.assertEqual(listing.price_eur, 1850)
        self.assertEqual(listing.rooms, "2 rooms, 1 bedroom")
        self.assertEqual(listing.bedrooms, 1)
        self.assertEqual(listing.size_m2, "55 m2")
        self.assertEqual(listing.size_m2_value, 55)
        self.assertEqual(listing.image_url, "https://images.example/funda.jpg")
        self.assertEqual(listing.property_type, "apartment")

    def test_scrape_sync_uses_rental_filters_pages_and_deduplicates(self):
        raw_listing = _raw_listing()

        class FakeClient:
            location = None
            filters = None
            kwargs = None
            max_pages = None

            def __init__(self, **kwargs):
                FakeClient.kwargs = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def iter_search(self, location, *, max_pages, **filters):
                FakeClient.location = location
                FakeClient.filters = filters
                FakeClient.max_pages = max_pages
                return [raw_listing, raw_listing]

        scraper = FundaScraper(
            city="Amsterdam",
            max_price=2000,
            min_bedrooms=2,
            min_size_m2=50,
            property_types=("apartment",),
        )

        with (
            patch("scrapers.funda.config.FUNDA_PYFUNDA_TIMEOUT_SECONDS", 8),
            patch("scrapers.funda.config.FUNDA_PYFUNDA_MAX_RETRIES", 1),
            patch("scrapers.funda.config.FUNDA_PYFUNDA_RETRY_BACKOFF_SECONDS", 0.05),
            patch("scrapers.funda.config.FUNDA_MAX_PAGES_PER_SCAN", 6),
        ):
            listings = scraper._scrape_sync(FakeClient)

        self.assertEqual(len(listings), 1)
        self.assertEqual(
            FakeClient.kwargs,
            {"timeout": 8, "max_retries": 1, "retry_backoff": 0.05},
        )
        self.assertEqual(FakeClient.location, "amsterdam")
        self.assertEqual(FakeClient.max_pages, 6)
        self.assertEqual(
            FakeClient.filters,
            {
                "category": "rent",
                "sort": "newest",
                "max_price": 2000,
                "min_bedrooms": 2,
                "min_area": 50,
                "object_type": "apartment",
            },
        )

    def test_apartment_filter_rejects_non_apartment_result(self):
        scraper = FundaScraper(max_price=0, property_types=("apartment",))
        listing = scraper._convert_listing(
            _raw_listing(property_details=SimpleNamespace(object_type="house"))
        )

        self.assertFalse(scraper._matches_filters(listing))

    def test_convert_listing_keeps_bedrooms_separate_from_total_rooms(self):
        listing = FundaScraper()._convert_listing(_raw_listing(rooms_count=1, bedrooms=0))

        self.assertEqual(listing.rooms, "1 room")
        self.assertEqual(listing.bedrooms, 0)
        self.assertFalse(FundaScraper(min_bedrooms=1)._matches_filters(listing))

    def test_convert_listing_expands_relative_urls(self):
        listing = FundaScraper()._convert_listing(
            _raw_listing(url=None, detail_url="/detail/huur/amsterdam/example/12345678/")
        )

        self.assertEqual(
            listing.url,
            "https://www.funda.nl/detail/huur/amsterdam/example/12345678/",
        )

    def test_convert_listing_expands_relative_urls_without_leading_slash(self):
        listing = FundaScraper()._convert_listing(
            _raw_listing(url=None, detail_url="detail/huur/amsterdam/example/12345678/")
        )

        self.assertEqual(
            listing.url,
            "https://www.funda.nl/detail/huur/amsterdam/example/12345678/",
        )

    def test_convert_listing_uses_working_id_fallback_url(self):
        listing = FundaScraper()._convert_listing(
            _raw_listing(url=None, detail_url=None)
        )

        self.assertEqual(listing.url, "https://www.funda.nl/detail/12345678/")


if __name__ == "__main__":
    unittest.main()
