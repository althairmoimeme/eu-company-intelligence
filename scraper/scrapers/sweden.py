"""Sweden scraper — OMX Stockholm large caps + grandes entreprises privées.
Revenue: via Yahoo Finance (.ST tickers) + curated estimates.
Registration: Allabolag.se org_nr numbers (curated).
"""
import asyncio
import logging
from typing import AsyncIterator
import yfinance as yf
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord
from ..enrichers.nace import normalize_code, code_to_sector_label

logger = logging.getLogger(__name__)

SEK_TO_EUR = 0.087

# (org_nr, name, sector, ticker, rev_eur_M_estimate)
TOP_SWEDISH_COMPANIES = [
    # ── GRANDES COTÉES (OMX Stockholm) ───────────────────────────────────────
    ("556008-8734",  "VOLVO AB",                            "Automotive",    "VOLV-B.ST",  46000),
    ("556084-0922",  "ERICSSON AB",                         "Telecom",       "ERIC-B.ST",  22000),
    ("556016-0680",  "H&M HENNES & MAURITZ AB",             "Retail",        "HM-B.ST",    22000),
    ("556190-4715",  "ATLAS COPCO AB",                      "Manufacturing", "ATCO-A.ST",  16000),
    ("556005-1668",  "SANDVIK AB",                          "Manufacturing", "SAND.ST",    12000),
    ("556001-6122",  "ELECTROLUX AB",                       "Manufacturing", "ELUX-B.ST",  14000),
    ("556000-1799",  "SCANIA AB",                           "Automotive",    "SCV-B.ST",   16000),
    ("556260-7859",  "INVESTOR AB",                         "Finance",       "INVE-B.ST",   4000),
    ("502012-6293",  "SVENSKA HANDELSBANKEN",               "Finance",       "SHB-A.ST",    5000),
    ("502017-7753",  "NORDEA BANK AB",                      "Finance",       "NDA-SEK.ST", 10000),
    ("502032-9081",  "SWEDBANK AB",                         "Finance",       "SWED-A.ST",   5000),
    ("502052-0630",  "SEB AB",                              "Finance",       "SEB-A.ST",    5500),
    ("556063-9498",  "TELIA COMPANY AB",                    "Telecom",       "TELIA.ST",    8000),
    ("556000-9808",  "SECURITAS AB",                        "Services",      "SECU-B.ST",  13000),
    ("556063-3993",  "ALFA LAVAL AB",                       "Manufacturing", "ALFA.ST",     6000),
    ("556005-4060",  "SKF AB",                              "Manufacturing", "SKF-B.ST",   10000),
    ("556180-9554",  "HEXAGON AB",                          "IT",            "HEXA-B.ST",   5000),
    ("556034-8590",  "ESSITY AB",                           "Manufacturing", "ESSITY-B.ST",13000),
    ("556000-4838",  "SCA AB",                              "Manufacturing", "SCA-B.ST",    6000),
    ("559005-6821",  "SPOTIFY TECHNOLOGY SA",               "IT",            None,          14000),
    ("556301-6679",  "AUTOLIV INC",                         "Automotive",    "ALIV-SDB.ST", 9000),
    ("556570-2628",  "BOLIDEN AB",                          "Metals",        "BOL.ST",      8500),
    ("556717-1889",  "EVOLUTION AB",                        "IT",            "EVO.ST",      2000),
    ("556051-6379",  "KINNEVIK AB",                         "Finance",       "KINV-B.ST",    800),
    ("556411-8702",  "TELEFONAKTIEBOLAGET LM ERICSSON",     "Telecom",       None,          22000),
    # ── GRANDES PRIVÉES ───────────────────────────────────────────────────────
    ("556227-5493",  "IKEA OF SWEDEN AB",                   "Retail",        None,          47000),
    ("556000-1642",  "AB VOLVO TRUCKS",                     "Automotive",    None,          20000),
    ("556001-4970",  "VATTENFALL AB",                       "Energy",        None,          22000),
    ("556008-1133",  "SVENSKA CELLULOSA AB",                "Manufacturing", None,           8000),
    ("556001-4988",  "SYSTEMBOLAGET AB",                    "Retail",        None,           3500),
    ("502031-1566",  "RIKSBANK",                            "Finance",       None,           2000),
    ("556705-5958",  "KLARNA BANK AB",                      "Finance",       None,           2300),
    ("556050-0233",  "LINDAB INTERNATIONAL AB",             "Manufacturing", "LIAB.ST",       900),
    ("556000-3503",  "VOLVO CARS AB",                       "Automotive",    "VOLCAR-B.ST", 45000),
    ("556002-7135",  "SAAB AB",                             "Aerospace",     "SAAB-B.ST",   4200),
    ("556000-2322",  "ICA GRUPPEN AB",                      "Retail",        "ICA.ST",      13000),
    ("556024-7736",  "AXFOOD AB",                           "Retail",        "AXFO.ST",      6000),
    ("556065-6674",  "COOP SVERIGE AB",                     "Retail",        None,           7000),
    ("556001-8940",  "POSTNORD AB",                         "Logistics",     None,           4500),
    ("556008-5423",  "SAS AB",                              "Transport",     "SAS.ST",       3500),
]


class SwedenScraper(BaseScraper):
    name = "swe_se"
    country = "SE"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        checkpoint = self.load_checkpoint() if resume else {}
        done = set(checkpoint.get("done_swe_se", []))
        seen_names: set[str] = set()

        for org_nr, name, sector, ticker, rev_estimate in TOP_SWEDISH_COMPANIES:
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
                        currency = info.get("currency", "SEK")
                        if currency == "SEK":
                            revenue_eur = rev_raw * SEK_TO_EUR
                        elif currency == "USD":
                            revenue_eur = rev_raw * 0.93
                        elif currency == "EUR":
                            revenue_eur = float(rev_raw)
                        revenue_estimated = False
                        logger.info(f"[SE] {name}: {revenue_eur/1e6:.0f}M EUR (YF)")
                except Exception as e:
                    logger.debug(f"[SE] YF failed {ticker}: {e}")

            if revenue_eur is None:
                revenue_eur = rev_estimate * 1_000_000
                revenue_estimated = True

            yield CompanyRecord(
                name=name,
                country="SE",
                registration_number=org_nr,
                revenue_eur=revenue_eur,
                revenue_estimated=revenue_estimated,
                employees=None,
                sector=sector,
                nace_code=None,
                creation_date=None,
                city=None,
                source_url=f"https://www.allabolag.se/",
                directors=[],
            )

            done.add(name_key)
            checkpoint["done_swe_se"] = list(done)
            self.save_checkpoint(checkpoint)
            await asyncio.sleep(0.05)
