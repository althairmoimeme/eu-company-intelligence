"""Enrichissement ciblé Equans — Pologne via rejestr.io Business.

Stratégie :
1. Sélectionne les sociétés PL avec codes NACE Equans dans la DB
2. krs-rozdzialy/ogolny (gratuit) → PKD complets, actionnaires+âge, dirigeants
3. krs-dokumenty + ?format=json (0.5 PLN/société) → CA réel
4. Filtre CA >= 75M€ (≈ 326M PLN) et score Equans
5. Met à jour la DB et retourne un rapport M&A trié

Coût estimé : ~0.5 PLN × nb_cibles (uniquement sur les compatibles Equans)
"""
import asyncio
import logging
from datetime import date
from typing import Optional

import httpx
from sqlalchemy import select, update, text

from ..db.session import get_session_factory
from ..db.models import Company, Director

logger = logging.getLogger(__name__)

REJESTR_BASE = "https://rejestr.io/api/v2"
PLN_TO_EUR = 0.23
MIN_REVENUE_PLN = 75_000_000 / PLN_TO_EUR   # ≈ 326M PLN

REQUESTS_PER_SECOND = 0.4  # 1 req/2.5s — conservateur

_status: dict = {
    "running": False,
    "processed": 0,
    "enriched": 0,
    "total": 0,
    "error": None,
}


def get_pl_equans_status() -> dict:
    return _status.copy()

# ─── Codes PKD Equans ─────────────────────────────────────────────────────────
EQUANS_PKD_PRIMARY = {
    "43.21", "43.22", "43.29",  # Installations électriques / sanitaires / autres
    "33.20",                     # Installation machines industrielles
    "71.12",                     # Ingénierie et conseil technique
    "26.51",                     # Instrumentation / mesure
    "27.33",                     # Appareillage basse tension
    "28.25",                     # Équipements réfrigération / clim
    "35.30",                     # Production/distribution chaleur/froid
    "43.99",                     # Autres travaux spécialisés
    "81.10",                     # Facility management intégré
}

EQUANS_PKD_EXTENDED = EQUANS_PKD_PRIMARY | {
    "33.12", "33.13", "33.14",  # Réparation machines / électronique / électrique
    "27.12",                     # Tableau HTA / appareillage
    "61.10",                     # Télécommunications
    "80.20",                     # Sécurité / surveillance
    "74.90",                     # Conseil technique spécialisé
}

EQUANS_KEYWORDS_PL = [
    "elektrycznych", "elektryczne", "elektrotechni",
    "klimatyzacj", "wentylacj", "ogrzewani", "chłodnictw",
    "automatyk", "sterowni", "BMS", "SCADA", "AKPiA",
    "instalacj", "instalatorstw", "montaż", "wykonyw",
    "szafy sterownicze", "rozdzielnic",
    "facility management", "utrzymanie techniczne", "utrzymanie ruchu",
    "serwis techniczny", "maintenance",
    "ochrona przeciwpożarowa", "przeciwpożar",
    "data center", "centrum danych", "cleanroom", "pomieszczenia czyste",
    "efektywność energetyczna", "fotowoltaik",
    "inżynieria", "techniczne doradztwo",
    "maszyn przemysłowych", "urządzeń przemysłowych",
    "instrumentacj", "pomiarow",
]


def _pkd_symbol(obj: dict) -> str:
    sym = obj.get("symbol", [])
    if isinstance(sym, list):
        return ".".join(str(s) for s in sym if str(s).strip())
    return str(sym)


def _is_equans_pkd(symbol: str, desc: str = "") -> bool:
    clean = symbol.replace(".", "").replace(" ", "").upper()
    for code in EQUANS_PKD_EXTENDED:
        if clean.startswith(code.replace(".", "").upper()):
            return True
    desc_lower = (desc or "").lower()
    for kw in EQUANS_KEYWORDS_PL:
        if kw.lower() in desc_lower:
            return True
    return False


