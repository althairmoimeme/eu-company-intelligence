"""Authentification HTTP Basic optionnelle.

Si APP_USERNAME et APP_PASSWORD sont définis dans .env, toutes les routes API
et le frontend nécessitent ces identifiants.

Si vides → pas d'auth (mode dev local).
"""
import secrets
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from .settings import get_settings

security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)):
    """Dependency FastAPI — vérifie les credentials si auth activée."""
    settings = get_settings()

    # Auth désactivée si pas de credentials configurés
    if not settings.APP_USERNAME or not settings.APP_PASSWORD:
        return True

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    correct_username = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.APP_USERNAME.encode("utf-8"),
    )
    correct_password = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.APP_PASSWORD.encode("utf-8"),
    )

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Identifiants incorrects",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True
