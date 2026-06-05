"""Portugal scraper — PSI 20 large caps + grandes entreprises privées.
Revenue: via Yahoo Finance (.LS tickers) + curated estimates.
Registration: NIF (Número de Identificação Fiscal, 9 digits).
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

# (nif, name, sector, ticker, rev_eur_M_estimate)
TOP_PORTUGUESE_COMPANIES = [
    # ── GRANDES COTÉES (PSI 20 / Euronext Lisbon) ────────────────────────────
    ("500697256",  "EDP - ENERGIAS DE PORTUGAL SA",              "Energy",          "EDP.LS",     19000),
    ("502264041",  "EDP RENOVÁVEIS SA",                          "Energy",          "EDPR.LS",     3500),
    ("504499777",  "GALP ENERGIA SGPS SA",                       "Energy",          "GALP.LS",    18000),
    ("502931491",  "NOS SGPS SA",                                "Telecom",         "NOS.LS",      1700),
    ("500273170",  "SONAE SGPS SA",                              "Retail",          "SON.LS",      8000),
    ("500033279",  "MOTA-ENGIL SGPS SA",                         "Construction",    "EGL.LS",      3000),
    ("500750997",  "JERÓNIMO MARTINS SGPS SA",                   "Retail",          "JMT.LS",     26000),
    ("503398504",  "THE NAVIGATOR COMPANY SA",                   "Manufacturing",   "NVG.LS",      2000),
    ("504176498",  "REN - REDES ENERGÉTICAS NACIONAIS SGPS SA",  "Energy",          "RENE.LS",      800),
    ("500077568",  "CTT - CORREIOS DE PORTUGAL SA",              "Logistics",       "CTT.LS",       900),
    ("500109093",  "SEMAPA SGPS SA",                             "Manufacturing",   "SEM.LS",      1200),
    ("503219694",  "ALTRI SGPS SA",                              "Manufacturing",   "ALTR.LS",     1000),
    ("500070294",  "CORTICEIRA AMORIM SGPS SA",                  "Manufacturing",   "COR.LS",      1000),
    ("501525882",  "BANCO COMERCIAL PORTUGUÊS SA",               "Finance",         "BCP.LS",      2200),
    ("506525762",  "SPORT LISBOA E BENFICA SAD",                 "Services",        "SLBEN.LS",     400),
    ("502849704",  "IMPRESA SGPS SA",                            "Media",           "IPR.LS",       300),
    ("502293225",  "COFINA SGPS SA",                             "Media",           "CFN.LS",       100),
    # ── GRANDES PRIVÉES ───────────────────────────────────────────────────────
    ("501525882",  "NOVO BANCO SA",                              "Finance",         None,           1800),
    ("500960046",  "CAIXA GERAL DE DEPÓSITOS SA",                "Finance",         None,           2000),
    ("500278725",  "TAP AIR PORTUGAL SA",                        "Transport",       None,           3200),
    ("500836570",  "CP - COMBOIOS DE PORTUGAL EPE",              "Transport",       None,            500),
    ("502181212",  "AUTOEUROPA - AUTOMÓVEIS LDA",                "Automotive",      None,           5000),
    ("502396525",  "SONAE SIERRA SGPS SA",                       "Real Estate",     None,            500),
    ("500543456",  "CONTINENTE (SONAE MC SGPS SA)",              "Retail",          None,           6000),
    ("502819615",  "ALTICE PORTUGAL SA",                         "Telecom",         None,           2000),
    ("500715891",  "VODAFONE PORTUGAL SA",                       "Telecom",         None,           1000),
    ("501625687",  "REFER / INFRAESTRUTURAS DE PORTUGAL SA",     "Transport",       None,            800),
    ("503711650",  "BRISA AUTOESTRADAS DE PORTUGAL SA",          "Transport",       None,            700),
    ("500284257",  "GRUPO PESTANA SGPS SA",                      "Services",        None,            600),
    ("500077568",  "MILLENNIUM BCP AGEAS SEGUROS SA",            "Finance",         None,            500),
]


class PortugalScraper(BaseScraper):
    name = "rnpc_pt"
    country = "PT"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        checkpoint = self.load_checkpoint() if resume else {}
        done = set(checkpoint.get("done_rnpc_pt", []))
        seen_names: set[str] = set()

        for nif, name, sector, ticker, rev_estimate in TOP_PORTUGUESE_COMPANIES:
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
                        logger.info(f"[PT] {name}: {revenue_eur/1e6:.0f}M EUR (YF)")
                except Exception as e:
                    logger.debug(f"[PT] YF failed {ticker}: {e}")

            if revenue_eur is None:
                revenue_eur = rev_estimate * 1_000_000
                revenue_estimated = True

            yield CompanyRecord(
                name=name,
                country="PT",
                registration_number=nif,
                revenue_eur=revenue_eur,
                revenue_estimated=revenue_estimated,
                employees=None,
                sector=sector,
                nace_code=None,
                creation_date=None,
                city=None,
                source_url="https://www.racius.com/",
                directors=[],
            )

            done.add(name_key)
            checkpoint["done_rnpc_pt"] = list(done)
            self.save_checkpoint(checkpoint)
            await asyncio.sleep(0.05)
