import unittest
from unittest.mock import AsyncMock, patch

from bs4 import BeautifulSoup

from scrapers.huurwoningen import HuurwoningenScraper


LISTINGS_HTML = """
<html><body>
  <section class="listing-search-item" data-listing-search-item-id="aaaaaaaa">
    <a class="listing-search-item__link--title" href="/en/huren/amsterdam/aaaaaaaa/singel/">Flat Singel</a>
    <h2 class="listing-search-item__title">Flat Singel</h2>
    <div class="listing-search-item__sub-title">1012 AB Amsterdam</div>
    <div class="listing-search-item__price-main">EUR 1500 pcm</div>
    <ul class="listing-search-item__features"><li>50 m2</li><li>2 rooms</li><li>Furnished</li></ul>
  </section>
  <section class="listing-search-item" data-listing-search-item-id="bbbbbbbb">
    <a class="listing-search-item__link--title" href="/en/huren/amsterdam/bbbbbbbb/markt/">House Markt</a>
    <h2 class="listing-search-item__title">House Markt</h2>
    <div class="listing-search-item__price-main">EUR 1400 pcm</div>
    <ul class="listing-search-item__features"><li>70 m2</li><li>3 rooms</li><li>Upholstered</li></ul>
  </section>
</body></html>
"""


class HuurwoningenScraperTests(unittest.IsolatedAsyncioTestCase):
    def test_apartment_and_furnished_use_native_search_filters(self):
        scraper = HuurwoningenScraper(
            city="Amsterdam",
            max_price=1800,
            min_bedrooms=1,
            min_size_m2=30,
            property_types=("apartment",),
            furnished=True,
        )

        url = scraper._build_url()

        self.assertIn("/en/appartement/huren/amsterdam/", url)
        self.assertIn("interior=gemeubileerd", url)
        self.assertIn("price=0-1800", url)

    def test_cards_are_locally_filtered_by_type_and_furnishing(self):
        scraper = HuurwoningenScraper(
            max_price=1800,
            min_bedrooms=0,
            property_types=("apartment",),
            furnished=True,
        )
        soup = BeautifulSoup(LISTINGS_HTML, "lxml")
        listings = [
            scraper._parse_article(article)
            for article in soup.select("section.listing-search-item")
        ]

        matching = [listing for listing in listings if scraper._matches_filters(listing)]

        self.assertEqual([listing.id for listing in matching], ["aaaaaaaa"])
        self.assertEqual(matching[0].property_type, "apartment")
        self.assertEqual(matching[0].furnishing, "furnished")

    async def test_forbidden_response_resets_shared_transport(self):
        scraper = HuurwoningenScraper(
            city="Amsterdam",
            max_price=2000,
            min_bedrooms=1,
            min_size_m2=0,
        )

        class ForbiddenResponse:
            text = ""

            def raise_for_status(self):
                raise RuntimeError("HTTP Error 403:")

        class FakeSession:
            async def get(self, *args, **kwargs):
                return ForbiddenResponse()

        with (
            patch("scrapers.huurwoningen._USE_CURL", True),
            patch("scrapers.huurwoningen.get_shared_session", AsyncMock(return_value=FakeSession())),
            patch("scrapers.huurwoningen.close_shared_session", AsyncMock()) as close_shared_session,
        ):
            listings = await scraper.scrape()

        self.assertEqual(listings, [])
        close_shared_session.assert_awaited_once_with("huurwoningen")


if __name__ == "__main__":
    unittest.main()
