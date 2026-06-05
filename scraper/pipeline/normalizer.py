"""Normalize raw scraper output to a unified dict for DB upsert."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DirectorRecord:
    name: str
    role: Optional[str] = None
    birth_year: Optional[int] = None
    nationality: Optional[str] = None
    appointed_at: Optional[str] = None   # ISO date "YYYY-MM-DD"


@dataclass
class CompanyRecord:
    name: str
    country: str                       # ISO 2-letter
    registration_number: str
    source_url: Optional[str] = None

    revenue_eur: Optional[float] = None
    revenue_year: Optional[int] = None
    revenue_estimated: bool = False

    employees: Optional[int] = None
    sector: Optional[str] = None
    nace_code: Optional[str] = None
    activity_description: Optional[str] = None

    creation_date: Optional[str] = None   # ISO date string
    address: Optional[str] = None
    city: Optional[str] = None
    postal_code: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

    directors: list[DirectorRecord] = field(default_factory=list)
