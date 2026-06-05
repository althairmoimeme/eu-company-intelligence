# EU Company Database

Scraper d'entreprises européennes avec CA > 75M€ — avec interface web pour filtrer, trier et exporter.

## Installation

```bash
cd eu-company-scraper
pip install -e .
cp .env.example .env
# Remplir les clés API dans .env
```

## Démarrage rapide

```bash
python run.py
# → http://localhost:8000
```

## Sources de données

| Pays | Source | CA | Dirigeants + âge | Inscription |
|------|--------|-----|-------------------|-------------|
| 🇫🇷 France | Pappers API | ✅ | ✅ | [pappers.fr/api](https://www.pappers.fr/api) |
| 🇬🇧 UK | Companies House | ❌ | ✅ | [developer.company-information.service.gov.uk](https://developer.company-information.service.gov.uk/) |
| 🇳🇴 Norvège | Brønnøysund | ✅ (NOK) | ✅ | Aucune (API publique) |
| 🇩🇰 Danemark | CVR Elasticsearch | ❌* | ✅ | Email: cvrselvbetjening@erst.dk |
| 🇧🇪 Belgique | OpenCorporates | ❌ | Partiel | [opencorporates.com](https://opencorporates.com/api_accounts/new) |

*Le CA danois nécessite un accès aux dépôts XBRL de l'Erhvervsstyrelsen.

## Lancer un scraping

Via l'interface web (bouton "Scraper") ou en ligne de commande :

```bash
# France uniquement
python -m scraper.cli run --scrapers pappers_fr

# Tous les scrapers disponibles
python -m scraper.cli run

# Repartir de zéro (ignorer les checkpoints)
python -m scraper.cli run --no-resume

# Stats de la base
python -m scraper.cli stats
```

## Structure

```
eu-company-scraper/
├── scraper/
│   ├── scrapers/        # Un scraper par pays
│   ├── pipeline/        # Orchestrateur + normalisation données
│   ├── enrichers/       # Conversion devises (ECB) + codes NACE
│   └── db/              # SQLite (SQLAlchemy async)
├── api/                 # FastAPI REST API
├── frontend/            # Interface web (HTML + Alpine.js)
├── .env                 # Clés API (à créer depuis .env.example)
└── companies.db         # Base SQLite (créée automatiquement)
```

## API REST

```
GET  /api/v1/companies          # Liste filtrée + paginée
GET  /api/v1/companies/{id}     # Détail
GET  /api/v1/companies/stats    # Statistiques globales
GET  /api/v1/companies/export/csv  # Export CSV
POST /api/v1/scrape/run         # Lancer un scraping
GET  /api/v1/scrape/status      # Statut des runs
```

Docs Swagger : http://localhost:8000/docs
