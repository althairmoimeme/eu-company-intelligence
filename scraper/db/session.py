"""Async SQLAlchemy session factory."""
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text
from .models import Base

_engine = None
_session_factory = None
_write_lock = asyncio.Lock()


def get_engine(db_path: str = "companies.db"):
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={
                "check_same_thread": False,
                "timeout": 30,  # wait up to 30s for lock to release
            },
            echo=False,
        )
    return _engine


def get_session_factory(db_path: str = "companies.db") -> async_sessionmaker:
    global _session_factory
    if _session_factory is None:
        engine = get_engine(db_path)
        _session_factory = async_sessionmaker(engine, expire_on_commit=False,
                                               class_=AsyncSession)
    return _session_factory


async def init_db(db_path: str = "companies.db"):
    engine = get_engine(db_path)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Enable WAL mode for better concurrent access (persists in DB file)
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA busy_timeout=30000"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        try:
            await conn.execute(text("ALTER TABLE favorites_list_items ADD COLUMN status VARCHAR(20) DEFAULT 'prospect'"))
        except Exception:
            pass  # column already exists
        try:
            await conn.execute(text("ALTER TABLE companies ADD COLUMN nace_inferred VARCHAR"))
        except Exception:
            pass  # column already exists
        try:
            await conn.execute(text("ALTER TABLE company_scores_equans ADD COLUMN is_european BOOLEAN DEFAULT 1"))
        except Exception:
            pass  # column already exists
        try:
            await conn.execute(text("ALTER TABLE company_scores_equans ADD COLUMN revenue_bracket VARCHAR(20)"))
        except Exception:
            pass  # column already exists


async def get_session(db_path: str = "companies.db"):
    factory = get_session_factory(db_path)
    async with factory() as session:
        yield session
