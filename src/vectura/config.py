"""Environment-driven settings. Every tunable lives here or in the database -- never in code."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Dict, List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VECTURA_", env_file=".env", extra="ignore")

    environment: str = "development"  # development | test | production
    database_url: str = f"sqlite:///{ROOT / 'vectura.db'}"
    log_level: str = "INFO"
    log_json: bool = True

    # security
    auth_enabled: bool = False
    api_keys: Dict[str, Dict[str, str]] = Field(default_factory=dict)  # {"key": {"user": "...", "role": "..."}}
    cors_origins: List[str] = Field(default_factory=list)
    allow_reset: bool = False  # exposes the audit reset endpoint (never in production)

    # settlement behaviour
    money_tolerance: float = 0.005  # amounts closer than this are treated as equal
    fsc_rate_decimals: int = 3
    default_timezone: str = "UTC"  # applied to naive timestamps that carry no zone
    conflict_tolerance_minutes: int = 5  # sources closer than this are considered in agreement
    prevention_warn_minutes: int = 30  # warn this long before free time runs out
    significant_writeoff_amount: float = 100.0  # write-offs above this need the large write-off permission
    dev_role: str = "admin"  # role granted when auth is disabled (local development only)

    document_dir: Path = ROOT / "var" / "documents"
    max_document_bytes: int = 10_000_000
    outbox_max_attempts: int = 5
    outbox_timeout_s: float = 10.0
    repeat_cause_threshold: int = 2  # a cause seen this many times at one location counts as repeated

    # EIA (free API key from https://www.eia.gov/opendata/)
    eia_api_key: str = ""
    eia_base_url: str = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
    eia_series: str = "EMD_EPD2D_PTE_NUS_DPG"
    eia_timeout_s: float = 90.0  # the EIA API can take 20+ seconds to answer
    eia_history_weeks: int = 104

    # data & UI
    seed_dir: Path = ROOT / "data" / "seed"
    frontend_dir: Path = ROOT / "frontend"
    ui_day_start_minutes: int = 360
    ui_day_end_minutes: int = 1320
    ui_slider_step_minutes: int = 5
    default_page_size: int = 50
    max_page_size: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
