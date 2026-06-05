from pydantic import BaseModel
from typing import Optional


class DirectorOut(BaseModel):
    id: int
    name: str
    role: Optional[str]
    birth_year: Optional[int]
    age: Optional[int]
    nationality: Optional[str]
    appointed_at: Optional[str] = None
    tenure_years: Optional[int] = None

    model_config = {"from_attributes": True}


class CompanySummary(BaseModel):
    id: int
    name: str
    country: str
    sector: Optional[str]
    nace_code: Optional[str]
    revenue_eur: Optional[float]
    revenue_year: Optional[int]
    revenue_estimated: bool
    employees: Optional[int]
    creation_date: Optional[str]
    city: Optional[str]
    source_url: Optional[str]
    directors: list[DirectorOut] = []
    # M&A signals
    ma_score: int = 0
    ma_signals: list[str] = []
    # DECP public contracts signal
    has_public_infra_contracts: int = 0

    model_config = {"from_attributes": True}


class CompanyOut(CompanySummary):
    registration_number: str
    activity_description: Optional[str]
    address: Optional[str]
    postal_code: Optional[str]
    website: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    scraped_at: Optional[str]

    model_config = {"from_attributes": True}


class PagedResponse(BaseModel):
    items: list
    total: int
    page: int
    page_size: int
    pages: int


class ScrapeRunOut(BaseModel):
    id: int
    scraper: str
    status: str
    companies_added: int
    companies_updated: int
    started_at: Optional[str]
    finished_at: Optional[str]
    error_message: Optional[str]

    model_config = {"from_attributes": True}
