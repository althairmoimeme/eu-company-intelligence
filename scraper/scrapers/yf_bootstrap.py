"""Yahoo Finance bootstrap scraper for PL, ES and RO.

Directly populates the DB with listed companies from:
- Warsaw Stock Exchange (WIG) — .WA tickers → PLN→EUR
- Madrid Stock Exchange (BME) — .MC tickers → EUR
- Bucharest Stock Exchange (BVB) — .RO tickers → RON→EUR

Revenue threshold: 75M EUR.
"""
import asyncio
import logging
from typing import AsyncIterator

import yfinance as yf

from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord

logger = logging.getLogger(__name__)

PLN_TO_EUR = 0.233
RON_TO_EUR = 0.201

# ── Warsaw Stock Exchange (WIG) ──────────────────────────────────────────────
WIG_TICKERS = [
    # WIG20
    "PKN.WA", "PZU.WA", "PKO.WA", "PGE.WA", "KGHM.WA", "DNP.WA", "LPP.WA",
    "MBK.WA", "ALE.WA", "CPS.WA", "CDR.WA", "JSW.WA", "KRU.WA", "OPL.WA",
    "PCO.WA", "SPL.WA", "TPE.WA", "VOT.WA", "WPL.WA", "XTB.WA",
    # WIG40
    "ACP.WA", "AGO.WA", "ALR.WA", "AMC.WA", "ATD.WA", "ATT.WA", "BNP.WA",
    "CAR.WA", "CCC.WA", "CMR.WA", "COG.WA", "ENG.WA", "EUR.WA", "GPW.WA",
    "GTC.WA", "ING.WA", "KTY.WA", "LVC.WA", "MCI.WA", "MIL.WA", "MOL.WA",
    "OAT.WA", "PCE.WA", "PKP.WA", "PLW.WA", "SNK.WA", "TEN.WA", "UNI.WA",
    # WIG80 and broader market
    "ABE.WA", "ACG.WA", "AML.WA", "APT.WA", "ARH.WA", "ASB.WA", "ASE.WA",
    "BDX.WA", "BFT.WA", "BIO.WA", "BMC.WA", "BML.WA", "BRS.WA", "BRG.WA",
    "CIE.WA", "CLN.WA", "CMP.WA", "CNT.WA", "CPG.WA", "CRM.WA", "CZT.WA",
    "DCR.WA", "DEG.WA", "DEL.WA", "DKR.WA", "ECL.WA", "ELB.WA", "ENA.WA",
    "ENE.WA", "ENT.WA", "ERB.WA", "ESG.WA", "ETL.WA", "FMG.WA", "FOR.WA",
    "FTE.WA", "GLC.WA", "GME.WA", "GNB.WA", "GRD.WA", "GSG.WA", "HTL.WA",
    "HUB.WA", "IEA.WA", "IGO.WA", "IMF.WA", "INC.WA", "INK.WA", "IPO.WA",
    "IVE.WA", "JWW.WA", "KAN.WA", "KGH.WA", "KLN.WA", "KMX.WA", "KPD.WA",
    "KRI.WA", "LAB.WA", "LKD.WA", "LME.WA", "LSI.WA", "LUG.WA", "MBR.WA",
    "MCB.WA", "MCE.WA", "MDI.WA", "MED.WA", "MEX.WA", "MFO.WA", "MGT.WA",
    "MNI.WA", "MOB.WA", "MPH.WA", "MRC.WA", "MRF.WA", "MSW.WA", "MTV.WA",
    "MWT.WA", "MXC.WA", "NEA.WA", "NEG.WA", "NME.WA", "NNG.WA", "NRE.WA",
    "NTT.WA", "NWG.WA", "OBL.WA", "OCE.WA", "OPF.WA", "OPM.WA", "ORB.WA",
    "PCF.WA", "PEO.WA", "PEP.WA", "PHN.WA", "PLY.WA", "PME.WA", "PMP.WA",
    "PRA.WA", "PRT.WA", "PSW.WA", "PXM.WA", "RAB.WA", "RFK.WA", "RLP.WA",
    "RNK.WA", "RON.WA", "RPC.WA", "RST.WA", "SFD.WA", "SGN.WA", "SHO.WA",
    "SIM.WA", "SKA.WA", "SKH.WA", "SMA.WA", "SME.WA", "SNT.WA", "SOK.WA",
    "SPH.WA", "SQZ.WA", "STX.WA", "SUN.WA", "SVE.WA", "SWG.WA", "TAR.WA",
    "TBB.WA", "TIM.WA", "TOR.WA", "TOW.WA", "TRK.WA", "TXT.WA", "TYP.WA",
    "ULG.WA", "VCR.WA", "VGO.WA", "VRG.WA", "VTL.WA", "WAB.WA", "WDX.WA",
    "WLT.WA", "WRS.WA", "WXF.WA", "ZEP.WA", "ZMT.WA", "ZPC.WA", "ZRE.WA",
]

