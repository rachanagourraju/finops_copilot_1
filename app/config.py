from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINANCE_", extra="ignore")

    gcp_project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    bigquery_analytics_dataset: str = Field(pattern=r"^[A-Za-z0-9_]{1,1024}$")
    bigquery_location: str = Field(min_length=1)
    vertex_location: str = Field(min_length=1)
    gemini_model: str = Field(min_length=1)
    max_bytes_billed: int = Field(default=104_857_600, ge=10_485_760)
    max_rows: int = Field(default=100, ge=1, le=250)
    query_timeout_seconds: int = Field(default=25, ge=5, le=120)

    @property
    def view(self) -> str:
        return (
            f"{self.gcp_project_id}."
            f"{self.bigquery_analytics_dataset}.fct_monthly_spend"
        )


def load_settings() -> Settings:
    env_file = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_file, override=False)
    return Settings()