"""Settings. Everything environment-specific lives here; secrets come from the gitignored .env."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path=ROOT / ".env"):
    """Minimal KEY=VALUE .env loader (real environment variables win)."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()

BASE_URL = os.environ.get("CRM_BASE_URL", "https://analyst-assessment-production.up.railway.app").rstrip("/")
API_URL = BASE_URL + "/api/v1"
API_TOKEN = os.environ.get("CRM_API_TOKEN", "")
DB_PATH = Path(os.environ.get("SYNC_DB_PATH", ROOT / "data" / "state.db"))

# The operator whose website we scrape, and the CRM parent account its facilities belong under.
TARGET_PARENT_NAME = os.environ.get("TARGET_PARENT_NAME", "Bellhaven Senior Living (Parent Account)")
BRAND_WORDS = {"bellhaven"}  # ignored when comparing facility names

# Website "care offering" -> CRM care_type.
CARE_TYPE_MAP = {
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
}

# Abort a run (propose nothing) if the scrape finds fewer than this share of the previous run's
# locations - a half-loaded website must never turn into a wave of "no longer on the website" flags.
# If an operator really does shed more than 20% of its communities, acknowledge it by running once
# with MIN_SCRAPE_RATIO=0.
MIN_SCRAPE_RATIO = float(os.environ.get("MIN_SCRAPE_RATIO", "0.8"))


def load_overrides(path=ROOT / "overrides.json"):
    """Optional reviewer overrides, e.g. {"survivor_pins": {"<location-slug>": "<account_id>"}}."""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