# ── Madrid Stock Exchange (BME/SIBE) ─────────────────────────────────────────
BME_TICKERS = [
    # IBEX 35
    "TEF.MC", "SAN.MC", "BBVA.MC", "IBE.MC", "REP.MC", "ITX.MC", "AENA.MC",
    "ACS.MC", "ANA.MC", "ACX.MC", "BKT.MC", "CABK.MC", "COL.MC", "ENG.MC",
    "FER.MC", "GRF.MC", "IAG.MC", "IDR.MC", "MAP.MC", "MTS.MC", "NTGY.MC",
    "RED.MC", "SAB.MC", "SOL.MC", "UNI.MC", "VIS.MC", "MEL.MC", "CLNX.MC",
    "ALM.MC", "ROVI.MC", "LOG.MC", "PHM.MC",
    # IBEX Medium Cap
    "ABE.MC", "ACE.MC", "AHV.MC", "ALB.MC", "AND.MC", "ANE.MC",
    "AZK.MC", "BAC.MC", "BQNE.MC", "CAF.MC", "CASH.MC", "CIE.MC",
    "CNT.MC", "DOM.MC", "DIA.MC", "EZE.MC", "FAE.MC", "GAM.MC",
    "GAR.MC", "GLJ.MC", "GEST.MC", "GCO.MC", "HEL.MC", "HTR.MC",
    "ISS.MC", "LAB.MC", "LGT.MC", "LRE.MC", "MCM.MC", "MDL.MC",
    "MDF.MC", "MRL.MC", "MTB.MC", "NAT.MC", "NBI.MC", "NHH.MC",
    "OHL.MC", "ORY.MC", "PRM.MC", "PSG.MC", "PRIM.MC", "QBT.MC",
    "REC.MC", "REN.MC", "RGST.MC", "RIO.MC", "RLIA.MC", "RVT.MC",
    "SAR.MC", "SCYR.MC", "SEM.MC", "SLR.MC", "SPE.MC", "SPS.MC",
    "TEC.MC", "TL5.MC", "TRG.MC", "TUD.MC", "UBS.MC", "UPL.MC",
    "VBC.MC", "VCT.MC", "VID.MC", "VOC.MC", "ZOT.MC",
]

# ── Bucharest Stock Exchange (BVB) ───────────────────────────────────────────
BVB_TICKERS = [
    # BET Index (top 20 Romanian listed companies)
    "SNP.RO", "TLV.RO", "H2O.RO", "SNN.RO", "BRD.RO", "TEL.RO",
    "TGN.RO", "SNT.RO", "EL.RO", "DIGI.RO", "ONE.RO", "FP.RO",
    "TRANSGAZ.RO", "NUCLEARELECTRICA.RO", "ROMGAZ.RO",
    # BET-BK & other large caps
    "AQ.RO", "ATB.RO", "BIO.RO", "BRK.RO", "CBC.RO", "CEON.RO",
    "CMF.RO", "COTE.RO", "ECR.RO", "EUR.RO", "MED.RO", "OIL.RO",
    "ROCE.RO", "SOCP.RO", "STK.RO", "TBM.RO", "WINE.RO",
]

EXCHANGE_CONFIG = {
    "PL": {
        "tickers": WIG_TICKERS,
        "currency_to_eur": PLN_TO_EUR,
        "country": "PL",
        "exchange": "Warsaw (WIG)",
    },
    "ES": {
        "tickers": BME_TICKERS,
        "currency_to_eur": 1.0,  # already EUR
        "country": "ES",
        "exchange": "Madrid (BME)",
    },
    "RO": {
        "tickers": BVB_TICKERS,
        "currency_to_eur": RON_TO_EUR,
        "country": "RO",
        "exchange": "Bucharest (BVB)",
    },
}

