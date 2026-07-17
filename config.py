import os
import sys
from dotenv import load_dotenv

load_dotenv()


def _parse_chat_ids(raw_value: str) -> set[int]:
    chat_ids: set[int] = set()
    for item in raw_value.replace(",", " ").split():
        try:
            chat_ids.add(int(item))
        except ValueError:
            sys.exit(f"ERRORE: TELEGRAM_ALLOWED_CHAT_IDS contiene un chat ID non valido: {item}")
    return chat_ids


def _parse_bool(raw_value: str) -> bool:
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_positive_ints(raw_value: str, name: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip()) for item in raw_value.split(",") if item.strip())
    except ValueError:
        sys.exit(f"ERRORE: {name} deve contenere interi separati da virgole")
    if not values or any(value <= 0 for value in values):
        sys.exit(f"ERRORE: {name} deve contenere solo interi positivi")
    return values


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))
FAST_POLL_INTERVAL_SECONDS = int(
    os.getenv(
        "FAST_POLL_INTERVAL_SECONDS",
        os.getenv("PARARIUS_POLL_INTERVAL_SECONDS", str(POLL_INTERVAL_SECONDS)),
    )
)
SCAN_JITTER_SECONDS = int(os.getenv("SCAN_JITTER_SECONDS", "30"))
FORBIDDEN_FAILURE_THRESHOLD = max(1, int(os.getenv("FORBIDDEN_FAILURE_THRESHOLD", "3")))
FORBIDDEN_BACKOFF_SECONDS = _parse_positive_ints(
    os.getenv("FORBIDDEN_BACKOFF_SECONDS", "21600,43200,86400"),
    "FORBIDDEN_BACKOFF_SECONDS",
)
PARARIUS_POLL_INTERVAL_SECONDS = FAST_POLL_INTERVAL_SECONDS
ROOFZ_POLL_INTERVAL_SECONDS = FAST_POLL_INTERVAL_SECONDS
SCRAPER_TIMEOUT_SECONDS = int(os.getenv("SCRAPER_TIMEOUT_SECONDS", "45"))
DB_PATH = os.getenv("DB_PATH", "listings.db")
KAMERNET_AUTOREPLY_TIMEOUT_SECONDS = int(os.getenv("KAMERNET_AUTOREPLY_TIMEOUT_SECONDS", "45"))
KAMERNET_AUTOREPLY_MAX_PER_SCAN = int(os.getenv("KAMERNET_AUTOREPLY_MAX_PER_SCAN", "2"))
KAMERNET_MAX_PAGES_PER_SCAN = int(os.getenv("KAMERNET_MAX_PAGES_PER_SCAN", "3"))
_DEFAULT_KAMERNET_AUTOREPLY_STORAGE_STATE_PATH = (
    os.path.join(os.path.dirname(DB_PATH), "kamernet_storage_state.json")
    if os.path.dirname(DB_PATH)
    else "kamernet_storage_state.json"
)
KAMERNET_AUTOREPLY_STORAGE_STATE_PATH = (
    os.getenv("KAMERNET_AUTOREPLY_STORAGE_STATE_PATH", "").strip()
    or _DEFAULT_KAMERNET_AUTOREPLY_STORAGE_STATE_PATH
)
KAMERNET_AUTOREPLY_EMAIL = os.getenv("KAMERNET_AUTOREPLY_EMAIL", "")
KAMERNET_AUTOREPLY_PASSWORD = os.getenv("KAMERNET_AUTOREPLY_PASSWORD", "")
KAMERNET_AUTOREPLY_HEADLESS = _parse_bool(os.getenv("KAMERNET_AUTOREPLY_HEADLESS", "true"))
KAMERNET_AUTOREPLY_DRY_RUN = _parse_bool(os.getenv("KAMERNET_AUTOREPLY_DRY_RUN", "false"))
KAMERNET_AUTOREPLY_DEFAULT_TEMPLATE = os.getenv(
    "KAMERNET_AUTOREPLY_DEFAULT_TEMPLATE",
    (
        "Hello, I am interested in this place and would like to schedule a viewing. "
        "Kind regards"
    ),
)
PARARIUS_SCRAPER_TIMEOUT_SECONDS = int(os.getenv("PARARIUS_SCRAPER_TIMEOUT_SECONDS", "20"))
FUNDA_SCRAPER_TIMEOUT_SECONDS = int(os.getenv("FUNDA_SCRAPER_TIMEOUT_SECONDS", "25"))
FUNDA_PYFUNDA_TIMEOUT_SECONDS = int(os.getenv("FUNDA_PYFUNDA_TIMEOUT_SECONDS", "12"))
FUNDA_PYFUNDA_MAX_RETRIES = int(os.getenv("FUNDA_PYFUNDA_MAX_RETRIES", "2"))
FUNDA_PYFUNDA_RETRY_BACKOFF_SECONDS = float(
    os.getenv("FUNDA_PYFUNDA_RETRY_BACKOFF_SECONDS", "0.1")
)
FUNDA_MAX_BACKGROUND_THREADS = int(os.getenv("FUNDA_MAX_BACKGROUND_THREADS", "1"))
FUNDA_MAX_PAGES_PER_SCAN = int(os.getenv("FUNDA_MAX_PAGES_PER_SCAN", "6"))
ROOFZ_SCRAPER_TIMEOUT_SECONDS = int(os.getenv("ROOFZ_SCRAPER_TIMEOUT_SECONDS", "90"))
ROOFZ_ENABLED = _parse_bool(os.getenv("ROOFZ_ENABLED", "true"))
VVA_MAX_PAGES_PER_SCAN = int(os.getenv("VVA_MAX_PAGES_PER_SCAN", "1"))
MAX_CONCURRENT_USERS_PER_JOB = int(os.getenv("MAX_CONCURRENT_USERS_PER_JOB", "3"))
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))
TELEGRAM_ALLOWED_CHAT_IDS = _parse_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))

if not TELEGRAM_TOKEN:
    sys.exit("ERRORE: TELEGRAM_TOKEN non trovato. Copia .env.example in .env e inserisci il token.")