def _extract_revenue(doc_json: dict) -> Optional[float]:
    """Extrait 'Przychody netto ze sprzedaży' (nœud A) depuis un RZiS JSON."""
    def walk(node):
        if not node:
            return None
        # Nœud A = revenus nets de ventes
        if node.get("nazwa_wezla") in ("A", "I") and node.get("pln_rok_obrotowy_biezacy"):
            try:
                return float(str(node["pln_rok_obrotowy_biezacy"]).replace(",", "."))
            except Exception:
                pass
        # Chercher "Przychody netto ze sprzedaży" dans le label
        label = (node.get("etykieta") or "").lower()
        if "przychody netto ze sprzedaży" in label and node.get("pln_rok_obrotowy_biezacy"):
            try:
                return float(str(node["pln_rok_obrotowy_biezacy"]).replace(",", "."))
            except Exception:
                pass
        for child in (node.get("podobiekty") or []):
            result = walk(child)
            if result:
                return result
        return None

    return walk(doc_json.get("zawartosc", {}))


def _parse_ogolny(data: dict) -> dict:
    """Parse le chapitre ogolny → dict structuré."""
    out = {
        "nip": None, "pkd_primary": None, "pkd_primary_desc": None,
        "pkd_all": [], "shareholders": [], "directors": [],
        "capital_pln": None, "city": None, "postal_code": None,
        "address": None, "creation_date": None,
    }

    out["nip"] = (data.get("nip") or {}).get("_wartosc")

    adr = (data.get("adres_znormalizowany") or data.get("adres") or {}).get("_wartosc") or {}
    out["city"] = adr.get("miasto")
    out["postal_code"] = adr.get("kod_pocztowy")
    if adr.get("ulica"):
        out["address"] = f"{adr.get('ulica','')} {adr.get('nr_domu','')}".strip()

    cap = (data.get("wysokosc_kapitalu_zakladowego") or {}).get("_wartosc") or {}
    if cap.get("kwota"):
        try:
            out["capital_pln"] = float(str(cap["kwota"]).replace(",", "."))
        except Exception:
            pass

    reg = data.get("krs_rejestry") or {}
    out["creation_date"] = (reg.get("rejestr_przedsiebiorcow_data_wpisu")
                            or reg.get("rejestr_stowarzyszen_data_wpisu"))

    # PKD principal
    prim = data.get("przedmiot_przewazajacej_dzialalnosci_przedsiebiorcy") or {}
    for _, obj in (prim.get("_obiekty") or {}).items():
        v = obj.get("_wartosc") or {}
        sym = _pkd_symbol(v)
        if sym:
            out["pkd_primary"] = sym
            out["pkd_primary_desc"] = v.get("opis")
            out["pkd_all"].append(v)
            break

    # PKD secondaires
    sec = data.get("przedmiot_pozostalej_dzialalnosci_przedsiebiorcy") or {}
    for _, obj in (sec.get("_obiekty") or {}).items():
        v = obj.get("_wartosc") or {}
        if _pkd_symbol(v):
            out["pkd_all"].append(v)

    # Actionnaires
    for _, obj in (( data.get("dane_wspolnikow") or {}).get("_obiekty") or {}).items():
        person = (obj.get("person") or {}).get("_wartosc") or {}
        name = person.get("nazwa") or f"{person.get('imie','')} {person.get('nazwisko','')}".strip()
        birth = person.get("data_urodzenia")
        birth_year = int(str(birth)[:4]) if birth else None
        if name:
            out["shareholders"].append({"person_name": name, "birth_year": birth_year})

    # Dirigeants
    organ = data.get("organ_reprezentacji") or {}
    for _, org_obj in (organ.get("_obiekty") or {}).items():
        dane = org_obj.get("dane_osob") or {}
        for _, p_obj in (dane.get("_obiekty") or {}).items():
            person = (p_obj.get("person") or {}).get("_wartosc") or {}
            name = person.get("nazwa") or f"{person.get('imie','')} {person.get('nazwisko','')}".strip()
            role_v = (p_obj.get("funkcja_w_organie") or {}).get("_wartosc") or {}
            role = role_v.get("nazwa") if isinstance(role_v, dict) else str(role_v)
            birth = person.get("data_urodzenia")
            birth_year = int(str(birth)[:4]) if birth else None
            if name:
                out["directors"].append({"name": name, "role": role, "birth_year": birth_year})

    return out


