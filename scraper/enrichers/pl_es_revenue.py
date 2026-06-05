"""Revenue enricher for Poland and Spain via Yahoo Finance.

Strategy:
1. Pre-load all listed companies from Warsaw (.WA) and Madrid (.MC) exchanges
2. For each company in DB, search Yahoo Finance by name → match ticker
3. Fetch annual revenue (totalRevenue) via yfinance
4. Convert PLN→EUR for Poland, keep EUR for Spain
5. Update DB with real revenue data
"""
import asyncio
import logging
import re
import httpx
import yfinance as yf
from sqlalchemy import select, update
from ..db.session import get_session_factory
from ..db.models import Company

logger = logging.getLogger(__name__)

YF_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"

# PLN to EUR (April 2026 approximate)
PLN_TO_EUR = 0.233

# ── Comprehensive WIG (Warsaw) tickers ──────────────────────────────────────
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
    # WIG80 & others
    "ABE.WA", "ACG.WA", "AML.WA", "APT.WA", "ARH.WA", "ASB.WA", "ASE.WA",
    "BDX.WA", "BFT.WA", "BIO.WA", "BMC.WA", "BML.WA", "BRS.WA", "BRG.WA",
    "BWB.WA", "CBA.WA", "CHL.WA", "CIE.WA", "CLN.WA", "CMP.WA", "CNT.WA",
    "CPG.WA", "CRM.WA", "CZT.WA", "DCR.WA", "DEG.WA", "DEL.WA", "DKR.WA",
    "DXN.WA", "ECL.WA", "ELB.WA", "ENA.WA", "ENE.WA", "ENT.WA", "ERB.WA",
    "ESG.WA", "ETL.WA", "FMG.WA", "FOR.WA", "FTE.WA", "GLC.WA", "GME.WA",
    "GNB.WA", "GRD.WA", "GSG.WA", "HTL.WA", "HUB.WA", "IEA.WA", "IGO.WA",
    "IMF.WA", "INC.WA", "INK.WA", "IPO.WA", "IVE.WA", "JWW.WA", "KAN.WA",
    "KGH.WA", "KLN.WA", "KMX.WA", "KPD.WA", "KRI.WA", "LAB.WA", "LKD.WA",
    "LME.WA", "LPP.WA", "LSI.WA", "LUG.WA", "MBR.WA", "MCB.WA", "MCE.WA",
    "MDI.WA", "MED.WA", "MEX.WA", "MFO.WA", "MGT.WA", "MNI.WA", "MOB.WA",
    "MPH.WA", "MRC.WA", "MRF.WA", "MSW.WA", "MTV.WA", "MWT.WA", "MXC.WA",
    "NEA.WA", "NEG.WA", "NME.WA", "NNG.WA", "NRE.WA", "NTT.WA", "NWG.WA",
    "OBL.WA", "OCE.WA", "OPF.WA", "OPM.WA", "ORB.WA", "PCF.WA", "PEO.WA",
    "PEP.WA", "PHN.WA", "PLY.WA", "PME.WA", "PMP.WA", "PRA.WA", "PRT.WA",
    "PSW.WA", "PXM.WA", "RAB.WA", "RFK.WA", "RLP.WA", "RNK.WA", "RON.WA",
    "RPC.WA", "RST.WA", "SFD.WA", "SGN.WA", "SHO.WA", "SIM.WA", "SKA.WA",
    "SKH.WA", "SMA.WA", "SME.WA", "SNT.WA", "SOK.WA", "SPH.WA", "SQZ.WA",
    "STX.WA", "SUN.WA", "SVE.WA", "SWG.WA", "TAR.WA", "TBB.WA", "TIM.WA",
    "TOR.WA", "TOW.WA", "TRK.WA", "TXT.WA", "TYP.WA", "ULG.WA", "VCR.WA",
    "VGO.WA", "VRG.WA", "VTL.WA", "WAB.WA", "WDX.WA", "WLT.WA", "WRS.WA",
    "WXF.WA", "ZEP.WA", "ZMT.WA", "ZPC.WA", "ZRE.WA",
]

