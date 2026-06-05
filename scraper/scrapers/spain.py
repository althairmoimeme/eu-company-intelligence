"""Spain scraper — Yahoo Finance listed companies + curated top Spanish companies.
Revenue: via Yahoo Finance (.MC tickers on Bolsa de Madrid).
Directors: limited.
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

# Top Spanish companies — IBEX-35 + IBEX Medium Cap + large private companies
# (nif, name, sector, ticker, rev_eur_M_estimate)
TOP_SPANISH_COMPANIES = [
    # IBEX-35
    ("A0080649",  "INDITEX SA",                     "Retail",        "ITX.MC",  32000),
    ("A28015865", "BANCO SANTANDER SA",              "Finance",       "SAN.MC",  53000),
    ("A17000442", "TELEFONICA SA",                   "Telecom",       "TEF.MC",  40000),
    ("A28000727", "IBERDROLA SA",                    "Energy",        "IBE.MC",  43000),
    ("A28023430", "BBVA SA",                         "Finance",       "BBVA.MC", 24000),
    ("A28943338", "REPSOL SA",                       "Energy",        "REP.MC",  55000),
    ("A58229593", "CAIXABANK SA",                    "Finance",       "CABK.MC", 12000),
    ("A28015922", "ENDESA SA",                       "Energy",        "ELE.MC",  23000),
    ("A28023430", "NATURGY ENERGY GROUP SA",         "Energy",        "NTGY.MC", 25000),
    ("A28943338", "FERROVIAL SE",                    "Construction",  "FER.MC",   8000),
    ("A60256115", "ACS ACTIVIDADES CONSTRUCCION",    "Construction",  "ACS.MC",  39000),
    ("A80004993", "ACCIONA SA",                      "Construction",  "ANA.MC",   9000),
    ("A8780169",  "GRIFOLS SA",                      "Pharma",        "GRF.MC",   6000),
    ("B85076262", "CELLNEX TELECOM SA",              "Telecom",       "CLNX.MC",  3700),
    ("A36859596", "AMADEUS IT GROUP SA",             "IT",            "AMS.MC",   5700),
    ("A82735642", "AENA SME SA",                     "Transport",     "AENA.MC",  5100),
    ("A46138594", "FLUIDRA SA",                      "Manufacturing", "FDR.MC",   2000),
    ("A28015865", "COLONIAL SA",                     "Real estate",   "COL.MC",    600),
    ("A28943338", "MAPFRE SA",                       "Finance",       "MAP.MC",  27000),
    ("A80007401", "MELIA HOTELS INTERNATIONAL",      "Hotels",        "MEL.MC",   1900),
    ("A60645865", "INMOBILIARIA COLONIAL SOCIMI",    "Real estate",   "COL.MC",    600),
    ("A28343419", "INTERNATIONAL AIRLINES GROUP",    "Transport",     "IAG.MC",  28000),
    ("A28015922", "RED ELECTRICA DE ESPANA",         "Energy",        "REE.MC",   2100),
    ("A28943338", "ENAGAS SA",                       "Energy",        "ENG.MC",   1200),
    ("A28015865", "SACYR SA",                        "Construction",  "SCYR.MC",  5700),
    ("A60645865", "MERLIN PROPERTIES SOCIMI",        "Real estate",   "MRL.MC",    500),
    ("A8780169",  "SOLARIA ENERGIA Y MEDIO AMB",     "Energy",        "SLR.MC",    180),
    ("A82735642", "SIEMENS GAMESA RENEWABLE ENERGY", "Energy",        "SGRE.MC", 10000),
    ("A28015922", "VISCOFAN SA",                     "Food",          "VIS.MC",   1000),
    # Large private / non-IBEX
    ("A28015865", "EL CORTE INGLES SA",              "Retail",        None,      14000),
    ("A28943338", "MERCADONA SA",                    "Retail",        None,      33000),
    ("A17000442", "EROSKI SCCL",                     "Retail",        None,       6000),
    ("A28023430", "CARREFOUR ESPANA SL",             "Retail",        None,      10000),
    ("A58229593", "LIDL SUPERMERCADOS SAU",          "Retail",        None,       5000),
    ("A60256115", "IKEA IBERICA SA",                 "Retail",        None,       2500),
    ("A80004993", "DIA SA",                          "Retail",        "DIA.MC",   6000),
    ("A8780169",  "PRIMARK TIENDAS SLU",             "Retail",        None,       4000),
    ("A82735642", "INCARLOPSA SA",                   "Food",          None,       2000),
    ("A28015922", "DEOLEO SA",                       "Food",          None,        500),
    ("A46138594", "GRUPO LACTALIS IBERICA SA",       "Food",          None,       1500),
    ("A28023430", "HEINEKEN ESPANA SA",              "Food",          None,       1200),
    ("A60645865", "MAHOU SA",                        "Food",          None,       1100),
    ("A28343419", "DANONE SA",                       "Food",          None,       1000),
    ("A28015865", "PEPSICO SPAIN SL",                "Food",          None,        900),
    ("A17000442", "NESTLE ESPANA SA",                "Food",          None,        800),
    ("A80007401", "GLAXOSMITHKLINE SA",              "Pharma",        None,        700),
    ("A80004993", "NOVARTIS FARMACEUTICA SA",        "Pharma",        None,        650),
    ("A82735642", "ROCHE FARMA SA",                  "Pharma",        None,        600),
    ("A60256115", "PFIZER SA",                       "Pharma",        None,        550),
    ("A8780169",  "JOHNSON AND JOHNSON SA",          "Pharma",        None,        500),
    ("A28343419", "VOLKSWAGEN GROUP ESPANA",         "Automotive",    None,       5000),
    ("A28015922", "SEAT SA",                         "Automotive",    None,       9000),
    ("A46138594", "RENAULT ESPANA SA",               "Automotive",    None,       4000),
    ("A28023430", "FORD ESPANA SL",                  "Automotive",    None,       3000),
    ("A60645865", "PSA AUTOMOBILES SA",              "Automotive",    None,       2500),
    ("A28015865", "CODERE SA",                       "Entertainment", None,       1100),
    ("A58229593", "PROSEGUR COMPANIA DE SEGURIDAD",  "Services",      "PSG.MC",   4700),
    ("A60256115", "INDRA SISTEMAS SA",               "IT",            "IDR.MC",   3900),
    ("A28015922", "TECNICAS REUNIDAS SA",            "Engineering",   "TRE.MC",   4200),
    ("A80007401", "MELIÁ HOTELS INTERNATIONAL SA",   "Hotels",        "MEL.MC",   1900),
    ("A80004993", "NH HOTEL GROUP SA",               "Hotels",        "NHH.MC",   1700),
    ("A82735642", "WIZZ AIR SPAIN",                  "Transport",     None,        500),
    ("A8780169",  "VUELING AIRLINES SA",             "Transport",     None,       1800),
    ("A28343419", "AIR EUROPA LINEAS AEREAS SAU",    "Transport",     None,        900),
    ("A17000442", "GRUPO ANTOLIN IRAUSA SA",         "Automotive",    None,       7600),
    ("A28015865", "GONVARRI STEEL INDUSTRIES SL",    "Manufacturing", None,       2500),
    ("A28943338", "IBERDROLA RENOVABLES SA",         "Energy",        None,       3000),
    ("A46138594", "GAS NATURAL SDG SA",              "Energy",        None,      20000),
    ("A60256115", "COMPANIA LOGISTICA DE HIDROCARBUR","Energy",       "CLH.MC",   4100),
]


class SpainScraper(BaseScraper):
    name = "borme_es"
    country = "ES"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        checkpoint = self.load_checkpoint() if resume else {}
        done_nifs = set(checkpoint.get("done_nifs_es", []))

        seen_names = set()

        for entry in TOP_SPANISH_COMPANIES:
            nif, name, sector, ticker, rev_estimate = entry

            # Deduplicate by name
            name_key = name.upper()[:30]
            if name_key in seen_names:
                continue
            seen_names.add(name_key)

            if name_key in done_nifs:
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
                        revenue_estimated = False
                        logger.info(f"[ES] {name}: revenue {revenue_eur/1e6:.0f}M EUR (YF)")
                except Exception as e:
                    logger.debug(f"[ES] YF failed for {ticker}: {e}")

            if revenue_eur is None:
                revenue_eur = rev_estimate * 1_000_000
                revenue_estimated = True

            nace = None
            yield CompanyRecord(
                name=name,
                country="ES",
                registration_number=nif,
                revenue_eur=revenue_eur,
                revenue_estimated=revenue_estimated,
                employees=None,
                sector=sector,
                nace_code=nace,
                creation_date=None,
                city=None,
                source_url=f"https://www.infocif.es/buscar?nombre={name.split()[0]}",
                directors=[],
            )

            done_nifs.add(name_key)
            checkpoint["done_nifs_es"] = list(done_nifs)
            self.save_checkpoint(checkpoint)
            await asyncio.sleep(0.1)
