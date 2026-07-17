from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
import re


class ForbiddenResponseError(RuntimeError):
    """Raised when a source rejects a scrape with HTTP 403."""

    def __init__(self, source: str):
        self.source = source
        super().__init__(f"{source} returned HTTP 403")


@dataclass
class Listing:
    id: str
    source: str
    title: str
    price: str
    address: str
    url: str
    image_url: str | None = None
    rooms: str | None = None
    size_m2: str | None = None
    price_eur: int | None = None
    bedrooms: int | None = None
    size_m2_value: int | None = None
    property_type: str | None = None
    furnishing: str | None = None


class BaseScraper(ABC):
    SOURCE = ""
    BASE_URL = ""

    def __init__(
        self,
        city: str = "Amsterdam",
        max_price: int = 2000,
        min_bedrooms: int = 1,
        min_size_m2: int = 0,
        property_types: str | Iterable[str] | None = None,
        furnished: bool = False,
    ):
        self.city = city.strip() or "Amsterdam"
        self.max_price = max_price
        self.min_bedrooms = min_bedrooms
        self.min_size_m2 = min_size_m2
        self.property_types = normalize_property_types(property_types)
        self.furnished = furnished

    @abstractmethod
    async def scrape(self) -> list[Listing]:
        pass

    def _matches_filters(self, listing: Listing) -> bool:
        if self.max_price:
            if listing.price_eur is None:
                return False
            if listing.price_eur > self.max_price:
                return False
        if self.min_bedrooms and listing.bedrooms is not None and listing.bedrooms < self.min_bedrooms:
            return False
        if self.min_size_m2 and listing.size_m2_value and listing.size_m2_value < self.min_size_m2:
            return False
        if self.property_types and listing.property_type not in self.property_types:
            return False
        if self.furnished and listing.furnishing not in {"furnished", "upholstered_or_furnished"}:
            return False
        return True


def normalize_property_types(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    values = re.split(r"[,;\n]+", value) if isinstance(value, str) else value
    return tuple(dict.fromkeys(str(item).strip().lower() for item in values if str(item).strip()))


def parse_furnishing(text: str | None) -> str | None:
    if not text:
        return None
    normalized = " ".join(text.lower().split())
    if "upholstered or furnished" in normalized or "gestoffeerd of gemeubileerd" in normalized:
        return "upholstered_or_furnished"
    if "furnished" in normalized or "gemeubileerd" in normalized:
        return "furnished"
    if "upholstered" in normalized or "gestoffeerd" in normalized:
        return "upholstered"
    if re.search(r"\bshell\b|\bkaal\b", normalized):
        return "shell"
    return None


def parse_euro_amount(text: str | None) -> int | None:
    if not text:
        return None

    normalized = text.replace("\xa0", " ").replace("\u20ac", "EUR")
    patterns = (
        r"(?:€|EUR)\s*(\d[\d.,\s]*)",
        r"rent\s*price:?\s*(?:€|EUR)?\s*(\d[\d.,\s]*)",
        r"(\d[\d.,\s]*)\s*(?:pcm|p/m|per\s+maand|per\s+month|/month)",
    )

    match = None
    for pattern in patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            break
    if not match:
        match = re.search(r"\d[\d.,\s]*", normalized)
    if not match:
        return None

    value = match.group(1) if match.lastindex else match.group(0)
    digits = _normalize_amount_digits(value)
    return int(digits) if digits else None


def _normalize_amount_digits(value: str) -> str:
    value = re.sub(r"\s+", "", value)
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").split(",", 1)[0]
        else:
            value = value.replace(",", "").split(".", 1)[0]
    elif "," in value:
        parts = value.split(",")
        if len(parts[-1]) == 3 and all(part.isdigit() for part in parts):
            value = "".join(parts)
        else:
            value = parts[0]
    elif "." in value:
        parts = value.split(".")
        if len(parts[-1]) == 3 and all(part.isdigit() for part in parts):
            value = "".join(parts)
        else:
            value = parts[0]

    return re.sub(r"\D", "", value)


def parse_first_int(text: str | None) -> int | None:
    if not text:
        return None

    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None
