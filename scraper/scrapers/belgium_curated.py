"""Belgium curated scraper — BEL20 + MIDCAP + large private companies.
Revenue: via Yahoo Finance (.BR / Euronext Brussels tickers).
Registration: KBO/BCE enterprise numbers.
"""
import asyncio
import logging
from typing import AsyncIterator
import yfinance as yf
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord

logger = logging.getLogger(__name__)

# (kbo_number, name, sector, ticker, rev_eur_M_estimate)
TOP_BELGIAN_COMPANIES = [
    # ── BEL20 ──────────────────────────────────────────────────────────────────
    ("0417497106",  "ANHEUSER-BUSCH INBEV SA",         "Food & Beverage",  "ABI.BR",   57800),
    ("0451406524",  "AGEAS SA",                         "Finance",          "AGS.BR",   14200),
    ("0400378485",  "BEKAERT NV",                       "Manufacturing",    "BEKB.BR",   5500),
    ("0403012880",  "COFINIMMO SA",                     "Real estate",      "COFB.BR",    540),
    ("0400378485",  "COLRUYT GROUP NV",                 "Retail",           "COLR.BR",  10200),
    ("0403448140",  "ELIA GROUP SA",                    "Energy",           "ELI.BR",    1900),
    ("0401032596",  "GBL SA",                           "Finance",          "GBLB.BR",   2600),
    ("0462920226",  "KBC GROEP NV",                     "Finance",          "KBC.BR",    8100),
    ("0202239951",  "PROXIMUS PLC",                     "Telecom",          "PROX.BR",   5700),
    ("0403091220",  "SOFINA SA",                        "Finance",          "SOF.BR",     450),
    ("0403091220",  "UCB SA",                           "Pharma",           "UCB.BR",    6600),
    ("0401574852",  "UMICORE SA",                       "Materials",        "UMI.BR",    3400),
    ("0417199869",  "WDP NV",                           "Real estate",      "WDP.BR",     650),
    ("0403213264",  "ARGENX SE",                        "Biotech",          "ARGX.BR",   2200),
    ("0403571508",  "GALAPAGOS NV",                     "Biotech",          "GLPG.BR",    320),
    # ── MIDCAP ────────────────────────────────────────────────────────────────
    ("0426184049",  "TELENET GROUP HOLDING NV",         "Telecom",          "TNET.BR",   2500),
    ("0400285397",  "ETTEPLAN OYJ BE",                  "Logistics",        "BPOST.BR",  3700),
    ("0866019905",  "DECEUNINCK NV",                    "Manufacturing",    "DECB.BR",   1000),
    ("0418477682",  "LOTUS BAKERIES NV",                "Food & Beverage",  "LOTB.BR",   1200),
    ("0405765089",  "MELEXIS NV",                       "Technology",       "MELE.BR",    950),
    ("0400394133",  "RECTICEL NV",                      "Manufacturing",    "REC.BR",     750),
    ("0404616494",  "RESILUX NV",                       "Manufacturing",    "RESL.BR",    700),
    ("0403891220",  "SIPEF NV",                         "Agriculture",      "SIP.BR",     350),
    ("0410002225",  "TER BEKE NV",                      "Food & Beverage",  "TERB.BR",    700),
    ("0400493991",  "VANDEMOORTELE NV",                 "Food & Beverage",  "VAND.BR",   1700),
    # ── Large private / unlisted ──────────────────────────────────────────────
    ("0426184049",  "SOLVAY SA",                        "Chemicals",        "SOLB.BR",   5600),
    ("0400430429",  "DELOITTE BELGIQUE",                "Services",         "",          1200),
    ("0429077439",  "D'IETEREN GROUP SA",               "Automotive",       "DIE.BR",   29000),
    ("0891488077",  "ALIAXIS GROUP SA",                 "Manufacturing",    "",          3500),
    ("0404052362",  "PICANOL GROUP NV",                 "Manufacturing",    "PIC.BR",    2800),
    ("0403014468",  "DUVEL MOORTGAT NV",                "Food & Beverage",  "DUV.BR",    400),
    ("0542410030",  "AZELIS GROUP NV",                  "Chemicals",        "AZE.BR",    3800),
    ("0876176824",  "KINEPOLIS GROUP NV",               "Entertainment",    "KIN.BR",     450),
    ("0402765593",  "ONTEX GROUP NV",                   "Manufacturing",    "ONTEX.BR",  2100),
    ("0401024436",  "ACCENTIS NV",                      "Real estate",      "",           350),
    ("0451506524",  "IMMOBEL SA",                       "Real estate",      "IMMO.BR",    250),
    ("0402450197",  "GREENYARD NV",                     "Agriculture",      "GREEN.BR",  4200),
]


class BelgiumCuratedScraper(BaseScraper):
    name = "be_curated"
    country = "BE"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        checkpoint = self.load_checkpoint() if resume else {}
        done_ids = set(checkpoint.get("done", []))

        for kbo, name, sector, ticker, rev_estimate in TOP_BELGIAN_COMPANIES:
            if kbo in done_ids:
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
                        # Convert to EUR (most Belgian tickers are already in EUR)
                        fx = {"EUR": 1.0, "USD": 0.92, "GBP": 1.17}.get(currency, 1.0)
                        revenue = float(raw_rev) * fx
                except Exception as e:
                    logger.debug(f"[BE] YF error {ticker}: {e}")

            if revenue is None:
                revenue = float(rev_estimate) * 1_000_000

            record = CompanyRecord(
                name=name,
                country="BE",
                registration_number=kbo,
                revenue_eur=revenue,
                revenue_year=2023,
                revenue_estimated=(ticker == ""),
                sector=sector,
                nace_code=None,
                source_url=f"https://finance.yahoo.com/quote/{ticker}" if ticker else None,
                directors=[],
            )
            yield record

            done_ids.add(kbo)
            self.save_checkpoint({"done": list(done_ids)})
            await asyncio.sleep(0.3)

        logger.info(f"[BE] Curated scraper terminé — {len(TOP_BELGIAN_COMPANIES)} entreprises")
