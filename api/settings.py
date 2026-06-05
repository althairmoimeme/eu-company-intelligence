from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    PAPPERS_API_KEY: str = ""
    REJESTR_IO_API_KEY: str = ""
    COMPANIES_HOUSE_API_KEY: str = ""
    CBEAPI_KEY: str = ""
    OPENCORPORATES_API_TOKEN: str = ""
    CVR_USERNAME: str = ""
    CVR_PASSWORD: str = ""
    KVK_API_KEY: str = ""
    NORTHDATA_API_KEY: str = ""

    MIN_REVENUE_EUR: float = 75_000_000
    MIN_EMPLOYEES_PROXY: int = 200
    DATABASE_PATH: str = "companies.db"
    CHECKPOINT_DIR: str = "checkpoints"

    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    # Auth — laisser vide pour désactiver
    APP_USERNAME: str = ""
    APP_PASSWORD: str = ""


_settings = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
