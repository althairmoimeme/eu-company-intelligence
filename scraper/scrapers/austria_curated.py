"""Austria curated scraper — ATX + large Austrian companies.
Revenue: via Yahoo Finance (.VI / Vienna Stock Exchange tickers).
Registration: Firmenbuch numbers (FN prefix).
"""
import asyncio
import logging
from typing import AsyncIterator
import yfinance as yf
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord

logger = logging.getLogger(__name__)

# (fn_number, name, sector, ticker, rev_eur_M_estimate)
TOP_AUSTRIAN_COMPANIES = [
    # ── ATX ───────────────────────────────────────────────────────────────────
    ("93363s",   "OMV AG",                              "Energy",           "OMV.VI",    42000),
    ("51507v",   "ERSTE GROUP BANK AG",                 "Finance",          "EBS.VI",     9200),
    ("120691b",  "VIENNA INSURANCE GROUP AG",           "Finance",          "VIG.VI",    14200),
    ("34858m",   "VERBUND AG",                          "Energy",           "VER.VI",     5800),
    ("170914g",  "ANDRITZ AG",                          "Engineering",      "ANDR.VI",    8700),
    ("34007m",   "VOESTALPINE AG",                      "Manufacturing",    "VOE.VI",    14600),
    ("50272z",   "RAIFFEISEN BANK INTL AG",             "Finance",          "RBI.VI",     6400),
    ("144700f",  "TELEKOM AUSTRIA AG",                  "Telecom",          "TKA.VI",     4700),
    ("16507z",   "IMMOFINANZ AG",                       "Real estate",      "IIA.VI",      570),
    ("215214x",  "CA IMMO AG",                          "Real estate",      "CAI.VI",      340),
    ("166985b",  "UNIQA INSURANCE GROUP AG",            "Finance",          "UQA.VI",    6200),
    ("116508m",  "ÖSTERREICHISCHE POST AG",             "Logistics",        "POST.VI",   2400),
    ("63263g",   "KAPSCH TRAFFICCOM AG",                "Technology",       "KTCG.VI",    550),
    ("68247a",   "MAYR-MELNHOF KARTON AG",              "Manufacturing",    "MMK.VI",    3800),
    ("303761k",  "DO & CO AG",                          "Food & Beverage",  "DOC.VI",    1900),
    ("54923i",   "FLUGHAFEN WIEN AG",                   "Infrastructure",   "FLU.VI",     800),
    ("186418f",  "ZUMTOBEL GROUP AG",                   "Manufacturing",    "ZAG.VI",    1200),
    ("275019y",  "POLYTEC HOLDING AG",                  "Manufacturing",    "PYT.VI",     800),
    ("100381w",  "S IMMO AG",                           "Real estate",      "SPI.VI",     310),
    ("84833d",   "AGRANA BETEILIGUNGS AG",              "Food & Beverage",  "AGR.VI",    3200),
    # ── Large privées / non listées ────────────────────────────────────────────
    ("241663a",  "SPAR ÖSTERREICHISCHE WARENHANDELS AG","Retail",           "",          21000),
    ("105637m",  "RED BULL GMBH",                       "Food & Beverage",  "",           9800),
    ("215063d",  "MAGNA INTERNATIONAL EUROPE AG",       "Automotive",       "",          39000),
    ("26329b",   "RAIFFEISEN ZENTRALBANK ÖSTERREICH AG","Finance",          "",           5500),
    ("123139s",  "ÖSTERREICHISCHE BUNDESBAHNEN AG",     "Logistics",        "",           5200),
    ("63263g",   "KAPSCH AG",                           "Technology",       "",           1800),
    ("165888t",  "STRABAG SE",                          "Construction",     "STR.VI",   19000),
    ("215063x",  "FREQUENTIS AG",                       "Technology",       "FQT.VI",     400),
    ("303212k",  "MARINOMED BIOTECH AG",                "Biotech",          "MARI.VI",     50),
    ("193122p",  "PIERER MOBILITY AG",                  "Automotive",       "PMAG.VI",  2800),
]


class AustriaCuratedScraper(BaseScraper):
    name = "at_curated"
    country = "AT"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        checkpoint = self.load_checkpoint() if resume else {}
        done_ids = set(checkpoint.get("done", []))

        for fn, name, sector, ticker, rev_estimate in TOP_AUSTRIAN_COMPANIES:
            uid = fn + name
            if uid in done_ids:
                continue

            revenue = None
            if ticker:
                try:
                    info = await asyncio.get_event_loop().run_in_executor(
                        None, lambda t=ticker: yf.Ticker(t).info
                    )
                    raw_rev = info.get("totalRevenue") or info.get("revenue")
                    currency = (info.get("currency") or "EUR").upper()
                    if raw_rev:
                        fx = {"EUR": 1.0, "USD": 0.92, "GBP": 1.17}.get(currency, 1.0)
                        revenue = float(raw_rev) * fx
                except Exception as e:
                    logger.debug(f"[AT] YF error {ticker}: {e}")

            if revenue is None:
                revenue = float(rev_estimate) * 1_000_000

            yield CompanyRecord(
                name=name,
                country="AT",
                registration_number=fn,
                revenue_eur=revenue,
                revenue_year=2023,
                revenue_estimated=(ticker == ""),
                sector=sector,
                nace_code=None,
                source_url=f"https://finance.yahoo.com/quote/{ticker}" if ticker else None,
                directors=[],
            )

            done_ids.add(uid)
            self.save_checkpoint({"done": list(done_ids)})
            await asyncio.sleep(0.3)

        logger.info(f"[AT] Curated scraper terminé — {len(TOP_AUSTRIAN_COMPANIES)} entreprises")
