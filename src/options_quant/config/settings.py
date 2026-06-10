"""Typed application settings placeholders."""

from pydantic import BaseModel, Field


class AppSettings(BaseModel):
    """Top-level application settings."""

    environment: str = Field(default="local")
    log_level: str = Field(default="INFO")


class DataSettings(BaseModel):
    """Data access settings."""

    provider: str = Field(default="example")
    cache_enabled: bool = Field(default=False)


class BacktestSettings(BaseModel):
    """Backtest runtime settings."""

    initial_cash: float = Field(default=100_000)
    commission_per_contract: float = Field(default=0.65)


class Settings(BaseModel):
    """Root settings object."""

    app: AppSettings = Field(default_factory=AppSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
