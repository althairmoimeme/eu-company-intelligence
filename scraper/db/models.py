"""SQLAlchemy ORM models."""
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Text, DateTime, Date,
    ForeignKey, UniqueConstraint, JSON, Index
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("country", "registration_number", name="uq_country_reg"),
        Index("ix_companies_country", "country"),
        Index("ix_companies_sector", "sector"),
        Index("ix_companies_revenue", "revenue_eur"),
        Index("ix_companies_employees", "employees"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    country = Column(String(2), nullable=False)          # ISO 2-letter
    registration_number = Column(String, nullable=False)

    # Financial
    revenue_eur = Column(Float, nullable=True)
    revenue_year = Column(Integer, nullable=True)
    revenue_estimated = Column(Boolean, default=False)
    employees = Column(Integer, nullable=True)

    # Classification
    sector = Column(String, nullable=True)               # human label
    nace_code = Column(String, nullable=True)            # e.g. "46.90"
    activity_description = Column(Text, nullable=True)  # objet social

    # Classification (inferred)
    nace_inferred = Column(String, nullable=True)         # inferred from SIC/keywords

    # Identity
    creation_date = Column(String, nullable=True)        # ISO date
    address = Column(Text, nullable=True)
    city = Column(String, nullable=True)
    postal_code = Column(String, nullable=True)
    website = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)

    # Meta
    source_url = Column(String, nullable=True)
    scraped_at = Column(DateTime, default=datetime.utcnow)
    # Enrichment signals
    idcc_code = Column(String, nullable=True)               # IDCC convention collective
    has_public_infra_contracts = Column(Integer, default=0) # DECP : marchés infra critique prouvés

    directors = relationship("Director", back_populates="company",
                             cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "country": self.country,
            "registration_number": self.registration_number,
            "revenue_eur": self.revenue_eur,
            "revenue_year": self.revenue_year,
            "revenue_estimated": self.revenue_estimated,
            "employees": self.employees,
            "sector": self.sector,
            "nace_code": self.nace_code,
            "activity_description": self.activity_description,
            "creation_date": self.creation_date,
            "address": self.address,
            "city": self.city,
            "postal_code": self.postal_code,
            "website": self.website,
            "email": self.email,
            "phone": self.phone,
            "source_url": self.source_url,
            "scraped_at": self.scraped_at.isoformat() if self.scraped_at else None,
            "has_public_infra_contracts": self.has_public_infra_contracts or 0,
            "directors": [d.to_dict() for d in self.directors],
        }


class Director(Base):
    __tablename__ = "directors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"))
    name = Column(String, nullable=False)
    role = Column(String, nullable=True)
    birth_year = Column(Integer, nullable=True)
    nationality = Column(String, nullable=True)
    appointed_at = Column(Date, nullable=True)     # date de prise de poste

    company = relationship("Company", back_populates="directors")

    def to_dict(self):
        today = date.today()
        tenure = None
        if self.appointed_at:
            tenure = (today - self.appointed_at).days // 365
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "birth_year": self.birth_year,
            "age": (today.year - self.birth_year) if self.birth_year else None,
            "nationality": self.nationality,
            "appointed_at": self.appointed_at.isoformat() if self.appointed_at else None,
            "tenure_years": tenure,
        }


