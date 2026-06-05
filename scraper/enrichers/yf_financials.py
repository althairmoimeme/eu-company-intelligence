"""Enrichissement historique financier via Yahoo Finance.

Pour les entreprises avec un ticker Yahoo Finance connu (listes DE, IT, SE, CH, UK listed, etc.)
Récupère 4-5 ans de CA, résultat net, trésorerie, dette.
"""
import asyncio
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db.session import get_session_factory
from ..db.models import Company, FinancialHistory

logger = logging.getLogger(__name__)

# Taux de change approximatifs vers EUR (pour les pays hors zone €)
FX = {
    "GB": 1.17,   # GBP → EUR  (1 GBP = 1.17 EUR)
    "SE": 0.087,  # SEK → EUR
    "CH": 1.04,   # CHF → EUR
    "NO": 0.086,  # NOK → EUR
    "PL": 0.23,   # PLN → EUR
    "DK": 0.134,  # DKK → EUR
}

# Suffixes Yahoo Finance par pays
YF_SUFFIX = {
    "FR": ".PA",
    "DE": ".DE",
    "IT": ".MI",
    "SE": ".ST",
    "CH": ".SW",
    "NO": ".OL",
    "DK": ".CO",
    "GB": "",  # LSE: souvent sans suffixe ou .L
}


def _guess_ticker(company: Company) -> str | None:
    """Essaie de deviner le ticker Yahoo Finance depuis les données de l'entreprise."""
    # Le ticker peut être stocké dans source_url (ex: "https://finance.yahoo.com/quote/TTE.PA")
    if company.source_url and "yahoo" in (company.source_url or ""):
        import re
        m = re.search(r'/quote/([A-Z0-9\.\-]+)', company.source_url)
        if m:
            return m.group(1)

    # Sinon, on cherche dans website
    return None


def _fetch_yf_history_sync(ticker_symbol: str, currency: str, fx_rate: float) -> list[dict]:
    """Version synchrone pour run_in_executor."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(ticker_symbol)
        income = ticker.income_stmt
        balance = ticker.balance_sheet

        if income is None or income.empty:
            return []

        snapshots = []
        for col in income.columns:
            try:
                year = col.year if hasattr(col, 'year') else int(str(col)[:4])
            except Exception:
                continue

            snap = {"year": year, "source": "yfinance"}

            for key in ["Total Revenue", "Revenue"]:
                if key in income.index:
                    v = income.loc[key, col]
                    if v is not None and str(v) not in ("nan", "None", "<NA>"):
                        snap["revenue_eur"] = float(v) / fx_rate
                        break

            for key in ["Operating Income", "EBIT"]:
                if key in income.index:
                    v = income.loc[key, col]
                    if v is not None and str(v) not in ("nan", "None", "<NA>"):
                        snap["operating_income_eur"] = float(v) / fx_rate
                        break

            for key in ["Net Income", "Net Income Common Stockholders"]:
                if key in income.index:
                    v = income.loc[key, col]
                    if v is not None and str(v) not in ("nan", "None", "<NA>"):
                        snap["net_income_eur"] = float(v) / fx_rate
                        break

            for key in ["EBITDA", "Normalized EBITDA"]:
                if key in income.index:
                    v = income.loc[key, col]
                    if v is not None and str(v) not in ("nan", "None", "<NA>"):
                        snap["ebitda_eur"] = float(v) / fx_rate
                        break

            if balance is not None and not balance.empty and col in balance.columns:
                for key in ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]:
                    if key in balance.index:
                        v = balance.loc[key, col]
                        if v is not None and str(v) not in ("nan", "None", "<NA>"):
                            snap["cash_eur"] = float(v) / fx_rate
                            break

                for key in ["Total Debt", "Long Term Debt"]:
                    if key in balance.index:
                        v = balance.loc[key, col]
                        if v is not None and str(v) not in ("nan", "None", "<NA>"):
                            snap["debt_eur"] = float(v) / fx_rate
                            break

            if len(snap) > 2:
                snapshots.append(snap)

        return snapshots
    except Exception as e:
        logger.debug(f"[YF-FIN] Erreur sync {ticker_symbol}: {e}")
        return []


async def enrich_financial_history(
    db_path: str,
    countries: list[str] | None = None,
    limit: int | None = None,
    concurrency: int = 3,
):
    """
    Enrichit financial_history pour les entreprises dont on peut deviner le ticker YF.

    Cible : entreprises DE, IT, SE, CH, GB, NO, FR (cotées) avec source_url Yahoo Finance.
    """
    factory = get_session_factory(db_path)

    # Récupérer les entreprises avec une source_url Yahoo Finance
    async with factory() as session:
        q = select(Company).where(
            Company.source_url.ilike("%yahoo%"),
        )
        if countries:
            q = q.where(Company.country.in_(countries))
        result = await session.execute(q)
        companies = result.scalars().all()

    if limit:
        companies = companies[:limit]

    logger.info(f"[YF-FIN] {len(companies)} entreprises avec ticker YF à enrichir")

    sem = asyncio.Semaphore(concurrency)
    total_enriched = 0

    async def process_one(company):
        nonlocal total_enriched
        ticker = _guess_ticker(company)
        if not ticker:
            return

        fx_rate = 1.0
        currency = "EUR"
        if company.country in FX:
            fx_rate = FX[company.country]
            currency = company.country

        async with sem:
            loop = asyncio.get_event_loop()
            snaps = await loop.run_in_executor(
                None, lambda: _fetch_yf_history_sync(ticker, currency, fx_rate)
            )

        if not snaps:
            return

        async with factory() as session:
            for snap in snaps:
                stmt = sqlite_insert(FinancialHistory).values(
                    company_id=company.id,
                    year=snap["year"],
                    revenue_eur=snap.get("revenue_eur"),
                    operating_income_eur=snap.get("operating_income_eur"),
                    net_income_eur=snap.get("net_income_eur"),
                    cash_eur=snap.get("cash_eur"),
                    debt_eur=snap.get("debt_eur"),
                    ebitda_eur=snap.get("ebitda_eur"),
                    source="yfinance",
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["company_id", "year"],
                    set_=dict(
                        revenue_eur=stmt.excluded.revenue_eur,
                        operating_income_eur=stmt.excluded.operating_income_eur,
                        net_income_eur=stmt.excluded.net_income_eur,
                        cash_eur=stmt.excluded.cash_eur,
                        debt_eur=stmt.excluded.debt_eur,
                        ebitda_eur=stmt.excluded.ebitda_eur,
                    )
                )
                await session.execute(stmt)
            await session.commit()

        total_enriched += 1
        logger.info(f"[YF-FIN] {company.name} ({ticker}): {len(snaps)} années enrichies")

    tasks = [process_one(c) for c in companies]
    await asyncio.gather(*tasks, return_exceptions=True)

    logger.info(f"[YF-FIN] Terminé — {total_enriched}/{len(companies)} enrichis")
    return total_enriched