# ── Comprehensive IBEX/BME (Madrid) tickers ──────────────────────────────────
BME_TICKERS = [
    # IBEX 35
    "TEF.MC", "SAN.MC", "BBVA.MC", "IBE.MC", "REP.MC", "ITX.MC", "AENA.MC",
    "ACS.MC", "ANA.MC", "ACX.MC", "BKT.MC", "CABK.MC", "COL.MC", "ENG.MC",
    "FER.MC", "GRF.MC", "IAG.MC", "IDR.MC", "MAP.MC", "MTS.MC", "NTGY.MC",
    "RED.MC", "SAB.MC", "SGRE.MC", "SOL.MC", "TRE.MC", "UNI.MC", "VIS.MC",
    "MEL.MC", "LOG.MC", "PHM.MC", "CLNX.MC", "ALM.MC", "FDR.MC", "ROVI.MC",
    # Mid Cap & others
    "ABE.MC", "ABG.MC", "ACE.MC", "ADZ.MC", "AFI.MC", "AGS.MC", "AHV.MC",
    "AIR.MC", "AIT.MC", "AJA.MC", "AKR.MC", "ALB.MC", "ALC.MC", "ALE.MC",
    "ALF.MC", "ALG.MC", "ALH.MC", "ALM.MC", "ALO.MC", "ALS.MC", "ALT.MC",
    "ALU.MC", "ALV.MC", "AME.MC", "AMP.MC", "AMS.MC", "AMT.MC", "AMU.MC",
    "AND.MC", "ANE.MC", "ANF.MC", "ANS.MC", "APD.MC", "APM.MC", "APP.MC",
    "ARC.MC", "ARG.MC", "ARI.MC", "ARM.MC", "ARN.MC", "ARO.MC", "ARP.MC",
    "ARS.MC", "ART.MC", "ARX.MC", "ASG.MC", "ASL.MC", "ASN.MC", "ASO.MC",
    "ASP.MC", "ASR.MC", "AST.MC", "ATI.MC", "ATL.MC", "ATO.MC", "ATP.MC",
    "ATS.MC", "ATX.MC", "AUC.MC", "AUN.MC", "AUR.MC", "AVI.MC", "AVM.MC",
    "AXA.MC", "AXI.MC", "AYG.MC", "AZK.MC", "BAC.MC", "BAF.MC", "BAP.MC",
    "BAR.MC", "BAY.MC", "BCO.MC", "BDL.MC", "BDM.MC", "BEC.MC", "BEN.MC",
    "BES.MC", "BIO.MC", "BKY.MC", "BMA.MC", "BME.MC", "BNC.MC", "BNT.MC",
    "BOE.MC", "BOR.MC", "BRM.MC", "BRO.MC", "BRS.MC", "BSB.MC", "BTE.MC",
    "BTG.MC", "CAF.MC", "CAM.MC", "CAP.MC", "CAR.MC", "CAS.MC", "CAT.MC",
    "CBE.MC", "CCC.MC", "CDI.MC", "CDU.MC", "CEA.MC", "CEC.MC", "CEG.MC",
    "CEM.MC", "CEP.MC", "CFG.MC", "CIN.MC", "CIR.MC", "CIT.MC", "CLH.MC",
    "CLM.MC", "CLS.MC", "CMC.MC", "CMO.MC", "CMP.MC", "CNB.MC", "CNC.MC",
    "COB.MC", "COE.MC", "COM.MC", "COR.MC", "CRI.MC", "CRV.MC", "CSB.MC",
    "CTC.MC", "CTG.MC", "CTR.MC", "CUN.MC", "CVG.MC", "CVI.MC", "DAM.MC",
    "DCO.MC", "DEA.MC", "DEN.MC", "DIA.MC", "DON.MC", "DOR.MC", "DTE.MC",
    "DWS.MC", "EBO.MC", "EDI.MC", "EDP.MC", "EGL.MC", "ELE.MC", "ELG.MC",
    "EME.MC", "EMO.MC", "ENC.MC", "ENE.MC", "ENO.MC", "ENT.MC", "EZE.MC",
    "FAE.MC", "FAG.MC", "FAU.MC", "FCC.MC", "FCE.MC", "FCI.MC", "FDR.MC",
    "FGR.MC", "FIE.MC", "FIN.MC", "FIV.MC", "FLO.MC", "FLS.MC", "FLU.MC",
    "FNM.MC", "FOC.MC", "FOM.MC", "FOR.MC", "FRS.MC", "FRX.MC", "FTE.MC",
    "GAL.MC", "GAS.MC", "GAT.MC", "GEK.MC", "GEN.MC", "GES.MC", "GHY.MC",
    "GLB.MC", "GLG.MC", "GNK.MC", "GNT.MC", "GOB.MC", "GON.MC", "GPE.MC",
    "GRI.MC", "GRO.MC", "GRS.MC", "GRV.MC", "GSJ.MC", "GWT.MC", "GXM.MC",
    "HAR.MC", "HDF.MC", "HER.MC", "HIL.MC", "HLX.MC", "HMI.MC", "HOM.MC",
    "HUV.MC", "HYD.MC", "IBG.MC", "IDL.MC", "IEF.MC", "IFX.MC", "IGG.MC",
    "ILU.MC", "IMA.MC", "IMO.MC", "INB.MC", "IND.MC", "INE.MC", "ING.MC",
    "INI.MC", "INM.MC", "INO.MC", "INR.MC", "INT.MC", "INV.MC", "IPH.MC",
    "IQE.MC", "IRE.MC", "IRM.MC", "ISA.MC", "ISS.MC", "ITA.MC", "ITV.MC",
    "IZB.MC", "JDP.MC", "JEN.MC", "JOL.MC", "JPM.MC", "JZS.MC", "LAB.MC",
    "LAF.MC", "LAR.MC", "LDE.MC", "LGT.MC", "LID.MC", "LIN.MC", "LMR.MC",
    "LOU.MC", "LRE.MC", "LYC.MC", "MAB.MC", "MAC.MC", "MAG.MC", "MAO.MC",
    "MAR.MC", "MAS.MC", "MAT.MC", "MCM.MC", "MDF.MC", "MDL.MC", "MDN.MC",
    "MDO.MC", "MED.MC", "MEG.MC", "MHI.MC", "MIL.MC", "MIN.MC", "MIR.MC",
    "MLS.MC", "MMB.MC", "MMT.MC", "MNC.MC", "MND.MC", "MNT.MC", "MOB.MC",
    "MOL.MC", "MOR.MC", "MPR.MC", "MQS.MC", "MRC.MC", "MRL.MC", "MRN.MC",
    "MRO.MC", "MRS.MC", "MRT.MC", "MSB.MC", "MTB.MC", "MTC.MC", "MTG.MC",
    "MTZ.MC", "MVC.MC", "NAT.MC", "NBC.MC", "NBI.MC", "NCG.MC", "NEA.MC",
    "NEX.MC", "NHH.MC", "NMA.MC", "NMT.MC", "NOA.MC", "NOR.MC", "NOS.MC",
    "NTG.MC", "NTH.MC", "NTS.MC", "NXT.MC", "OHL.MC", "OLE.MC", "OLI.MC",
    "OLS.MC", "OMG.MC", "ONA.MC", "OPD.MC", "OPI.MC", "OPS.MC", "OPT.MC",
    "ORB.MC", "ORI.MC", "ORM.MC", "ORN.MC", "OXY.MC", "PAC.MC", "PAG.MC",
    "PAR.MC", "PEC.MC", "PEN.MC", "PER.MC", "PES.MC", "PEU.MC", "PGS.MC",
    "PHA.MC", "PIM.MC", "PIN.MC", "PLA.MC", "PLR.MC", "PLS.MC", "PLT.MC",
    "POL.MC", "POM.MC", "POP.MC", "POR.MC", "PPS.MC", "PPT.MC", "PRC.MC",
    "PRG.MC", "PRI.MC", "PRM.MC", "PRO.MC", "PRS.MC", "PSG.MC", "PSN.MC",
    "PTO.MC", "PTS.MC", "PUR.MC", "QBT.MC", "QFA.MC", "QUE.MC", "RAC.MC",
    "RAG.MC", "RAM.MC", "RDW.MC", "REC.MC", "REG.MC", "REN.MC", "RES.MC",
    "REX.MC", "RGS.MC", "RIA.MC", "RIB.MC", "RIO.MC", "ROC.MC", "ROM.MC",
    "ROV.MC", "RPC.MC", "RPI.MC", "RPL.MC", "RPP.MC", "RPS.MC", "RPT.MC",
    "RRA.MC", "RRI.MC", "RSL.MC", "RTB.MC", "RTE.MC", "RTG.MC", "RUM.MC",
    "SAL.MC", "SAR.MC", "SCI.MC", "SCY.MC", "SED.MC", "SEG.MC", "SEP.MC",
    "SEQ.MC", "SIG.MC", "SIL.MC", "SIM.MC", "SKY.MC", "SLR.MC", "SNA.MC",
    "SNK.MC", "SNT.MC", "SRE.MC", "SRP.MC", "SSY.MC", "STE.MC", "STG.MC",
    "STO.MC", "STR.MC", "SUP.MC", "SUS.MC", "SWS.MC", "SYN.MC", "TAG.MC",
    "TAL.MC", "TAO.MC", "TCM.MC", "TDG.MC", "TEC.MC", "TED.MC", "TEN.MC",
    "TEP.MC", "TES.MC", "TIT.MC", "TKA.MC", "TLC.MC", "TOR.MC", "TPC.MC",
    "TRG.MC", "TRK.MC", "TRM.MC", "TRS.MC", "TST.MC", "TUI.MC", "TUN.MC",
    "TWS.MC", "UCM.MC", "UFP.MC", "UME.MC", "UNE.MC", "URB.MC", "URO.MC",
    "VAL.MC", "VCP.MC", "VGP.MC", "VID.MC", "VIG.MC", "VIS.MC", "VLO.MC",
    "VMT.MC", "VNT.MC", "VOD.MC", "VRS.MC", "VRT.MC", "VST.MC", "WAM.MC",
    "WDP.MC", "WPP.MC", "WRB.MC", "XPB.MC", "YNN.MC", "ZAR.MC", "ZEL.MC",
]