class FinancialHistory(Base):
    """Historique financier multi-années d'une entreprise."""
    __tablename__ = "financial_history"
    __table_args__ = (
        UniqueConstraint("company_id", "year", name="uq_fin_company_year"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    year = Column(Integer, nullable=False)
    revenue_eur = Column(Float, nullable=True)        # CA
    operating_income_eur = Column(Float, nullable=True)  # résultat exploitation
    net_income_eur = Column(Float, nullable=True)     # résultat net
    cash_eur = Column(Float, nullable=True)           # trésorerie
    debt_eur = Column(Float, nullable=True)           # dettes financières
    ebitda_eur = Column(Float, nullable=True)
    source = Column(String(50), nullable=True)        # "yfinance" | "pappers_mcp" | "gouv_fr"
    created_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", backref="financial_history")


class BrokerListing(Base):
    """A company listing found on a business-for-sale platform."""
    __tablename__ = "broker_listings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String, nullable=False)          # "cession-pme.fr" | "fusac.fr" | ...
    listing_name = Column(String, nullable=False)    # raw name from broker site
    listing_url = Column(String, nullable=True)
    sector_hint = Column(String, nullable=True)
    region_hint = Column(String, nullable=True)
    price_hint = Column(String, nullable=True)       # "500K€" etc. if available
    matched_company_id = Column(
        Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )
    match_score = Column(Float, nullable=True)       # rapidfuzz ratio 0-100
    scraped_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", backref="broker_listings")


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scraper = Column(String, nullable=False)
    status = Column(String, default="running")   # running|done|failed
    companies_added = Column(Integer, default=0)
    companies_updated = Column(Integer, default=0)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)


class FavoritesList(Base):
    __tablename__ = "favorites_lists"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    investment_thesis = Column(Text, nullable=True)
    filter_snapshot = Column(Text, nullable=True)  # JSON string of filters
    color = Column(String(20), default="blue")  # blue, green, amber, red, purple
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    items = relationship("FavoritesListItem", back_populates="list",
                         cascade="all, delete-orphan", lazy="dynamic")

    def item_count(self):
        return self.items.count()


class FavoritesListItem(Base):
    __tablename__ = "favorites_list_items"

    id = Column(Integer, primary_key=True, index=True)
    list_id = Column(Integer, ForeignKey("favorites_lists.id", ondelete="CASCADE"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    notes = Column(Text, nullable=True)
    status = Column(String(20), default="prospect", nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    # CRM pipeline fields
    contacted_at = Column(DateTime, nullable=True)        # 1er contact effectué
    last_activity_at = Column(DateTime, nullable=True)    # dernière mise à jour
    next_action = Column(Text, nullable=True)             # prochaine action à faire
    next_action_date = Column(Date, nullable=True)        # date de relance
    contact_channel = Column(String(20), default="email") # email / linkedin / phone / meeting

    list = relationship("FavoritesList", back_populates="items")
    company = relationship("Company")

    __table_args__ = (
        UniqueConstraint("list_id", "company_id", name="uq_fav_list_company"),
    )


class FounderIntelligence(Base):
    """Profil dirigeant enrichi pour le deal origination M&A."""
    __tablename__ = "founder_intelligence"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"),
                        unique=True, nullable=False)

    # Identité
    full_name = Column(String, nullable=True)
    current_role = Column(String, nullable=True)
    estimated_age = Column(Integer, nullable=True)
    founder_status = Column(String(30), default="unknown")   # founder/family_successor/hired_manager/unknown
    years_in_role = Column(Integer, nullable=True)

    # Transmission
    children_signal = Column(String(10), default="unknown")          # yes/no/unknown
    children_in_business = Column(String(10), default="unknown")     # yes/no/unknown
    successor_signal = Column(String(30), default="unknown")         # none/possible_internal/likely_family/likely_operational/unknown

    # Profil
    operator_type = Column(String(20), default="unknown")            # builder/operator/patrimonial/disengaged/unknown
    public_visibility = Column(String(10), default="unknown")        # low/medium/high
    relationship_to_company = Column(Text, nullable=True)

    # Signaux vendeurs
    main_why_now_hypothesis = Column(Text, nullable=True)
    seller_signal_strength = Column(String(10), default="unknown")   # low/moderate/high
    seller_signal_reason = Column(Text, nullable=True)

    # Outreach
    recommended_approach_angle = Column(Text, nullable=True)
    avoid_in_outreach = Column(Text, nullable=True)
    approach_hooks = Column(Text, nullable=True)                      # JSON list

    # Contact
    professional_email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    linkedin_url = Column(String, nullable=True)

    # Meta
    confidence_score = Column(Integer, default=0)                    # 0-100
    sources_snapshot = Column(Text, nullable=True)                   # JSON
    enrichment_status = Column(String(20), default="pending")        # pending/running/done/failed
    last_enriched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", backref="founder_intelligence")


class EquansScore(Base):
    """Score de compatibilité M&A Equans pour une société (multi-pays)."""
    __tablename__ = "company_scores_equans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True
    )

    # Score total et sous-scores (0-100 total, max par dimension ci-dessous)
    total_score       = Column(Integer, default=0)   # /100
    sector_score      = Column(Integer, default=0)   # /30 — compatibilité métier PKD/NACE
    revenue_score     = Column(Integer, default=0)   # /20 — CA >= 75M€
    integration_score = Column(Integer, default=0)   # /15 — ing+install+maint
    critical_score    = Column(Integer, default=0)   # /15 — secteurs critiques
    founder_score     = Column(Integer, default=0)   # /10 — fondateur-PME
    longevity_score   = Column(Integer, default=0)   # /10 — ancienneté >= 10 ans

    # Signaux qualitatifs booléens (utiles pour filtres UI)
    has_engineering     = Column(Boolean, default=False)
    has_installation    = Column(Boolean, default=False)
    has_maintenance     = Column(Boolean, default=False)
    has_critical_sectors = Column(Boolean, default=False)
    is_founder_owned    = Column(Boolean, default=False)
    is_european         = Column(Boolean, default=True)   # pays EU/EEA

    # Taille (bracket pour filtres UI)
    revenue_bracket = Column(String(20), nullable=True)   # ex: "75-300M€"

    # Thèse textuelle et raisons (JSON list)
    thesis          = Column(Text, nullable=True)   # ex: "Installation électrique B2B industriel, fondateur ~60 ans"
    match_reasons   = Column(Text, nullable=True)   # JSON array de strings

    scored_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", backref="equans_score")