def _score_equans(pkd_primary: str, pkd_all: list, shareholders: list,
                  directors: list, capital_pln: float, creation_date: str,
                  revenue_eur: float) -> tuple[int, list[str]]:
    score = 0
    reasons = []

    # 30 pts : compatibilité métier PKD principal
    clean = pkd_primary.replace(".", "").upper() if pkd_primary else ""
    for code in EQUANS_PKD_PRIMARY:
        if clean.startswith(code.replace(".", "").upper()):
            score += 30
            reasons.append(f"PKD principal {pkd_primary} = cœur métier Equans")
            break
    else:
        for pkd in pkd_all:
            if _is_equans_pkd(_pkd_symbol(pkd), pkd.get("opis", "")):
                score += 20
                reasons.append(f"PKD secondaire {_pkd_symbol(pkd)} compatible Equans")
                break

    # 20 pts : CA >= 75M€
    if revenue_eur and revenue_eur >= 75_000_000:
        score += 20
        reasons.append(f"CA {revenue_eur/1e6:.0f}M€ ≥ 75M€")
    elif revenue_eur and revenue_eur >= 20_000_000:
        score += 10
        reasons.append(f"CA {revenue_eur/1e6:.0f}M€ (20-75M€)")

    # 15 pts : ingénierie + installation + maintenance
    all_descs = " ".join(p.get("opis", "") for p in pkd_all).lower()
    pts = 0
    if any(k in all_descs for k in ["instalacj", "montaż", "wykonyw"]): pts += 5
    if any(k in all_descs for k in ["inżynier", "projektow", "doradztwo technicz"]): pts += 5
    if any(k in all_descs for k in ["serwis", "utrzymanie", "konserwacj", "maintenance"]): pts += 5
    if pts:
        score += pts
        reasons.append(f"ingénierie+installation+maintenance (+{pts}pts)")

    # 15 pts : secteurs critiques
    if any(k in all_descs for k in [
        "farmac", "mikroelektron", "data center", "czyst", "szpital",
        "przemysłow", "energetyk", "cleanroom", "krytyczn"
    ]):
        score += 15
        reasons.append("exposition secteurs critiques")

    # 10 pts : propriétaire unique / fondateur-PME
    if len(shareholders) == 1 or (
        len(shareholders) <= 3 and all(s.get("person_name") for s in shareholders)
    ):
        score += 10
        reasons.append("fondateur-PME (propriétaire personne physique)")

    # 10 pts : ancienneté >= 10 ans
    if creation_date:
        try:
            created = date.fromisoformat(str(creation_date)[:10])
            age = (date.today() - created).days / 365
            if age >= 10:
                score += 10
                reasons.append(f"ancienneté {int(age)} ans")
        except Exception:
            pass

    return min(score, 100), reasons


