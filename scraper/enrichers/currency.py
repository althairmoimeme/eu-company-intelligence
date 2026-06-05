"""Currency conversion to EUR using ECB reference rates."""
import json
import os
from datetime import datetime, timedelta
import httpx

CACHE_PATH = os.path.join(os.path.dirname(__file__), "../../data/ecb_rates_cache.json")
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

FALLBACK_RATES = {
    "GBP": 1.17,
    "DKK": 7.46,
    "NOK": 11.85,
    "SEK": 11.55,
    "CHF": 0.96,
    "PLN": 4.28,
    "CZK": 25.20,
    "HUF": 396.0,
    "RON": 4.97,
    "BGN": 1.956,
    "HRK": 7.53,
    "USD": 1.09,
    "EUR": 1.0,
}

_rates: dict[str, float] = {}
_rates_fetched_at: datetime | None = None


async def _fetch_ecb_rates() -> dict[str, float]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(ECB_URL)
            resp.raise_for_status()
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp.text)
            ns = {"gesmes": "http://www.gesmes.org/xml/2002-08-01",
                  "ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
            rates = {"EUR": 1.0}
            for cube in root.findall(".//ecb:Cube[@currency]", ns):
                currency = cube.attrib["currency"]
                rate = float(cube.attrib["rate"])
                rates[currency] = rate
            # Cache to file
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH, "w") as f:
                json.dump({"fetched_at": datetime.utcnow().isoformat(), "rates": rates}, f)
            return rates
    except Exception:
        return FALLBACK_RATES


async def get_rates() -> dict[str, float]:
    global _rates, _rates_fetched_at

    # In-memory cache (1 hour TTL)
    if _rates and _rates_fetched_at and datetime.utcnow() - _rates_fetched_at < timedelta(hours=1):
        return _rates

    # File cache (24 hour TTL)
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH) as f:
                data = json.load(f)
            fetched_at = datetime.fromisoformat(data["fetched_at"])
            if datetime.utcnow() - fetched_at < timedelta(hours=24):
                _rates = data["rates"]
                _rates_fetched_at = datetime.utcnow()
                return _rates
    except Exception:
        pass

    _rates = await _fetch_ecb_rates()
    _rates_fetched_at = datetime.utcnow()
    return _rates


async def to_eur(amount: float, currency: str) -> float:
    if currency == "EUR":
        return amount
    rates = await get_rates()
    rate = rates.get(currency.upper(), FALLBACK_RATES.get(currency.upper(), 1.0))
    return round(amount / rate, 2)
