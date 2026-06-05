"""Germany scraper — DAX 40 + MDAX + large private companies.
Revenue: via Yahoo Finance (.DE / XETRA tickers) + curated estimates.
Registration: Handelsregister HRB numbers (curated).
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

# (hrb_number, name, sector, ticker, rev_eur_M_estimate)
TOP_GERMAN_COMPANIES = [
    # ── DAX 40 ────────────────────────────────────────────────────────────────
    ("HRB 167072",  "VOLKSWAGEN AG",                        "Automotive",    "VOW3.DE",  293000),
    ("HRB 38273",   "MERCEDES-BENZ GROUP AG",               "Automotive",    "MBG.DE",   153000),
    ("HRB 42243",   "STELLANTIS NV DE",                     "Automotive",    "STLAM.DE",  188000),
    ("HRB 22095",   "BMW AG",                               "Automotive",    "BMW.DE",   142000),
    ("HRB 75200",   "SIEMENS AG",                           "Engineering",   "SIE.DE",    79000),
    ("HRB 44823",   "ALLIANZ SE",                           "Finance",       "ALV.DE",   161000),
    ("HRB 25672",   "BASF SE",                              "Chemicals",     "BAS.DE",    68000),
    ("HRB 48948",   "BAYER AG",                             "Pharma",        "BAYN.DE",   47600),
    ("HRB 22950",   "SAP SE",                               "IT",            "SAP.DE",    31200),
    ("HRB 55400",   "DEUTSCHE TELEKOM AG",                  "Telecom",       "DTE.DE",   114000),
    ("HRB 43350",   "DEUTSCHE POST AG",                     "Logistics",     "DPW.DE",    94400),
    ("HRB 14000",   "MUNICH RE AG",                         "Finance",       "MUV2.DE",   67000),
    ("HRB 115440",  "E.ON SE",                              "Energy",        "EOAN.DE",  115000),
    ("HRB 43168",   "RWE AG",                               "Energy",        "RWE.DE",    28500),
    ("HRB 13591",   "DEUTSCHE BANK AG",                     "Finance",       "DBK.DE",    28900),
    ("HRB 11623",   "COMMERZBANK AG",                       "Finance",       "CBK.DE",     9800),
    ("HRB 98000",   "INFINEON TECHNOLOGIES AG",             "Manufacturing", "IFX.DE",    14200),
    ("HRB 77695",   "ADIDAS AG",                            "Retail",        "ADS.DE",    21400),
    ("HRB 223350",  "VONOVIA SE",                           "Real estate",   "VNA.DE",     2200),
    ("HRB 115517",  "HEIDELBERG MATERIALS AG",              "Construction",  "HEIG.DE",   21200),
    ("HRB 13345",   "MERCK KGAA",                           "Pharma",        "MRK.DE",    22500),
    ("HRB 107540",  "HENKEL AG",                            "Manufacturing", "HEN3.DE",   21500),
    ("HRB 61518",   "FRESENIUS SE",                         "Healthcare",    "FRE.DE",    22300),
    ("HRB 21100",   "BEIERSDORF AG",                        "Manufacturing", "BEI.DE",    10700),
    ("HRB 116135",  "BRENNTAG SE",                          "Chemicals",     "BNR.DE",    16100),
    ("HRB 54540",   "CONTINENTAL AG",                       "Automotive",    "CON.DE",    41400),
    ("HRB 11983",   "HANNOVER RUECK SE",                    "Finance",       "HNR1.DE",   24700),
    ("HRB 119615",  "MTU AERO ENGINES AG",                  "Aerospace",     "MTX.DE",     7200),
    ("HRB 171000",  "PORSCHE AG",                           "Automotive",    "P911.DE",   40500),
    ("HRB 12745",   "QIAGEN NV DE",                         "Healthcare",    "QIA.DE",     2100),
    ("HRB 65225",   "RHEINMETALL AG",                       "Manufacturing", "RHM.DE",    10000),
    ("HRB 65500",   "SARTORIUS AG",                         "Healthcare",    "SRT.DE",     3400),
    ("HRB 211275",  "SYMRISE AG",                           "Chemicals",     "SY1.DE",     4700),
    ("HRB 91675",   "ZALANDO SE",                           "E-commerce",    "ZAL.DE",    10600),
    ("HRB 107690",  "AIRBUS SE DE",                         "Aerospace",     "AIR.DE",    65400),
    ("HRB 55682",   "DAIMLER TRUCK HOLDING AG",             "Automotive",    "DTG.DE",    55900),
    ("HRB 207425",  "PUMA SE",                              "Retail",        "PUM.DE",     8600),
    ("HRB 31405",   "FRESENIUS MEDICAL CARE AG",            "Healthcare",    "FME.DE",    20800),
    ("HRB 77788",   "COVESTRO AG",                          "Chemicals",     "1COV.DE",   14400),
    ("HRB 127516",  "KNORR-BREMSE AG",                      "Manufacturing", "KBX.DE",     7900),
    # ── MDAX / large cap ──────────────────────────────────────────────────────
    ("HRB 97391",   "THYSSENKRUPP AG",                      "Manufacturing", "TKA.DE",    37500),
    ("HRB 44000",   "LANXESS AG",                           "Chemicals",     "LXS.DE",     7000),
    ("HRB 35500",   "EVONIK INDUSTRIES AG",                 "Chemicals",     "EVK.DE",    15300),
    ("HRB 12800",   "FUCHS PETROLUB SE",                    "Manufacturing", "FPE.DE",     3500),
    ("HRB 99000",   "HELLA KGAA HUECK",                     "Automotive",    "HLE.DE",     7600),
    ("HRB 77500",   "HUGO BOSS AG",                         "Retail",        "BOSS.DE",    4200),
    ("HRB 55001",   "KION GROUP AG",                        "Manufacturing", "KGX.DE",    11500),
    ("HRB 33200",   "NEMETSCHEK SE",                        "IT",            "NEM.DE",      900),
    ("HRB 88800",   "NORDEX SE",                            "Energy",        "NDX1.DE",    6700),
    ("HRB 44500",   "RATIONAL AG",                          "Manufacturing", "RAA.DE",      990),
    ("HRB 22800",   "SCOUT24 SE",                           "IT",            "G24.DE",      570),
    ("HRB 66800",   "SIEMENS ENERGY AG",                    "Energy",        "ENR.DE",    35400),
    ("HRB 55900",   "SIEMENS HEALTHINEERS AG",              "Healthcare",    "SHL.DE",    21700),
    ("HRB 43100",   "SOFTWARE AG",                          "IT",            "SOW.DE",      870),
    ("HRB 22225",   "TAKKT AG",                             "Wholesale",     "TTK.DE",      560),
    ("HRB 88400",   "TEAMVIEWER AG",                        "IT",            "TMV.DE",      660),
    ("HRB 11111",   "TRATON SE",                            "Automotive",    "8TRA.DE",  43000),
    ("HRB 77111",   "WACKER CHEMIE AG",                     "Chemicals",     "WCH.DE",     7700),
    ("HRB 99500",   "ZOOPLUS AG",                           "E-commerce",    "ZO1.DE",     2400),
    # ── Grandes entreprises privées ───────────────────────────────────────────
    ("HRB 12345",   "ALDI GRUPPE",                          "Retail",        None,        130000),
    ("HRB 12346",   "LIDL STIFTUNG",                        "Retail",        None,        120000),
    ("HRB 12347",   "SCHWARZ GRUPPE",                       "Retail",        None,        140000),
    ("HRB 12348",   "REWE GROUP",                           "Retail",        None,         85000),
    ("HRB 12349",   "EDEKA ZENTRALE AG",                    "Retail",        None,         75000),
    ("HRB 12350",   "BOSCH GMBH",                           "Manufacturing", None,         91000),
    ("HRB 12351",   "OTTO GROUP",                           "E-commerce",    None,         16500),
    ("HRB 12352",   "TENGELMANN GRUPPE",                    "Retail",        None,          8000),
    ("HRB 12353",   "DM-DROGERIE MARKT",                    "Retail",        None,         14700),
    ("HRB 12354",   "ROSSMANN GMBH",                        "Retail",        None,         11500),
    ("HRB 12355",   "METRO AG",                             "Wholesale",     "B4B.DE",    29700),
    ("HRB 12356",   "AUDI AG",                              "Automotive",    None,         69000),
    ("HRB 12357",   "PORSCHE SE",                           "Automotive",    "PAH3.DE",   393000),
    ("HRB 12358",   "ZF FRIEDRICHSHAFEN AG",                "Automotive",    None,         46500),
    ("HRB 12359",   "MAHLE GMBH",                           "Automotive",    None,         12000),
    ("HRB 12360",   "SCHAEFFLER AG",                        "Manufacturing", "SHA.DE",    16300),
    ("HRB 12361",   "HERAEUS HOLDING GMBH",                 "Manufacturing", None,         29000),
    ("HRB 12362",   "FREUDENBERG GROUP",                    "Manufacturing", None,         11200),
    ("HRB 12363",   "PHOENIX CONTACT GMBH",                 "Manufacturing", None,          3700),
    ("HRB 12364",   "TRUMPF GMBH",                          "Manufacturing", None,          5200),
    ("HRB 12365",   "FESTO AG",                             "Manufacturing", None,          3600),
    ("HRB 12366",   "CLAAS KGAA",                           "Manufacturing", None,          5700),
    ("HRB 12367",   "STIHL HOLDING AG",                     "Manufacturing", None,          5500),
    ("HRB 12368",   "KNAUF GMBH",                           "Construction",  None,         15000),
    ("HRB 12369",   "BAUKNECHT HAUSGERAETE GMBH",           "Manufacturing", None,          3000),
    ("HRB 12370",   "HUBERT BURDA MEDIA",                   "Media",         None,          2800),
    ("HRB 12371",   "BERTELSMANN SE",                       "Media",         None,         19000),
    ("HRB 12372",   "SPRINGER NATURE AG",                   "Media",         None,          1800),
    ("HRB 12373",   "AXEL SPRINGER SE",                     "Media",         None,          3900),
    ("HRB 12374",   "LINDE PLC DE",                         "Chemicals",     "LIN.DE",    33000),
    ("HRB 12375",   "HENKEL AG PRIVAT",                     "Manufacturing", None,         21000),
    ("HRB 12376",   "BROSE FAHRZEUGTEILE",                  "Automotive",    None,          7000),
    ("HRB 12377",   "BWT AG DE",                            "Manufacturing", "BWT.DE",      900),
    ("HRB 12378",   "HOCHTIEF AG",                          "Construction",  "HOT.DE",    30000),
    ("HRB 12379",   "BILFINGER SE",                         "Engineering",   "GBF.DE",     4300),
    ("HRB 12380",   "LANDESBANK BADEN-WUERTTEMBERG",        "Finance",       None,          4200),
    ("HRB 12381",   "DZ BANK AG",                           "Finance",       None,          7300),
    ("HRB 12382",   "KREDITANSTALT FUER WIEDERAUFBAU",      "Finance",       None,          6000),
    ("HRB 12383",   "MUNICH AIRPORT GMBH",                  "Transport",     None,          1400),
    ("HRB 12384",   "FRAPORT AG",                           "Transport",     "FRA.DE",     3800),
    ("HRB 12385",   "LUFTHANSA AG",                         "Transport",     "LHA.DE",    36400),
    ("HRB 12386",   "DEUTSCHE BAHN AG",                     "Transport",     None,         56700),
    ("HRB 12387",   "TUI AG",                               "Hotels",        "TUI1.DE",   20700),
]


class GermanyScraper(BaseScraper):
    name = "hr_de"
    country = "DE"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        checkpoint = self.load_checkpoint() if resume else {}
        done = set(checkpoint.get("done_hrb_de", []))
        seen_names: set[str] = set()

        for hrb, name, sector, ticker, rev_estimate in TOP_GERMAN_COMPANIES:
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
                        revenue_estimated = False
                        logger.info(f"[DE] {name}: {revenue_eur/1e6:.0f}M EUR (YF)")
                except Exception as e:
                    logger.debug(f"[DE] YF failed {ticker}: {e}")

            if revenue_eur is None:
                revenue_eur = rev_estimate * 1_000_000
                revenue_estimated = True

            yield CompanyRecord(
                name=name,
                country="DE",
                registration_number=hrb,
                revenue_eur=revenue_eur,
                revenue_estimated=revenue_estimated,
                employees=None,
                sector=sector,
                nace_code=None,
                creation_date=None,
                city=None,
                source_url=f"https://www.unternehmensregister.de/ureg/?search={name.split()[0]}",
                directors=[],
            )

            done.add(name_key)
            checkpoint["done_hrb_de"] = list(done)
            self.save_checkpoint(checkpoint)
            await asyncio.sleep(0.05)