async def enrich_pl_es_revenues(db_path: str, country: str = "ALL"):
    """Enrich Polish and Spanish companies with Yahoo Finance revenue data."""
    factory = get_session_factory(db_path)

    countries = []
    if country in ("ALL", "PL"):
        countries.append("PL")
    if country in ("ALL", "ES"):
        countries.append("ES")

    for ctry in countries:
        await _enrich_country(factory, ctry)


async def _enrich_country(factory, country: str):
    """Enrich one country."""
    suffix = "WA" if country == "PL" else "MC"
    currency_rate = PLN_TO_EUR if country == "PL" else 1.0
    currency_name = "PLN" if country == "PL" else "EUR"
    tickers = WIG_TICKERS if country == "PL" else BME_TICKERS

    logger.info(f"[{country}-ENRICH] Loading all {suffix} listed companies...")

    # Step 1: Build ticker → (name, revenue) lookup from Yahoo Finance
    ticker_data = {}
    loop = asyncio.get_event_loop()

    for i, ticker in enumerate(tickers):
        try:
            name, revenue = await loop.run_in_executor(
                None, _fetch_yf_data, ticker
            )
            if name and revenue and revenue > 0:
                ticker_data[ticker] = {
                    "name": name,
                    "revenue_eur": revenue * currency_rate,
                    "symbol": ticker,
                }
        except Exception as e:
            logger.debug(f"[{country}-ENRICH] Skip {ticker}: {e}")

        if i % 50 == 0:
            logger.info(f"[{country}-ENRICH] Loaded {i}/{len(tickers)} tickers, "
                        f"{len(ticker_data)} with revenue")
        await asyncio.sleep(0.05)

    logger.info(f"[{country}-ENRICH] {len(ticker_data)} listed companies with revenue data")

    # Step 2: Get all companies of this country from DB
    async with factory() as session:
        result = await session.execute(
            select(Company).where(Company.country == country)
        )
        db_companies = result.scalars().all()

    logger.info(f"[{country}-ENRICH] {len(db_companies)} companies in DB to match")

    # Step 3: Match DB companies to ticker data
    enriched = 0
    for company in db_companies:
        best_match = _find_best_match(company.name, ticker_data)
        if best_match and best_match["revenue_eur"] >= 75_000_000:
            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        update(Company)
                        .where(Company.id == company.id)
                        .values(
                            revenue_eur=best_match["revenue_eur"],
                            revenue_year=2024,
                            revenue_estimated=False,
                        )
                    )
            enriched += 1
            logger.info(
                f"[{country}-ENRICH] ✓ {company.name} → {best_match['symbol']}: "
                f"{best_match['revenue_eur']/1e6:.0f}M EUR"
            )

    # Step 4: For unmatched companies, search Yahoo Finance by name
    unmatched = [c for c in db_companies if c.revenue_eur is None]
    logger.info(f"[{country}-ENRICH] Searching Yahoo Finance for {len(unmatched)} unmatched companies...")

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15, follow_redirects=True
    ) as client:
        for i, company in enumerate(unmatched[:500]):  # Limit to 500
            revenue = await _search_yf_by_name(client, company.name, f".{suffix}")
            if revenue and revenue * currency_rate >= 75_000_000:
                async with factory() as session:
                    async with session.begin():
                        await session.execute(
                            update(Company)
                            .where(Company.id == company.id)
                            .values(
                                revenue_eur=revenue * currency_rate,
                                revenue_year=2024,
                                revenue_estimated=False,
                            )
                        )
                enriched += 1
                logger.info(
                    f"[{country}-ENRICH] ✓ {company.name}: "
                    f"{revenue * currency_rate / 1e6:.0f}M EUR (via search)"
                )
            if i % 100 == 0:
                logger.info(f"[{country}-ENRICH] Search progress: {i}/{len(unmatched[:500])}")
            await asyncio.sleep(0.3)

    logger.info(f"[{country}-ENRICH] Done — {enriched}/{len(db_companies)} companies enriched")
    return enriched


