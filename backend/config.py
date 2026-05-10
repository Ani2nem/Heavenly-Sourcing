from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    cors_allowed_origins: str = "http://localhost:5173"

    google_places_api_key: str = ""
    jina_api_key: str = ""
    usda_api_key: str = ""
    ams_api_key: str = ""

    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    imap_server: str = "imap.gmail.com"
    imap_user: str = ""
    imap_password: str = ""

    openai_api_key: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",")]


settings = Settings()