_SECTOR_MAP = {
    "Basic Materials": "Industries extractives / matériaux",
    "Communication Services": "Télécommunications / médias",
    "Consumer Cyclical": "Commerce / distribution / loisirs",
    "Consumer Defensive": "Agroalimentaire / grande conso",
    "Energy": "Énergie",
    "Financial Services": "Finance / assurance",
    "Healthcare": "Santé / pharma",
    "Industrials": "Industrie / construction",
    "Real Estate": "Immobilier",
    "Technology": "Technologies / IT",
    "Utilities": "Services aux collectivités",
}


def _yf_info_to_record(ticker: str, info: dict, country: str,
                        currency_to_eur: float) -> CompanyRecord | None:
    """Convert yfinance info dict to CompanyRecord. Returns None if below threshold."""
    name = info.get("longName") or info.get("shortName") or ""
    if not name:
        return None

    revenue_raw = info.get("totalRevenue")
    if not revenue_raw or float(revenue_raw) <= 0:
        return None

    revenue_eur = float(revenue_raw) * currency_to_eur
    if revenue_eur < 75_000_000:
        return None

    sector_en = info.get("sector", "")
    sector = _SECTOR_MAP.get(sector_en, sector_en) if sector_en else None

    employees = info.get("fullTimeEmployees")
    city = info.get("city")
    website = info.get("website")

    return CompanyRecord(
        name=name,
        country=country,
        registration_number=ticker,  # use ticker as registration number
        revenue_eur=revenue_eur,
        revenue_year=2024,
        revenue_estimated=False,
        employees=int(employees) if employees else None,
        sector=sector,
        nace_code=None,
        activity_description=info.get("longBusinessSummary", "")[:500] if info.get("longBusinessSummary") else None,
        city=city,
        website=website,
        source_url=f"https://finance.yahoo.com/quote/{ticker}",
        directors=[],
    )


def _fetch_yf_info(ticker: str) -> dict | None:
    """Synchronous yfinance info fetch."""
    try:
        t = yf.Ticker(ticker)
        info = t.info
        if info and info.get("quoteType") not in (None, "NONE"):
            return info
        return None
    except Exception:
        return None


class YFBootstrapScraper(BaseScraper):
    """Bootstrap scraper: fetches listed companies from Yahoo Finance.

    Covers Poland (WIG), Spain (BME), Romania (BVB).
    Bypasses broken national registry APIs.
    """
    name = "yf_bootstrap"
    country = "MULTI"

    # Which exchanges to include — configurable via config['YF_COUNTRIES']
    DEFAULT_COUNTRIES = ["PL", "ES", "RO"]

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        loop = asyncio.get_event_loop()
        checkpoint = self.load_checkpoint() if resume else {}
        done_tickers = set(checkpoint.get("done_tickers", []))

        target_countries = self.config.get("YF_COUNTRIES", self.DEFAULT_COUNTRIES)

        for country_code in target_countries:
            cfg = EXCHANGE_CONFIG.get(country_code)
            if not cfg:
                continue

            tickers = cfg["tickers"]
            currency_to_eur = cfg["currency_to_eur"]
            country = cfg["country"]
            exchange = cfg["exchange"]

            logger.info(f"[YF-BOOT] {country} — {exchange}: {len(tickers)} tickers")
            found = 0

            for i, ticker in enumerate(tickers):
                if ticker in done_tickers:
                    continue

                try:
                    info = await loop.run_in_executor(None, _fetch_yf_info, ticker)
                    if info:
                        record = _yf_info_to_record(ticker, info, country, currency_to_eur)
                        if record:
                            yield record
                            found += 1
                            logger.info(
                                f"[YF-BOOT] ✓ {ticker}: {record.name} "
                                f"— CA {record.revenue_eur/1e6:.0f}M EUR"
                            )
                except Exception as e:
                    logger.debug(f"[YF-BOOT] {ticker} error: {e}")

                done_tickers.add(ticker)
                checkpoint["done_tickers"] = list(done_tickers)
                if i % 20 == 0:
                    self.save_checkpoint(checkpoint)

                await asyncio.sleep(0.2)

            self.save_checkpoint(checkpoint)
            logger.info(f"[YF-BOOT] {country} done — {found} companies added")
