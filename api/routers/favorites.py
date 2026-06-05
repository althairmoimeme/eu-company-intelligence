"""Favorites lists management router."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from datetime import datetime
import math

from ..settings import get_settings
from scraper.db.session import get_session_factory
from scraper.db.models import FavoritesList, FavoritesListItem, Company, Director

router = APIRouter(prefix="/favorites", tags=["favorites"])


def _get_db_dep():
    settings = get_settings()
    factory = get_session_factory(settings.DATABASE_PATH)
    async def dep():
        async with factory() as session:
            yield session
    return dep

db_dep = _get_db_dep()


# ── Schemas ─────────────────────────────────────────────────────────────────

class ListCreate(BaseModel):
    name: str
    description: Optional[str] = None
    investment_thesis: Optional[str] = None
    color: str = "blue"
    filter_snapshot: Optional[str] = None  # JSON string

class ListUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    investment_thesis: Optional[str] = None
    color: Optional[str] = None
    filter_snapshot: Optional[str] = None

class ItemAdd(BaseModel):
    company_id: int
    notes: Optional[str] = None
    status: str = "prospect"

class ItemUpdate(BaseModel):
    notes: Optional[str] = None
    status: Optional[str] = None
    next_action: Optional[str] = None
    next_action_date: Optional[str] = None   # ISO date string
    contact_channel: Optional[str] = None

CRM_STATUSES = ["prospect", "to_contact", "contacted", "replied", "meeting", "negociation", "deal", "pass"]


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _list_to_dict(session, lst: FavoritesList) -> dict:
    count_result = await session.execute(
        select(func.count()).select_from(FavoritesListItem)
        .where(FavoritesListItem.list_id == lst.id)
    )
    count = count_result.scalar() or 0
    return {
        "id": lst.id,
        "name": lst.name,
        "description": lst.description,
        "investment_thesis": lst.investment_thesis,
        "filter_snapshot": lst.filter_snapshot,
        "color": lst.color,
        "item_count": count,
        "created_at": lst.created_at.isoformat() if lst.created_at else None,
        "updated_at": lst.updated_at.isoformat() if lst.updated_at else None,
    }


# ── List CRUD ────────────────────────────────────────────────────────────────

@router.get("/lists")
async def get_all_lists(session: AsyncSession = Depends(db_dep)):
    """Get all favorites lists with item counts."""
    result = await session.execute(
        select(FavoritesList).order_by(FavoritesList.updated_at.desc())
    )
    lists = result.scalars().all()
    return [await _list_to_dict(session, lst) for lst in lists]


@router.post("/lists")
async def create_list(body: ListCreate, session: AsyncSession = Depends(db_dep)):
    """Create a new favorites list."""
    async with session.begin():
        lst = FavoritesList(
            name=body.name,
            description=body.description,
            investment_thesis=body.investment_thesis,
            color=body.color,
            filter_snapshot=body.filter_snapshot,
        )
        session.add(lst)
        await session.flush()
        return await _list_to_dict(session, lst)


@router.get("/lists/{list_id}")
async def get_list(
    list_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort_by: str = Query(default="added_at"),
    sort_dir: str = Query(default="desc"),
    session: AsyncSession = Depends(db_dep),
):
    """Get a favorites list with its companies (paginated)."""
    lst = await session.get(FavoritesList, list_id)
    if not lst:
        raise HTTPException(404, "List not found")

    # Count items
    count_result = await session.execute(
        select(func.count()).select_from(FavoritesListItem)
        .where(FavoritesListItem.list_id == list_id)
    )
    total = count_result.scalar() or 0

    # Fetch items with companies
    q = (
        select(FavoritesListItem)
        .where(FavoritesListItem.list_id == list_id)
        .options(selectinload(FavoritesListItem.company).selectinload(Company.directors))
    )
    if sort_by == "added_at":
        q = q.order_by(
            FavoritesListItem.added_at.desc() if sort_dir == "desc"
            else FavoritesListItem.added_at.asc()
        )
    elif sort_by == "name":
        q = q.join(Company, FavoritesListItem.company_id == Company.id).order_by(
            Company.name.asc() if sort_dir == "asc" else Company.name.desc()
        )
    elif sort_by == "revenue_eur":
        q = q.join(Company, FavoritesListItem.company_id == Company.id).order_by(
            Company.revenue_eur.desc().nulls_last() if sort_dir == "desc"
            else Company.revenue_eur.asc().nulls_last()
        )
    else:
        q = q.order_by(FavoritesListItem.added_at.desc())

    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(q)
    items = result.scalars().all()

    # Import MA scoring
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from api.routers.companies import _compute_ma, CURRENT_YEAR

    item_dicts = []
    for item in items:
        c = item.company
        d = c.to_dict() if c else {}
        if c:
            ma_score, ma_signals = _compute_ma(c)
            d["ma_score"] = ma_score
            d["ma_signals"] = ma_signals
        item_dicts.append({
            "id": item.id,
            "company_id": item.company_id,
            "notes": item.notes,
            "status": item.status,
            "added_at": item.added_at.isoformat() if item.added_at else None,
            "contacted_at": item.contacted_at.isoformat() if item.contacted_at else None,
            "last_activity_at": item.last_activity_at.isoformat() if item.last_activity_at else None,
            "next_action": item.next_action,
            "next_action_date": item.next_action_date.isoformat() if item.next_action_date else None,
            "contact_channel": item.contact_channel,
            "company": d,
        })

    list_dict = await _list_to_dict(session, lst)
    list_dict.update({
        "items": item_dicts,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, math.ceil(total / page_size)),
    })
    return list_dict


@router.patch("/lists/{list_id}")
async def update_list(list_id: int, body: ListUpdate, session: AsyncSession = Depends(db_dep)):
    """Update list metadata."""
    async with session.begin():
        lst = await session.get(FavoritesList, list_id)
        if not lst:
            raise HTTPException(404, "List not found")
        if body.name is not None:
            lst.name = body.name
        if body.description is not None:
            lst.description = body.description
        if body.investment_thesis is not None:
            lst.investment_thesis = body.investment_thesis
        if body.color is not None:
            lst.color = body.color
        if body.filter_snapshot is not None:
            lst.filter_snapshot = body.filter_snapshot
        lst.updated_at = datetime.utcnow()
    return await _list_to_dict(session, lst)


@router.delete("/lists/{list_id}")
async def delete_list(list_id: int, session: AsyncSession = Depends(db_dep)):
    """Delete a favorites list and all its items."""
    async with session.begin():
        lst = await session.get(FavoritesList, list_id)
        if not lst:
            raise HTTPException(404, "List not found")
        await session.delete(lst)
    return {"status": "deleted", "id": list_id}


# ── Item CRUD ────────────────────────────────────────────────────────────────

@router.post("/lists/{list_id}/items")
async def add_to_list(list_id: int, body: ItemAdd, session: AsyncSession = Depends(db_dep)):
    """Add a company to a favorites list. Returns 409 if already present."""
    async with session.begin():
        lst = await session.get(FavoritesList, list_id)
        if not lst:
            raise HTTPException(404, "List not found")

        # Check duplicate
        existing = await session.execute(
            select(FavoritesListItem)
            .where(FavoritesListItem.list_id == list_id,
                   FavoritesListItem.company_id == body.company_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(409, "Company already in this list")

        item = FavoritesListItem(
            list_id=list_id,
            company_id=body.company_id,
            notes=body.notes,
            status=body.status,
        )
        session.add(item)
        await session.flush()
        lst.updated_at = datetime.utcnow()
        added_at_str = item.added_at.isoformat() if item.added_at else None
        item_id = item.id

    return {"id": item_id, "list_id": list_id, "company_id": body.company_id, "added_at": added_at_str}


@router.delete("/lists/{list_id}/items/{company_id}")
async def remove_from_list(list_id: int, company_id: int, session: AsyncSession = Depends(db_dep)):
    """Remove a company from a favorites list."""
    async with session.begin():
        result = await session.execute(
            select(FavoritesListItem)
            .where(FavoritesListItem.list_id == list_id,
                   FavoritesListItem.company_id == company_id)
        )
        item = result.scalar_one_or_none()
        if not item:
            raise HTTPException(404, "Item not found in list")
        await session.delete(item)
    return {"status": "removed", "list_id": list_id, "company_id": company_id}


@router.patch("/lists/{list_id}/items/{company_id}")
async def update_item(
    list_id: int, company_id: int, body: ItemUpdate,
    session: AsyncSession = Depends(db_dep),
):
    """Update notes, status, next_action, etc. on a company in a list."""
    from datetime import date as _date
    async with session.begin():
        result = await session.execute(
            select(FavoritesListItem)
            .where(FavoritesListItem.list_id == list_id,
                   FavoritesListItem.company_id == company_id)
        )
        item = result.scalar_one_or_none()
        if not item:
            raise HTTPException(404, "Item not found in list")
        if body.notes is not None:
            item.notes = body.notes
        if body.status is not None:
            old_status = item.status
            item.status = body.status
            # Auto-set contacted_at on first contact
            if body.status == "contacted" and old_status in ("prospect", "to_contact") and not item.contacted_at:
                item.contacted_at = datetime.utcnow()
        if body.next_action is not None:
            item.next_action = body.next_action
        if body.next_action_date is not None:
            try:
                item.next_action_date = _date.fromisoformat(body.next_action_date)
            except Exception:
                pass
        if body.contact_channel is not None:
            item.contact_channel = body.contact_channel
        item.last_activity_at = datetime.utcnow()
        # Save values before session closes
        ret = {
            "status": item.status,
            "notes": item.notes,
            "next_action": item.next_action,
            "next_action_date": item.next_action_date.isoformat() if item.next_action_date else None,
            "contacted_at": item.contacted_at.isoformat() if item.contacted_at else None,
            "contact_channel": item.contact_channel,
        }
    return ret


@router.patch("/lists/{list_id}/items/{company_id}/status")
async def update_item_status(
    list_id: int, company_id: int,
    body: ItemUpdate,
    session: AsyncSession = Depends(db_dep),
):
    """Update pipeline status of a company in a list."""
    async with session.begin():
        result = await session.execute(
            select(FavoritesListItem)
            .where(FavoritesListItem.list_id == list_id,
                   FavoritesListItem.company_id == company_id)
        )
        item = result.scalar_one_or_none()
        if not item:
            raise HTTPException(404, "Item not found in list")
        if body.status is not None:
            old_status = item.status
            item.status = body.status
            if body.status == "contacted" and old_status in ("prospect", "to_contact") and not item.contacted_at:
                item.contacted_at = datetime.utcnow()
            item.last_activity_at = datetime.utcnow()
        status_val = item.status
    return {"status": status_val, "company_id": company_id}


@router.get("/company/{company_id}/lists")
async def get_company_lists(company_id: int, session: AsyncSession = Depends(db_dep)):
    """Get all lists that contain a specific company."""
    result = await session.execute(
        select(FavoritesListItem, FavoritesList)
        .join(FavoritesList, FavoritesListItem.list_id == FavoritesList.id)
        .where(FavoritesListItem.company_id == company_id)
    )
    rows = result.all()
    return [
        {"list_id": item.list_id, "list_name": lst.name, "list_color": lst.color, "notes": item.notes}
        for item, lst in rows
    ]


@router.get("/lists/{list_id}/pipeline-stats")
async def get_pipeline_stats(list_id: int, session: AsyncSession = Depends(db_dep)):
    """Retourne le comptage par statut CRM pour une liste."""
    from sqlalchemy import func as sqlfunc
    lst = await session.get(FavoritesList, list_id)
    if not lst:
        raise HTTPException(404, "List not found")
    result = await session.execute(
        select(FavoritesListItem.status, sqlfunc.count(FavoritesListItem.id))
        .where(FavoritesListItem.list_id == list_id)
        .group_by(FavoritesListItem.status)
    )
    counts = {row[0]: row[1] for row in result.all()}
    # Retourner tous les statuts dans l'ordre du pipeline
    pipeline = []
    for s in CRM_STATUSES:
        pipeline.append({"status": s, "count": counts.get(s, 0)})
    return {"list_id": list_id, "pipeline": pipeline, "total": sum(counts.values())}


@router.get("/all-favorited-ids")
async def get_all_favorited_ids(session: AsyncSession = Depends(db_dep)):
    """Get distinct company IDs that are in any list (for UI star indicators)."""
    result = await session.execute(
        select(FavoritesListItem.company_id).distinct()
    )
    return {"ids": [row[0] for row in result.all()]}
