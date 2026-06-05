#!/usr/bin/env python3
"""Quick launcher — python run.py"""
import uvicorn
from dotenv import load_dotenv
load_dotenv()

if __name__ == "__main__":
    import asyncio
    from scraper.db.session import init_db
    from api.settings import get_settings
    settings = get_settings()
    asyncio.run(init_db(settings.DATABASE_PATH))
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
