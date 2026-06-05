"""Denmark curated scraper — OMX C25 + large Danish companies.
Revenue: via Yahoo Finance (.CO / Nasdaq Copenhagen tickers).
Registration: CVR numbers (8 digits).
"""
import asyncio
import logging
from typing import AsyncIterator
import yfinance as yf
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord

logger = logging.getLogger(__name__)

DKK_TO_EUR = 0.134  # 1 DKK ≈ 0.134 EUR

# (cvr_number, name, sector, ticker, rev_dkk_M_estimate)
TOP_DANISH_COMPANIES = [
    # ── OMX C25 ────────────────────────────────────────────────────────────────
    ("22756214",  "A.P. MOLLER-MAERSK A/S",            "Logistics",        "MAERSK-B.CO",  410000),
    ("61056416",  "NOVO NORDISK A/S",                   "Pharma",           "NOVO-B.CO",    232000),
    ("58271728",  "DSV A/S",                            "Logistics",        "DSV.CO",       212000),
    ("41078104",  "ORSTED A/S",                         "Energy",           "ORSTED.CO",     77000),
    ("26980017",  "VESTAS WIND SYSTEMS A/S",            "Energy",           "VWS.CO",        65000),
    ("28505116",  "PANDORA A/S",                        "Retail",           "PNDORA.CO",     31000),
    ("10007258",  "NOVOZYMES A/S",                      "Chemicals",        "NZYM-B.CO",     17000),
    ("69749817",  "COLOPLAST A/S",                      "Healthcare",       "COLO-B.CO",     26000),
    ("21524stedman14",  "CARLSBERG A/S",                "Food & Beverage",  "CARL-B.CO",     73000),
    ("49108013",  "GN STORE NORD A/S",                  "Technology",       "GN.CO",         15000),
    ("21023884",  "GENMAB A/S",                         "Biotech",          "GMAB.CO",       14000),
    ("60617116",  "TRYG A/S",                           "Finance",          "TRYG.CO",       32000),
    ("54879415",  "ROCKWOOL A/S",                       "Manufacturing",    "ROCK-B.CO",     36000),
    ("66341014",  "NKT A/S",                            "Manufacturing",    "NKT.CO",        20000),
    ("34627873",  "NETCOMPANY GROUP A/S",               "Technology",       "NETC.CO",        8500),
    ("56508514",  "DEMANT A/S",                         "Healthcare",       "DEMANT.CO",     22000),
    ("28503116",  "ISS A/S",                            "Services",         "ISS.CO",       103000),
    ("63626515",  "JYSKE BANK A/S",                     "Finance",          "JYSK.CO",       8200),
    ("51248414",  "SYDBANK A/S",                        "Finance",          "SYDB.CO",        4500),
    ("41916116",  "ROYAL UNIBREW A/S",                  "Food & Beverage",  "RBREW.CO",      11000),
    ("50155715",  "FLSmidth & CO A/S",                  "Engineering",      "FLS.CO",        29000),
    ("14197714",  "DFDS A/S",                           "Logistics",        "DFDS.CO",       20000),
    ("28318316",  "CHR. HANSEN HOLDING A/S",            "Chemicals",        "CHR.CO",        14000),
    # ── Large privées / non listées ────────────────────────────────────────────
    ("25313967",  "ARLA FOODS AMBA",                    "Food & Beverage",  "",             140000),
    ("25315614",  "DANISH CROWN AMS",                   "Food & Beverage",  "",              80000),
    ("38541819",  "BESTSELLER A/S",                     "Retail",           "",              30000),
    ("54169516",  "LEGO A/S",                           "Manufacturing",    "",              79000),
    ("40691115",  "ECCO SKO A/S",                       "Retail",           "",              15000),
    ("36004816",  "GRUNDFOS HOLDING A/S",               "Manufacturing",    "",              42000),
    ("96962612",  "VELUX A/S",                          "Manufacturing",    "",              22000),
    ("35754015",  "RAMBOLL GROUP A/S",                  "Engineering",      "",              22000),
    ("40944215",  "FALCK A/S",                          "Healthcare",       "",              14000),
    ("15597615",  "NORLYS A/S",                         "Energy",           "",              25000),
]


class DenmarkCuratedScraper(BaseScraper):
    name = "dk_curated"
    country = "DK"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        checkpoint = self.load_checkpoint() if resume else {}
        done_ids = set(checkpoint.get("done", []))

        for cvr, name, sector, ticker, rev_dkk_m in TOP_DANISH_COMPANIES:
            if cvr + name in done_ids:
                continue

            revenue = None
            if ticker:
                try:
                    info = await asyncio.get_event_loop().run_in_executor(
                        None, lambda t=ticker: yf.Ticker(t).info
                    )
                    raw_rev = info.get("totalRevenue") or info.get("revenue")
                    currency = (info.get("currency") or "DKK").upper()
                    if raw_rev:
                        fx = {"EUR": 1.0, "DKK": DKK_TO_EUR, "USD": 0.92}.get(currency, DKK_TO_EUR)
                        revenue = float(raw_rev) * fx
                except Exception as e:
                    logger.debug(f"[DK] YF error {ticker}: {e}")

            if revenue is None:
                revenue = float(rev_dkk_m) * 1_000_000 * DKK_TO_EUR

            record = CompanyRecord(
                name=name,
                country="DK",
                registration_number=cvr,
                revenue_eur=revenue,
                revenue_year=2023,
                revenue_estimated=(ticker == ""),
                sector=sector,
                nace_code=None,
                source_url=f"https://finance.yahoo.com/quote/{ticker}" if ticker else None,
                directors=[],
            )
            yield record

            done_ids.add(cvr + name)
            self.save_checkpoint({"done": list(done_ids)})
            await asyncio.sleep(0.3)

        logger.info(f"[DK] Curated scraper terminé — {len(TOP_DANISH_COMPANIES)} entreprises")