async def enrich_pl_equans(
    db_path: str,
    api_key: str,
    limit: int = 700,
    min_revenue_eur: float = 5_000_000,
    fetch_financials: bool = True,
    min_equans_score: int = 0,
) -> dict:
    """Enrichit et score les cibles Equans PL.

    1. ogolny (gratuit) → PKD, actionnaires, dirigeants
    2. krs-dokumenty + format=json (0.5 PLN) → CA réel
    3. Score Equans 0-100
    4. Mise à jour DB + rapport top cibles

    Args:
        db_path: chemin DB SQLite
        api_key: clé API rejestr.io Business
        limit: max de sociétés à traiter
        min_revenue_eur: seuil de CA minimum pour garder une société
        fetch_financials: si True, appelle les documents financiers (0.5 PLN/sté)
        min_equans_score: score Equans minimum (filtrage optionnel)
    """
    global _status
    _status = {"running": True, "processed": 0, "enriched": 0, "total": 0, "error": None}
    factory = get_session_factory(db_path)
    processed = enriched = skipped = errors = 0
    top_targets = []

    # Codes NACE Equans — vérifier nace_code (officiel) ET nace_inferred (inféré)
    _equans_nace = [
        "43.21", "43.22", "43.29", "33.20", "71.12", "43.99",
        "26.51", "27.33", "28.25", "35.30", "33.12", "33.14",
        "27.12", "81.10",
    ]
    nace_filter = " OR ".join(
        [f"nace_code LIKE '{n}%'" for n in _equans_nace] +
        [f"nace_inferred LIKE '{n}%'" for n in _equans_nace]
    )

    async with factory() as session:
        rows = (await session.execute(
            text(f"""
                SELECT id, registration_number, name, creation_date, employees
                FROM companies
                WHERE country = 'PL'
                  AND ({nace_filter})
                  AND revenue_eur IS NULL
                  AND length(registration_number) <= 10
                  AND registration_number GLOB '[0-9]*'
                ORDER BY employees DESC NULLS LAST, id
                LIMIT :lim
            """),
            {"lim": limit}
        )).fetchall()

    logger.info(f"[PL-EQ] {len(rows)} sociétés NACE Equans à traiter")
    _status["total"] = len(rows)

    async with httpx.AsyncClient(
        timeout=20,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as client:

        for company_id, krs, name, creation_date, employees in rows:
            krs_pad = str(krs).zfill(10)
            processed += 1
            _status["processed"] = processed

            try:
                # ── Étape 1 : ogolny (gratuit) ──────────────────────────────
                r_org = await client.get(
                    f"{REJESTR_BASE}/org/{krs_pad}/krs-rozdzialy/ogolny"
                )
                await asyncio.sleep(1 / REQUESTS_PER_SECOND)

                if r_org.status_code != 200:
                    skipped += 1
                    continue

                body = r_org.json()
                # Détecter HTTP 200 avec erreur JSON (rejestr.io retourne {"kod": 403, ...})
                if isinstance(body, dict) and body.get("kod") in (403, 401, 429):
                    api_code = body.get("kod")
                    logger.error(f"[PL-EQ] rejestr.io erreur {api_code}: {body.get('info', '')} — arrêt")
                    _status["error"] = f"rejestr.io {api_code}: {body.get('info', '')}"
                    break

                parsed = _parse_ogolny(body)

                # Vérifier compatibilité Equans via PKD complets
                equans_ok = False
                pkd_clean = (parsed["pkd_primary"] or "").replace(".", "").upper()
                for code in EQUANS_PKD_PRIMARY:
                    if pkd_clean.startswith(code.replace(".", "").upper()):
                        equans_ok = True
                        break
                if not equans_ok:
                    for pkd in parsed["pkd_all"]:
                        if _is_equans_pkd(_pkd_symbol(pkd), pkd.get("opis", "")):
                            equans_ok = True
                            break

                if not equans_ok:
                    skipped += 1
                    continue

                # ── Étape 2 : documents financiers (0.5 PLN) ─────────────
                revenue_eur = None
                revenue_year = None

                if fetch_financials:
                    r_docs = await client.get(
                        f"{REJESTR_BASE}/org/{krs_pad}/krs-dokumenty"
                    )
                    await asyncio.sleep(1 / REQUESTS_PER_SECOND)

                    if r_docs.status_code == 200:
                        docs_list = r_docs.json()
                        doc_id = None
                        doc_year = None

                        for period in sorted(docs_list,
                                             key=lambda x: x.get("data_koniec", ""),
                                             reverse=True):
                            for doc in period.get("dokumenty", []):
                                if doc.get("czy_ma_json") and (
                                    "zysk" in doc.get("nazwa", "").lower() or
                                    "przychod" in doc.get("nazwa", "").lower()
                                ):
                                    doc_id = doc["id"]
                                    doc_year = int(str(period.get("data_koniec", "0"))[:4])
                                    break
                            if doc_id:
                                break

                        if doc_id:
                            r_fin = await client.get(
                                f"{REJESTR_BASE}/org/{krs_pad}/krs-dokumenty/{doc_id}",
                                params={"format": "json"},
                            )
                            await asyncio.sleep(1 / REQUESTS_PER_SECOND)

                            if r_fin.status_code == 200:
                                rev_pln = _extract_revenue(r_fin.json())
                                if rev_pln and rev_pln > 0:
                                    revenue_eur = round(rev_pln * PLN_TO_EUR, 0)
                                    revenue_year = doc_year

                # Filtre CA minimum
                if revenue_eur is not None and revenue_eur < min_revenue_eur:
                    skipped += 1
                    continue

                # ── Score Equans ─────────────────────────────────────────
                score, reasons = _score_equans(
                    pkd_primary=parsed["pkd_primary"] or "",
                    pkd_all=parsed["pkd_all"],
                    shareholders=parsed["shareholders"],
                    directors=parsed["directors"],
                    capital_pln=parsed["capital_pln"] or 0,
                    creation_date=parsed["creation_date"] or creation_date,
                    revenue_eur=revenue_eur or 0,
                )

                # ── Mise à jour DB ────────────────────────────────────────
                async with factory() as session:
                    async with session.begin():
                        upd = {}
                        if parsed["city"]:
                            upd["city"] = parsed["city"]
                        if parsed["postal_code"]:
                            upd["postal_code"] = parsed["postal_code"]
                        if parsed["address"]:
                            upd["address"] = parsed["address"]
                        if revenue_eur:
                            upd["revenue_eur"] = revenue_eur
                        if revenue_year:
                            upd["revenue_year"] = revenue_year
                        if parsed["pkd_primary_desc"]:
                            upd["activity_description"] = parsed["pkd_primary_desc"]
                        if upd:
                            await session.execute(
                                text("UPDATE companies SET " +
                                     ", ".join(f"{k}=:{k}" for k in upd) +
                                     " WHERE id=:id"),
                                {**upd, "id": company_id}
                            )

                        if parsed["directors"]:
                            await session.execute(
                                text("DELETE FROM directors WHERE company_id=:id"),
                                {"id": company_id}
                            )
                            for d in parsed["directors"]:
                                await session.execute(
                                    text("""INSERT INTO directors
                                         (company_id, name, role, birth_year)
                                         VALUES (:cid, :name, :role, :by)"""),
                                    {"cid": company_id, "name": d["name"],
                                     "role": d["role"], "by": d["birth_year"]}
                                )

                enriched += 1
                _status["enriched"] = enriched

                # Âge fondateur
                founder_age = None
                for s in parsed["shareholders"]:
                    if s.get("birth_year"):
                        founder_age = date.today().year - s["birth_year"]
                        break

                top_targets.append({
                    "krs": krs_pad,
                    "name": name,
                    "city": parsed["city"],
                    "pkd_primary": parsed["pkd_primary"],
                    "pkd_desc": parsed["pkd_primary_desc"],
                    "revenue_eur": revenue_eur,
                    "revenue_year": revenue_year,
                    "employees": employees,
                    "capital_pln": parsed["capital_pln"],
                    "shareholders_count": len(parsed["shareholders"]),
                    "founder_age": founder_age,
                    "score": score,
                    "reasons": ", ".join(reasons),
                    "directors": [d["name"] for d in parsed["directors"][:2]],
                })

                if enriched % 20 == 0:
                    logger.info(f"[PL-EQ] {enriched}/{len(rows)} traitées | "
                                f"{len([t for t in top_targets if (t['revenue_eur'] or 0) >= 75_000_000])} ≥75M€")

            except Exception as e:
                logger.warning(f"[PL-EQ] Erreur {krs}: {e}")
                errors += 1
                await asyncio.sleep(2)

    # Trier par score puis CA
    top_targets.sort(key=lambda x: (x["score"], x["revenue_eur"] or 0), reverse=True)

    big = [t for t in top_targets if (t["revenue_eur"] or 0) >= 75_000_000]
    medium = [t for t in top_targets if 20_000_000 <= (t["revenue_eur"] or 0) < 75_000_000]

    logger.info(f"[PL-EQ] === RÉSULTATS EQUANS POLOGNE ===")
    logger.info(f"[PL-EQ] Cibles ≥75M€ : {len(big)}")
    logger.info(f"[PL-EQ] Cibles 20-75M€ : {len(medium)}")
    logger.info(f"[PL-EQ] Traité: {processed} | Enrichi: {enriched} | Ignoré: {skipped} | Erreurs: {errors}")
    logger.info("[PL-EQ] TOP CIBLES ≥75M€ :")
    for t in big[:20]:
        logger.info(
            f"  [{t['score']:3d}] {t['name'][:45]:45s} | "
            f"CA: {(t['revenue_eur'] or 0)/1e6:.0f}M€ | "
            f"PKD: {t['pkd_primary']} | {t['city']} | "
            f"Fondateur ~{t['founder_age'] or '?'} ans"
        )

    _status["running"] = False
    _status["enriched"] = enriched

    return {
        "processed": processed,
        "enriched": enriched,
        "skipped": skipped,
        "errors": errors,
        "targets_75m_plus": len(big),
        "targets_20_75m": len(medium),
        "top_targets": top_targets[:100],
    }
