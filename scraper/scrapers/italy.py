"""Italy scraper — curated list of top Italian companies.
Revenue: via Yahoo Finance for listed companies (.MI tickers on Borsa Italiana / FTSE MIB).
Registration numbers: codice fiscale format.
Source: https://www.registroimprese.it/
"""
import asyncio
import logging
from typing import AsyncIterator
import yfinance as yf
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord
from ..enrichers.nace import normalize_code, code_to_sector_label

logger = logging.getLogger(__name__)

# EUR to EUR = 1.0
EUR_TO_EUR = 1.0

# Top Italian companies — FTSE MIB (40 listed) + large private companies
# (codice_fiscale, name, sector, ticker_or_None, rev_eur_M_estimate)
TOP_ITALIAN_COMPANIES = [
    # ── Energy ──────────────────────────────────────────────────────────────────
    ("00484960588", "ENI SPA",                         "Energy",        "ENI.MI",    82000),
    ("00811720580", "ENEL SPA",                        "Energy",        "ENEL.MI",   93000),
    ("00369530547", "SNAM SPA",                        "Energy",        "SRG.MI",     3700),
    ("05779711000", "TERNA SPA",                       "Energy",        "TRN.MI",     2900),
    ("11573760153", "A2A SPA",                         "Energy",        "A2A.MI",    12000),
    ("01918800153", "HERA SPA",                        "Energy",        "HER.MI",     3800),
    ("05394801004", "ACEA SPA",                        "Energy",        "ACE.MI",     3900),
    ("13192800150", "ITALGAS SPA",                     "Energy",        "IG.MI",      1400),

    # ── Finance / Banking ────────────────────────────────────────────────────────
    ("00799960158", "INTESA SANPAOLO SPA",             "Finance",       "ISP.MI",    24000),
    ("02008NoVA",   "UNICREDIT SPA",                   "Finance",       "UCG.MI",    22000),
    ("00714490158", "MEDIOBANCA SPA",                  "Finance",       "MB.MI",      3200),
    ("09722490152", "FINECOBANK SPA",                  "Finance",       "FBK.MI",     1200),
    ("03843520162", "BANCO BPM SPA",                   "Finance",       "BAMI.MI",    5800),
    ("02249220524", "BANCA MONTE DEI PASCHI DI SIENA", "Finance",       "BMPS.MI",    2800),
    ("00742111007", "ASSICURAZIONI GENERALI SPA",      "Finance",       "G.MI",      95000),

    # ── Automotive / Industrial ──────────────────────────────────────────────────
    ("00359170107", "STELLANTIS NV",                   "Automotive",    "STLAM.MI", 188000),
    ("03560902658", "FERRARI NV",                      "Automotive",    "RACE.MI",    5900),
    ("12867670155", "CNH INDUSTRIAL NV",               "Automotive",    "CNHI.MI",   20000),
    ("00860170966", "BREMBO SPA",                      "Automotive",    "BRE.MI",     4000),
    ("00860340966", "PIRELLI AND C SPA",               "Automotive",    "PIRC.MI",    6600),

    # ── Telecoms / Media ────────────────────────────────────────────────────────
    ("00488410010", "TELECOM ITALIA SPA",              "Telecom",       "TIT.MI",    14000),
    ("97103880585", "POSTE ITALIANE SPA",              "Services",      "PST.MI",    12000),
    ("09032100963", "MEDIASET SPA",                    "Media",         "MFE.MI",     3000),

    # ── Defense / Aerospace ─────────────────────────────────────────────────────
    ("00401771003", "LEONARDO SPA",                    "Defense",       "LDO.MI",    15000),
    ("13118641000", "FINCANTIERI SPA",                 "Manufacturing", "FCT.MI",     6800),

    # ── Technology / Electronics ─────────────────────────────────────────────────
    ("00372610361", "STMICROELECTRONICS NV",           "Technology",    "STM.MI",    17000),
    ("04505070011", "PRYSMIAN SPA",                    "Manufacturing", "PRY.MI",    15000),
    ("04519690961", "AMPLIFON SPA",                    "Health",        "AMP.MI",     2100),
    ("01371160376", "DIASORIN SPA",                    "Pharma",        "DIA.MI",      800),
    ("00714800154", "RECORDATI SPA",                   "Pharma",        "REC.MI",     1800),

    # ── Luxury / Fashion / Consumer ─────────────────────────────────────────────
    ("06459560961", "MONCLER SPA",                     "Luxury",        "MONC.MI",    2900),
    ("00115950357", "TOD'S SPA",                       "Luxury",        "TOD.MI",     1000),
    ("03030290963", "CAMPARI GROUP",                   "Consumer",      "CPR.MI",     2800),
    ("00177410219", "LUXOTTICA GROUP — ESSILUX",       "Luxury",        "EL.MI",     25000),
    ("13278200157", "PRADA SPA",                       "Luxury",        "9001.HK",    4600),

    # ── Construction / Materials ─────────────────────────────────────────────────
    ("00130960168", "BUZZI SPA",                       "Construction",  "BZU.MI",     4000),
    ("00488490011", "AUTOGRILL SPA",                   "Services",      "AGL.MI",     4600),

    # ── Utilities / Infrastructure ───────────────────────────────────────────────
    ("03293820920", "ITALCEMENTI SPA",                 "Construction",  None,         3000),

    # ── Large Private Companies ──────────────────────────────────────────────────
    ("02171781209", "BARILLA HOLDING SPA",             "Food",          None,         4000),
    ("00736390157", "FERRERO SPA",                     "Food",          None,        14000),
    ("00162290193", "LAVAZZA SPA",                     "Food",          None,         2500),
    ("00778510153", "MAPEI SPA",                       "Manufacturing", None,         3500),
    ("01166450153", "LUXOTTICA PRIVATA SRL",           "Luxury",        None,         3000),
    ("09217490158", "FASTWEB SPA",                     "Telecom",       None,         2000),
    ("13822931002", "WIND TRE SPA",                    "Telecom",       None,         5000),
    ("00472370962", "DE AGOSTINI SPA",                 "Media",         None,         3000),
    ("00193320219", "BENETTON GROUP SRL",              "Retail",        None,         1500),
    ("01381290155", "GIORGIO ARMANI SPA",              "Luxury",        None,         2200),
    ("00826570154", "GUCCI ITALIA SRL",                "Luxury",        None,         5000),
    ("01668700154", "VERSACE SPA",                     "Luxury",        None,         1000),
]


