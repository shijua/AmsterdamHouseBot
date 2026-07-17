import unittest
from unittest.mock import AsyncMock, patch

from scrapers.base import ForbiddenResponseError
from scrapers.pararius import ParariusScraper


LATEST_HTML = """
<html>
  <body>
    <article>
      <a href="/appartement-te-huur/amsterdam/0ab6aa57/singel">Appartement Singel</a>
      <p>1012 WL Amsterdam (Centrum)</p>
      <p>EUR 1500 per maand</p>
      <p>50 m2</p>
      <p>2 kamers</p>
    </article>
    <article>
      <a href="/appartement-te-huur/rotterdam/abcdef12/markt">Appartement Markt</a>
      <p>3011 XZ Rotterdam (Centrum)</p>
      <p>EUR 1200 per maand</p>
      <p>55 m2</p>
      <p>2 kamers</p>
    </article>
  </body>
</html>
"""


CITY_HTML = """
<html>
  <body>
    <section class="listing-search-item">
      <a class="listing-search-item__link--title" href="/appartement-te-huur/amsterdam/0ab6aa57/singel">
        Appartement Singel
      </a>
      <h2 class="listing-search-item__title">Appartement Singel</h2>
      <div class="listing-search-item__sub-title">1012 WL Amsterdam (Centrum)</div>
      <div class="listing-search-item__price">EUR 1500 per maand</div>
      <ul class="listing-search-item__features">
        <li>50 m2</li>
        <li>2 kamers</li>
      </ul>
    </section>
    <section class="listing-search-item">
      <a class="listing-search-item__link--title" href="/appartement-te-huur/amsterdam/12345678/prinsengracht">
        Appartement Prinsengracht
      </a>
      <h2 class="listing-search-item__title">Appartement Prinsengracht</h2>
      <div class="listing-search-item__sub-title">1015 AB Amsterdam (Jordaan)</div>
      <div class="listing-search-item__price">EUR 1750 per maand</div>
      <ul class="listing-search-item__features">
        <li>65 m2</li>
        <li>3 kamers</li>
      </ul>
    </section>
  </body>
</html>
"""


PREFERENCE_HTML = """
<html><body>
  <section class="listing-search-item">
    <a class="listing-search-item__link--title" href="/appartement-te-huur/amsterdam/aaaaaaaa/singel">Appartement Singel</a>
    <h2 class="listing-search-item__title">Appartement Singel</h2>
    <div class="listing-search-item__sub-title">1012 AB Amsterdam</div>
    <div class="listing-search-item__price">EUR 1500 per maand</div>
    <ul class="listing-search-item__features"><li>50 m2</li><li>2 kamers</li><li>Gestoffeerd of gemeubileerd</li></ul>
  </section>
  <section class="listing-search-item">
    <a class="listing-search-item__link--title" href="/huis-te-huur/amsterdam/bbbbbbbb/markt">Huis Markt</a>
    <h2 class="listing-search-item__title">Huis Markt</h2>
    <div class="listing-search-item__sub-title">1013 AB Amsterdam</div>
    <div class="listing-search-item__price">EUR 1400 per maand</div>
    <ul class="listing-search-item__features"><li>80 m2</li><li>3 kamers</li><li>Gemeubileerd</li></ul>
  </section>
</body></html>
"""


class ParariusScraperTests(unittest.IsolatedAsyncioTestCase):
    async def test_apartment_and_ambiguous_furnished_are_combined_with_and(self):
        scraper = ParariusScraper(
            city="Amsterdam",
            max_price=2000,
            min_bedrooms=0,
            property_types=("apartment",),
            furnished=True,
        )
        scraper._fetch_pages = AsyncMock(return_value=[("city", PREFERENCE_HTML)])

        listings = await scraper.scrape()

        self.assertEqual([listing.id for listing in listings], ["aaaaaaaa"])
        self.assertEqual(listings[0].property_type, "apartment")
        self.assertEqual(listings[0].furnishing, "upholstered_or_furnished")

    async def test_scrape_merges_latest_and_city_pages_without_duplicate_listing_ids(self):
        scraper = ParariusScraper(
            city="Amsterdam",
            max_price=2000,
            min_bedrooms=1,
            min_size_m2=0,
        )
        scraper._fetch_pages = AsyncMock(
            return_value=[
                ("latest", LATEST_HTML),
                ("city", CITY_HTML),
            ]
        )

        listings = await scraper.scrape()

        self.assertEqual([listing.id for listing in listings], ["0ab6aa57", "12345678"])
        self.assertTrue(all(listing.source == "pararius" for listing in listings))
        self.assertEqual(listings[0].title, "Appartement Singel")
        self.assertEqual(listings[0].price_eur, 1500)
        self.assertEqual(listings[0].size_m2_value, 50)
        self.assertEqual(listings[0].bedrooms, 2)

    async def test_latest_page_is_filtered_by_city_and_price(self):
        scraper = ParariusScraper(
            city="Amsterdam",
            max_price=1400,
            min_bedrooms=1,
            min_size_m2=0,
        )
        scraper._fetch_pages = AsyncMock(return_value=[("latest", LATEST_HTML)])

        listings = await scraper.scrape()

        self.assertEqual(listings, [])

    async def test_forbidden_response_resets_shared_transport(self):
        scraper = ParariusScraper(
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
            patch("scrapers.pararius._USE_CURL", True),
            patch("scrapers.pararius.close_shared_session", AsyncMock()) as close_shared_session,
        ):
            with self.assertRaises(ForbiddenResponseError):
                await scraper._fetch_with_session(
                    FakeSession(),
                    (("city", "https://example.test/city"),),
                )

        close_shared_session.assert_awaited_once_with("pararius")


if __name__ == "__main__":
    unittest.main()
