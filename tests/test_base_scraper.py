import unittest

from scrapers.base import BaseScraper, Listing, parse_euro_amount, parse_furnishing


class _ConcreteScraper(BaseScraper):
    async def scrape(self) -> list[Listing]:
        return []


class BaseScraperTests(unittest.TestCase):
    def test_max_price_rejects_listings_without_parseable_price(self):
        scraper = _ConcreteScraper(max_price=1750)
        listing = Listing(
            id="missing-price",
            source="test",
            title="Missing price",
            price="",
            address="Amsterdam",
            url="https://example.test/missing-price",
            price_eur=None,
        )

        self.assertFalse(scraper._matches_filters(listing))

    def test_parse_euro_amount_handles_real_euro_symbol(self):
        self.assertEqual(parse_euro_amount("\u20ac 2.350,- /mnd"), 2350)

    def test_property_type_and_furnishing_are_combined_with_and(self):
        scraper = _ConcreteScraper(
            max_price=0,
            property_types=("apartment",),
            furnished=True,
        )
        matching = Listing(
            id="matching",
            source="test",
            title="Apartment",
            price="",
            address="Amsterdam",
            url="https://example.test/matching",
            property_type="apartment",
            furnishing="furnished",
        )
        wrong_type = Listing(**{**matching.__dict__, "id": "house", "property_type": "house"})
        wrong_furnishing = Listing(
            **{**matching.__dict__, "id": "shell", "furnishing": "shell"}
        )

        self.assertTrue(scraper._matches_filters(matching))
        self.assertFalse(scraper._matches_filters(wrong_type))
        self.assertFalse(scraper._matches_filters(wrong_furnishing))

    def test_ambiguous_upholstered_or_furnished_is_accepted(self):
        self.assertEqual(
            parse_furnishing("Gestoffeerd of gemeubileerd"),
            "upholstered_or_furnished",
        )


if __name__ == "__main__":
    unittest.main()
