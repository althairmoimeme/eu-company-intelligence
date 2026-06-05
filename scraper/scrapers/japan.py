"""Japan scraper — TSE (Tokyo Stock Exchange) listed companies.

Source 1 : JPX (Japan Exchange Group) — liste officielle des titres cotés
Source 2 : yfinance (Yahoo Finance) — CA, effectif, dirigeants, site web

~3 595 sociétés cotées (Prime + Standard + Growth), hors ETF/REIT.
Secteurs cibles M&A : industrie, chimie, machines, commerce de gros, services B2B.
Devise : JPY → EUR (taux ~0.0062, soit ¥160 ≈ €1).
"""
import asyncio
import logging
from typing import AsyncIterator

import httpx
import xlrd
import yfinance as yf

from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord

logger = logging.getLogger(__name__)

JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
JPY_TO_EUR = 0.0062  # ¥160 ≈ €1

# Secteurs TSE 33 → label interne
SECTOR_MAP = {
    "水産・農林業": "Agriculture",
    "鉱業": "Énergie",
    "建設業": "Construction",
    "食料品": "Alimentaire",
    "繊維製品": "Industrie",
    "パルプ・紙": "Industrie",
    "化学": "Chimie",
    "医薬品": "Santé",
    "石油・石炭製品": "Énergie",
    "ゴム製品": "Industrie",
    "ガラス・土石製品": "Industrie",
    "鉄鋼": "Industrie",
    "非鉄金属": "Industrie",
    "金属製品": "Industrie",
    "機械": "Machines/Équipements",
    "電気機器": "Électronique",
    "輸送用機器": "Automobile",
    "精密機器": "Électronique",
    "その他製品": "Industrie",
    "電気・ガス業": "Énergie",
    "陸運業": "Transport/Logistique",
    "海運業": "Transport/Logistique",
    "空運業": "Transport/Logistique",
    "倉庫・運輸関連業": "Transport/Logistique",
    "情報・通信業": "Tech/IT",
    "卸売業": "Commerce de gros",
    "小売業": "Commerce de détail",
    "不動産業": "Immobilier",
    "サービス業": "Services B2B",
}

# Secteurs financiers à exclure pour M&A
EXCLUDE_SECTORS = {"銀行業", "証券、商品先物取引業", "保険業", "その他金融業"}


def _fetch_ticker_info(ticker: str) -> dict | None:
    """Récupère les données yfinance (synchrone, à appeler via to_thread)."""
    try:
        info = yf.Ticker(ticker).info
        if not info or info.get("trailingPegRatio") is None and not info.get("longName"):
            # Ticker vide / inconnu
            return None
        return info
    except Exception as e:
        logger.debug(f"[JP] yfinance erreur {ticker}: {e}")
        return None


def _parse_info(info: dict, ticker: str) -> tuple[dict, list[dict]]:
    """Extrait les champs utiles de l'objet info yfinance."""
    rev_jpy = info.get("totalRevenue") or 0
    data = {
        "ticker": ticker,
        "revenue_eur": round(rev_jpy * JPY_TO_EUR, 0) if rev_jpy > 0 else None,
        "employees": info.get("fullTimeEmployees"),
        "activity_description": (info.get("longBusinessSummary") or "")[:500] or None,
        "website": info.get("website"),
        "city": info.get("city"),
        "sector_yf": info.get("sector"),
    }

    officers = []
    for o in (info.get("companyOfficers") or [])[:5]:
        name = (o.get("name") or "").strip()
        if not name:
            continue
        birth_year = o.get("yearBorn")
        officers.append({
            "name": name,
            "role": o.get("title", ""),
            "birth_year": int(birth_year) if birth_year else None,
        })

    return data, officers


class JapanScraper(BaseScraper):
    name = "tse_jp"
    country = "JP"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        min_revenue = self.config.get("MIN_REVENUE_EUR", 5_000_000)
        checkpoint = self.load_checkpoint() if resume else {}
        done_tickers: set[str] = set(checkpoint.get("done", []))

        # Télécharger la liste JPX
        logger.info("[JP] Téléchargement liste JPX...")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(JPX_URL)
            resp.raise_for_status()
        xls_data = resp.content

        # Parser le fichier Excel
        wb = xlrd.open_workbook(file_contents=xls_data)
        ws = wb.sheet_by_index(0)

        companies = []
        for i in range(1, ws.nrows):
            market = str(ws.cell_value(i, 3))
            code_raw = ws.cell_value(i, 1)
            name = str(ws.cell_value(i, 2)).strip()
            sector = str(ws.cell_value(i, 5))

            # Titres actions internes uniquement
            if "内国株式" not in market:
                continue
            if sector in EXCLUDE_SECTORS:
                continue
            try:
                code = int(float(code_raw))
            except Exception:
                continue

            ticker = f"{code}.T"
            if ticker in done_tickers:
                continue

            companies.append((ticker, name, sector))

        logger.info(f"[JP] {len(companies)} sociétés à traiter (hors financières/ETF)")

        # Semaphore pour limiter la concurrence sur yfinance
        sem = asyncio.Semaphore(4)

        async def process_one(ticker: str, name: str, sector_jp: str):
            async with sem:
                info = await asyncio.to_thread(_fetch_ticker_info, ticker)
                await asyncio.sleep(0.3)  # politesse
                return info

        for ticker, name, sector_jp in companies:
            info = await process_one(ticker, name, sector_jp)

            if not info:
                done_tickers.add(ticker)
                continue

            yf_data, officers = _parse_info(info, ticker)
            revenue = yf_data.get("revenue_eur")

            if revenue and revenue < min_revenue:
                done_tickers.add(ticker)
                continue

            # Secteur : privilégier le mapping TSE, sinon le secteur Yahoo Finance
            sector_label = SECTOR_MAP.get(sector_jp) or yf_data.get("sector_yf") or sector_jp

            directors = [
                DirectorRecord(
                    name=d["name"],
                    role=d.get("role"),
                    birth_year=d.get("birth_year"),
                )
                for d in officers
            ]

            record = CompanyRecord(
                name=name,
                country="JP",
                registration_number=ticker.replace(".T", ""),
                revenue_eur=revenue,
                revenue_year=2024,
                employees=yf_data.get("employees"),
                sector=sector_label,
                activity_description=yf_data.get("activity_description"),
                city=yf_data.get("city"),
                website=yf_data.get("website"),
                source_url=f"https://finance.yahoo.com/quote/{ticker}",
                directors=directors,
            )

            done_tickers.add(ticker)
            self.save_checkpoint({"done": list(done_tickers)})
            yield record

        logger.info("[JP] Terminé")
