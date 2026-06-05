"""Netherlands curated scraper — AEX large caps + grandes entreprises privées.
Revenue: via Yahoo Finance (.AS tickers) + curated estimates.
Registration: KvK numbers (curated).
"""
import asyncio
import logging
from typing import AsyncIterator
import yfinance as yf
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord
from ..enrichers.nace import normalize_code, code_to_sector_label

logger = logging.getLogger(__name__)

EUR_TO_EUR = 1.0

# (kvk_number, name, sector, ticker, rev_eur_M_estimate)
TOP_DUTCH_COMPANIES = [
    # ── GRANDES COTÉES (AEX / Euronext Amsterdam) ────────────────────────────
    ("27312872",  "ASML HOLDING NV",                          "Manufacturing",   "ASML.AS",    27000),
    ("34179503",  "SHELL PLC",                                "Energy",          "SHEL.AS",   380000),
    ("27001001",  "UNILEVER NV",                              "Consumer Goods",  "UNA.AS",     60000),
    ("17001964",  "KONINKLIJKE PHILIPS NV",                   "Manufacturing",   "PHIA.AS",    18000),
    ("33031431",  "ING GROEP NV",                             "Finance",         "INGA.AS",    22000),
    ("34334259",  "ABN AMRO BANK NV",                         "Finance",         "ABN.AS",      7000),
    ("39046699",  "AEGON NV",                                 "Finance",         "AGN.AS",     10000),
    ("24288945",  "AIRBUS SE",                                "Aerospace",       "AIR.AS",     67000),
    ("27305211",  "HEINEKEN NV",                              "Consumer Goods",  "HEIA.AS",    30000),
    ("31042418",  "WOLTERS KLUWER NV",                        "IT",              "WKL.AS",      5600),
    ("65900675",  "RELX NV",                                  "IT",              "REN.AS",      9500),
    ("68787044",  "DSM-FIRMENICH AG",                         "Manufacturing",   "DSFIR.AS",   12000),
    ("33089429",  "RANDSTAD NV",                              "Services",        "RAND.AS",    25000),
    ("52503399",  "NN GROUP NV",                              "Finance",         "NN.AS",       5000),
    ("56306583",  "STELLANTIS NV",                            "Automotive",      "STLAM.AS",  188000),
    ("34253298",  "NXP SEMICONDUCTORS NV",                    "Manufacturing",   "NXPI.AS",    13000),
    ("27264946",  "BE SEMICONDUCTOR INDUSTRIES NV",           "Manufacturing",   "BESI.AS",      800),
    ("34275009",  "FLOW TRADERS NV",                          "Finance",         "FLOW.AS",      500),
    ("24282008",  "OCI NV",                                   "Chemicals",       "OCI.AS",      3000),
    ("71990401",  "JDE PEET'S NV",                            "Consumer Goods",  "JDEP.AS",     8000),
    ("27001914",  "KONINKLIJKE AHOLD DELHAIZE NV",            "Retail",          "AD.AS",      88000),
    ("29048662",  "POSTNL NV",                                "Logistics",       "PNL.AS",      3500),
    ("24316437",  "FUGRO NV",                                 "Services",        "FUR.AS",      2300),
    ("09036979",  "ARCADIS NV",                               "Services",        "ARCAD.AS",    4500),
    ("24312971",  "SBM OFFSHORE NV",                          "Energy",          "SBMO.AS",     3500),
    ("23073031",  "TKH GROUP NV",                             "Manufacturing",   "TWEKA.AS",    2000),
    ("39033431",  "AALBERTS INDUSTRIES NV",                   "Manufacturing",   "AALB.AS",     3300),
    ("30112147",  "BRUNEL INTERNATIONAL NV",                  "Services",        "BRNL.AS",     1000),
    # ── GRANDES PRIVÉES ───────────────────────────────────────────────────────
    ("11013941",  "IKEA GROUP (INGKA HOLDING BV)",            "Retail",          None,          45000),
    ("27271636",  "SHV HOLDINGS NV",                          "Energy",          None,          35000),
    ("23028271",  "PON HOLDINGS BV",                          "Automotive",      None,          12000),
    ("23007703",  "LYONDELLBASELL INDUSTRIES NV",             "Chemicals",       None,          40000),
    ("11011887",  "FRIESLANDCAMPINA NV",                      "Consumer Goods",  None,          13000),
    ("30066822",  "COÖPERATIEVE RABOBANK UA",                 "Finance",         None,          12000),
    ("30067099",  "ACHMEA BV",                                "Finance",         None,          22000),
    ("31037413",  "JUMBO GROEP HOLDING BV",                   "Retail",          None,           9000),
    ("27189379",  "VION FOOD GROUP BV",                       "Consumer Goods",  None,           8000),
    ("37001959",  "ROYAL COSUN UA",                           "Consumer Goods",  None,           3000),
    ("29039850",  "DE LAGE LANDEN INTERNATIONAL BV",          "Finance",         None,           2500),
    ("27177017",  "BOSCH NEDERLAND BV",                       "Manufacturing",   None,           3000),
]


class NetherlandsCuratedScraper(BaseScraper):
    name = "kvk_nl_curated"
    country = "NL"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        checkpoint = self.load_checkpoint() if resume else {}
        done = set(checkpoint.get("done_kvk_nl_curated", []))
        seen_names: set[str] = set()

        for kvk_nr, name, sector, ticker, rev_estimate in TOP_DUTCH_COMPANIES:
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
                        currency = info.get("currency", "EUR")
                        if currency == "EUR":
                            revenue_eur = float(rev_raw)
                        elif currency == "USD":
                            revenue_eur = rev_raw * 0.93
                        elif currency == "GBP":
                            revenue_eur = rev_raw * 1.17
                        revenue_estimated = False
                        logger.info(f"[NL] {name}: {revenue_eur/1e6:.0f}M EUR (YF)")
                except Exception as e:
                    logger.debug(f"[NL] YF failed {ticker}: {e}")

            if revenue_eur is None:
                revenue_eur = rev_estimate * 1_000_000
                revenue_estimated = True

            yield CompanyRecord(
                name=name,
                country="NL",
                registration_number=kvk_nr,
                revenue_eur=revenue_eur,
                revenue_estimated=revenue_estimated,
                employees=None,
                sector=sector,
                nace_code=None,
                creation_date=None,
                city=None,
                source_url="https://www.kvk.nl/",
                directors=[],
            )

            done.add(name_key)
            checkpoint["done_kvk_nl_curated"] = list(done)
            self.save_checkpoint(checkpoint)
            await asyncio.sleep(0.05)
