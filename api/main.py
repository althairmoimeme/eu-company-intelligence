"""FastAPI application."""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

from .settings import get_settings
from .routers import companies, scrape, signals
from .routers.favorites import router as favorites_router
from .routers.founder import router as founder_router
from .routers.dashboard import router as dashboard_router
from .routers.export import router as export_router
from .routers.equans import router as equans_router
from .auth import require_auth
from scraper.db.session import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db(settings.DATABASE_PATH)
    yield


app = FastAPI(
    title="EU Company Database",
    description="Entreprises européennes avec CA > 75M€",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(companies.router, prefix="/api/v1", dependencies=[Depends(require_auth)])
app.include_router(scrape.router, prefix="/api/v1", dependencies=[Depends(require_auth)])
app.include_router(signals.router, prefix="/api/v1", dependencies=[Depends(require_auth)])
app.include_router(favorites_router, prefix="/api/v1", dependencies=[Depends(require_auth)])
app.include_router(founder_router, prefix="/api/v1", dependencies=[Depends(require_auth)])
app.include_router(dashboard_router, prefix="/api/v1", dependencies=[Depends(require_auth)])
app.include_router(export_router, prefix="/api/v1", dependencies=[Depends(require_auth)])
app.include_router(equans_router, prefix="/api/v1", dependencies=[Depends(require_auth)])


@app.get("/", include_in_schema=False)
async def serve_frontend(auth=Depends(require_auth)):
    frontend_path = os.path.join(os.path.dirname(__file__), "../frontend/index.html")
    return FileResponse(frontend_path)


@app.get("/health")
async def health():
    return {"status": "ok"}