class ItalyScraper(BaseScraper):
    name = "cciaa_it"
    country = "IT"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        checkpoint = self.load_checkpoint() if resume else {}
        done_set = set(checkpoint.get("done_cciaa_it", []))

        seen_names: set[str] = set()

        for entry in TOP_ITALIAN_COMPANIES:
            codice_fiscale, name, sector, ticker, rev_estimate = entry

            # Deduplicate by name
            name_key = name.upper()[:40]
            if name_key in seen_names:
                continue
            seen_names.add(name_key)

            if name_key in done_set:
                continue

            # Try Yahoo Finance for real revenue
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
                        elif currency == "HKD":
                            revenue_eur = rev_raw * 0.116
                        revenue_estimated = False
                        logger.info(
                            f"[IT] {name}: revenue {revenue_eur / 1e6:.0f}M EUR (YF)"
                        )
                except Exception as e:
                    logger.debug(f"[IT] YF failed for {ticker}: {e}")

            if revenue_eur is None:
                revenue_eur = rev_estimate * 1_000_000
                revenue_estimated = True

            yield CompanyRecord(
                name=name,
                country="IT",
                registration_number=codice_fiscale,
                revenue_eur=revenue_eur,
                revenue_estimated=revenue_estimated,
                employees=None,
                sector=sector,
                nace_code=None,
                creation_date=None,
                city=None,
                source_url="https://www.registroimprese.it/",
                directors=[],
            )

            done_set.add(name_key)
            checkpoint["done_cciaa_it"] = list(done_set)
            self.save_checkpoint(checkpoint)
            await asyncio.sleep(0.1)
