"""SQLite migrations — run before restarting the API when schema changes."""
import sqlite3
import sys
import logging

logger = logging.getLogger(__name__)


def migrate(db_path: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # ── directors: add appointed_at ─────────────────────────────────────────
    cols = [r[1] for r in cur.execute("PRAGMA table_info(directors)")]
    if "appointed_at" not in cols:
        cur.execute("ALTER TABLE directors ADD COLUMN appointed_at DATE")
        print("✓ Added appointed_at to directors")
    else:
        print("  appointed_at already present")

    # ── broker_listings table ────────────────────────────────────────────────
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if "broker_listings" not in tables:
        cur.execute("""
            CREATE TABLE broker_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                listing_name TEXT NOT NULL,
                listing_url TEXT,
                sector_hint TEXT,
                region_hint TEXT,
                price_hint TEXT,
                matched_company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
                match_score REAL,
                scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("✓ Created broker_listings table")
    else:
        print("  broker_listings already present")

    # ── favorites_lists table ────────────────────────────────────────────────
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if "favorites_lists" not in tables:
        cur.execute("""
            CREATE TABLE favorites_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                description TEXT,
                investment_thesis TEXT,
                filter_snapshot TEXT,
                color VARCHAR(20) DEFAULT 'blue',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("✓ Created favorites_lists table")
    else:
        print("  favorites_lists already present")

    # ── favorites_list_items table ───────────────────────────────────────────
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if "favorites_list_items" not in tables:
        cur.execute("""
            CREATE TABLE favorites_list_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER NOT NULL REFERENCES favorites_lists(id) ON DELETE CASCADE,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                notes TEXT,
                added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_fav_list_company UNIQUE (list_id, company_id)
            )
        """)
        print("✓ Created favorites_list_items table")
    else:
        print("  favorites_list_items already present")

    conn.commit()
    conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "companies.db"
    migrate(path)
