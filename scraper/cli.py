"""CLI for running scrapers."""
import asyncio
import logging
import click
from rich.console import Console
from rich.table import Table
from dotenv import load_dotenv
import os

load_dotenv()
console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
)


@click.group()
def cli():
    """EU Company Scraper CLI"""
    pass


@cli.command()
@click.option("--scrapers", "-s", multiple=True,
              help="Scrapers to run (pappers_fr, companies_house_uk, brreg_no, cvr_dk, opencorporates_be)")
@click.option("--no-resume", is_flag=True, default=False, help="Start fresh (ignore checkpoints)")
@click.option("--db", default="companies.db", help="SQLite database path")
def run(scrapers, no_resume, db):
    """Run scrapers and populate the database."""
    from .pipeline.orchestrator import run_all, SCRAPER_MAP
    from .db.session import init_db

    config = {k: os.getenv(k, "") for k in [
        "PAPPERS_API_KEY", "COMPANIES_HOUSE_API_KEY", "CBEAPI_KEY",
        "OPENCORPORATES_API_TOKEN", "CVR_USERNAME", "CVR_PASSWORD",
    ]}
    config["MIN_REVENUE_EUR"] = float(os.getenv("MIN_REVENUE_EUR", "75000000"))
    config["MIN_EMPLOYEES_PROXY"] = int(os.getenv("MIN_EMPLOYEES_PROXY", "200"))

    targets = list(scrapers) if scrapers else list(SCRAPER_MAP.keys())

    console.print(f"\n[bold green]Démarrage du scraping[/bold green]")
    console.print(f"Scrapers : {', '.join(targets)}")
    console.print(f"DB : {db}")
    console.print(f"Reprise : {'Non' if no_resume else 'Oui'}\n")

    async def _run():
        await init_db(db)
        results = await run_all(config=config, scrapers=targets,
                                 resume=not no_resume, db_path=db)
        return results

    results = asyncio.run(_run())

    table = Table(title="Résultats du scraping")
    table.add_column("Scraper", style="cyan")
    table.add_column("Ajoutées", style="green")
    table.add_column("Mises à jour", style="yellow")
    table.add_column("Erreur", style="red")

    for name, r in results.items():
        table.add_row(
            name,
            str(r.get("added", 0)),
            str(r.get("updated", 0)),
            r.get("error", "") or "",
        )
    console.print(table)


@cli.command()
@click.option("--db", default="companies.db", help="SQLite database path")
def stats(db):
    """Show database statistics."""
    import sqlite3
    if not os.path.exists(db):
        console.print(f"[red]Database not found: {db}[/red]")
        return

    conn = sqlite3.connect(db)
    cur = conn.cursor()

    total = cur.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    with_revenue = cur.execute("SELECT COUNT(*) FROM companies WHERE revenue_eur IS NOT NULL").fetchone()[0]
    avg_rev = cur.execute("SELECT AVG(revenue_eur) FROM companies WHERE revenue_eur IS NOT NULL").fetchone()[0]

    console.print(f"\n[bold]Base de données:[/bold] {db}")
    console.print(f"Total entreprises : [bold green]{total:,}[/bold green]")
    console.print(f"Avec CA renseigné : [bold]{with_revenue:,}[/bold]")
    if avg_rev:
        console.print(f"CA moyen : [bold]{avg_rev/1e6:.1f}M€[/bold]")

    table = Table(title="Par pays")
    table.add_column("Pays")
    table.add_column("Nb entreprises", justify="right")
    for row in cur.execute("SELECT country, COUNT(*) FROM companies GROUP BY country ORDER BY COUNT(*) DESC"):
        table.add_row(row[0], f"{row[1]:,}")
    console.print(table)

    table2 = Table(title="Top secteurs")
    table2.add_column("Secteur")
    table2.add_column("Nb", justify="right")
    for row in cur.execute("SELECT sector, COUNT(*) FROM companies WHERE sector IS NOT NULL GROUP BY sector ORDER BY COUNT(*) DESC LIMIT 15"):
        table2.add_row(row[0] or "—", f"{row[1]:,}")
    console.print(table2)
    conn.close()


@cli.command()
@click.option("--host", default="0.0.0.0")
@click.option("--port", default=8000)
def serve(host, port):
    """Start the API server with the web frontend."""
    import uvicorn
    console.print(f"\n[bold green]Démarrage du serveur[/bold green] → http://localhost:{port}")
    uvicorn.run("api.main:app", host=host, port=port, reload=False)


@cli.command()
@click.argument("scraper_name")
def clear_checkpoint(scraper_name):
    """Clear a scraper's checkpoint to restart from scratch."""
    from .utils.checkpoints import clear_checkpoint as _clear
    _clear(scraper_name)
    console.print(f"[green]Checkpoint effacé pour : {scraper_name}[/green]")


if __name__ == "__main__":
    cli()