def _fetch_yf_data(ticker: str) -> tuple:
    """Fetch name and revenue from Yahoo Finance (synchronous)."""
    t = yf.Ticker(ticker)
    info = t.info
    name = info.get("longName") or info.get("shortName") or ""
    revenue = info.get("totalRevenue")
    return name, float(revenue) if revenue else None


async def _search_yf_by_name(client: httpx.AsyncClient, company_name: str,
                               suffix: str) -> float | None:
    """Search Yahoo Finance by name and get revenue if listed."""
    try:
        resp = await client.get(
            YF_SEARCH_URL,
            params={"q": company_name, "lang": "en", "type": "equity", "newsCount": 0},
        )
        if resp.status_code != 200:
            return None

        quotes = resp.json().get("quotes", [])
        for q in quotes[:5]:
            sym = q.get("symbol", "")
            if not sym.endswith(suffix):
                continue
            name_match = _name_similarity(
                company_name,
                q.get("longname") or q.get("shortname") or ""
            )
            if name_match < 0.55:
                continue

            # Get revenue
            loop = asyncio.get_event_loop()
            _, revenue = await loop.run_in_executor(None, _fetch_yf_data, sym)
            return revenue

    except Exception:
        pass
    return None


def _find_best_match(company_name: str, ticker_data: dict) -> dict | None:
    """Find best matching ticker for a company name."""
    best_score = 0.6  # Minimum threshold
    best_match = None

    for ticker, data in ticker_data.items():
        score = _name_similarity(company_name, data["name"])
        if score > best_score:
            best_score = score
            best_match = data

    return best_match


def _name_similarity(name1: str, name2: str) -> float:
    """Simple Jaccard similarity on words."""
    if not name1 or not name2:
        return 0.0

    def clean(n):
        n = n.upper()
        # Remove common suffixes
        for suffix in ["S.A.", "SA", "S.A", "SP. Z O.O.", "SP.Z O.O.", "SP.ZO.O.",
                       "PLC", "LTD", "LIMITED", "GROUP", "HOLDINGS", "INC",
                       "SOCIEDAD ANONIMA", "S.L.", "SL", "SLU", "SL.",
                       "SPOLKA AKCYJNA", "SPÓŁKA AKCYJNA",
                       "CORPORATION", "CORP", "CO.", "& CO"]:
            n = n.replace(suffix, "")
        # Keep only alphanum
        n = re.sub(r'[^A-Z0-9\s]', ' ', n)
        return set(w for w in n.split() if len(w) > 2)

    words1 = clean(name1)
    words2 = clean(name2)

    if not words1 or not words2:
        return 0.0

    intersection = words1 & words2
    union = words1 | words2

    if not union:
        return 0.0

    return len(intersection) / len(union)
