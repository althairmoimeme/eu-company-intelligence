"""Switzerland scraper — SMI / SMI Expanded + grandes entreprises privées.
Revenue: via Yahoo Finance (.SW tickers) + curated estimates.
Registration: Zefix UID numbers (curated).
"""
import asyncio
import logging
from typing import AsyncIterator
import yfinance as yf
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord
from ..enrichers.nace import normalize_code, code_to_sector_label

logger = logging.getLogger(__name__)

CHF_TO_EUR = 1.04

# (uid_number, name, sector, ticker, rev_eur_M_estimate)
TOP_SWISS_COMPANIES = [
    # ── GRANDES COTÉES (SMI / SMI Expanded) ──────────────────────────────────
    ("CHE-116.281.710",  "NESTLE SA",                       "Food",          "NESN.SW",   94000),
    ("CHE-103.868.522",  "NOVARTIS AG",                     "Pharma",        "NOVN.SW",   45000),
    ("CHE-103.756.483",  "ROCHE HOLDING AG",                "Pharma",        "ROG.SW",    60000),
    ("CHE-103.682.174",  "ABB LTD",                         "Manufacturing", "ABBN.SW",   32000),
    ("CHE-103.810.959",  "ZURICH INSURANCE GROUP",          "Finance",       "ZURN.SW",   74000),
    ("CHE-114.798.724",  "UBS GROUP AG",                    "Finance",       "UBSG.SW",   45000),
    ("CHE-115.880.189",  "LONZA GROUP AG",                  "Pharma",        "LONN.SW",    6400),
    ("CHE-102.294.136",  "SWISS RE AG",                     "Finance",       "SREN.SW",   46000),
    ("CHE-114.913.162",  "PARTNERS GROUP HOLDING AG",       "Finance",       "PGHN.SW",    2200),
    ("CHE-116.427.977",  "SIKA AG",                         "Chemicals",     "SIKA.SW",   11200),
    ("CHE-105.872.978",  "RICHEMONT SA",                    "Luxury",        "CFR.SW",    20000),
    ("CHE-107.789.077",  "ALCON INC",                       "Healthcare",    "ALC.SW",    10000),
    ("CHE-103.836.490",  "GIVAUDAN SA",                     "Chemicals",     "GIVN.SW",    7600),
    ("CHE-107.852.621",  "KUHNE + NAGEL INTL AG",           "Logistics",     "KNIN.SW",   26000),
    ("CHE-116.568.452",  "STRAUMANN HOLDING AG",            "Healthcare",    "STMN.SW",    2500),
    ("CHE-107.042.560",  "SONOVA HOLDING AG",               "Healthcare",    "SOON.SW",    3700),
    ("CHE-105.964.836",  "SCHINDLER HOLDING AG",            "Manufacturing", "SCHN.SW",   11500),
    ("CHE-103.838.011",  "GEBERIT AG",                      "Manufacturing", "GEBN.SW",    3400),
    ("CHE-100.064.407",  "HOLCIM LTD",                      "Construction",  "HOLN.SW",   27000),
    ("CHE-102.708.037",  "LINDT & SPRUENGLI AG",            "Food",          "LISP.SW",    5200),
    ("CHE-104.610.722",  "LOGITECH INTL SA",                "IT",            "LOGN.SW",    5600),
    ("CHE-107.095.288",  "TEMENOS AG",                      "IT",            "TEMN.SW",    1000),
    ("CHE-107.788.797",  "BACHEM HOLDING AG",               "Pharma",        "BANB.SW",     700),
    ("CHE-116.202.719",  "VAT GROUP AG",                    "Manufacturing", "VACN.SW",    1000),
    # ── GRANDES PRIVÉES ───────────────────────────────────────────────────────
    ("CHE-105.989.010",  "GLENCORE PLC",                    "Metals",        "GLEN.SW",  256000),
    ("CHE-109.557.064",  "VITOL GROUP BV",                  "Energy",        None,        380000),
    ("CHE-102.169.002",  "MIGROS-GENOSSENSCHAFTS-BUND",     "Retail",        None,         30000),
    ("CHE-102.697.003",  "COOP GENOSSENSCHAFT",             "Retail",        None,         33000),
    ("CHE-103.887.906",  "SWATCH GROUP AG",                 "Luxury",        "UHR.SW",     7200),
    ("CHE-116.356.419",  "HELVETIA HOLDING AG",             "Finance",       "HELN.SW",   12000),
    ("CHE-103.730.792",  "BALOISE HOLDING AG",              "Finance",       "BALN.SW",    9600),
    ("CHE-108.611.239",  "FENACO GENOSSENSCHAFT",           "Food",          None,          7000),
    ("CHE-105.012.375",  "SWISSCOM AG",                     "Telecom",       "SCMN.SW",   11000),
    ("CHE-101.830.575",  "SBB CFF FFS AG",                  "Transport",     None,         10000),
    ("CHE-105.986.002",  "SWISS POST",                      "Logistics",     None,          7500),
    ("CHE-107.799.174",  "DUFRY AG",                        "Retail",        "DUFN.SW",    4500),
]


class SwitzerlandScraper(BaseScraper):
    name = "uid_ch"
    country = "CH"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        checkpoint = self.load_checkpoint() if resume else {}
        done = set(checkpoint.get("done_uid_ch", []))
        seen_names: set[str] = set()

        for uid, name, sector, ticker, rev_estimate in TOP_SWISS_COMPANIES:
            name_key = name.upper()[:35]
            if name_key in seen_names or name_key in done:
                continue
            seen_names.add(name_key)

            # Try Yahoo Finance for listed companies
            revenue_eur = None
            revenue_estimated = True

            if ticker:
                try:
                    info = yf.Ticker(ticker).info
                    rev_raw = info.get("totalRevenue")
                    if rev_raw and rev_raw > 0:
                        currency = info.get("currency", "CHF")
                        if currency == "CHF":
                            revenue_eur = rev_raw * CHF_TO_EUR
                        elif currency == "USD":
                            revenue_eur = rev_raw * 0.93
                        elif currency == "EUR":
                            revenue_eur = float(rev_raw)
                        revenue_estimated = False
                        logger.info(f"[CH] {name}: {revenue_eur/1e6:.0f}M EUR (YF)")
                except Exception as e:
                    logger.debug(f"[CH] YF failed {ticker}: {e}")

            if revenue_eur is None:
                revenue_eur = rev_estimate * 1_000_000
                revenue_estimated = True

            yield CompanyRecord(
                name=name,
                country="CH",
                registration_number=uid,
                revenue_eur=revenue_eur,
                revenue_estimated=revenue_estimated,
                employees=None,
                sector=sector,
                nace_code=None,
                creation_date=None,
                city=None,
                source_url=f"https://www.zefix.ch/",
                directors=[],
            )

            done.add(name_key)
            checkpoint["done_uid_ch"] = list(done)
            self.save_checkpoint(checkpoint)
            await asyncio.sleep(0.05)
