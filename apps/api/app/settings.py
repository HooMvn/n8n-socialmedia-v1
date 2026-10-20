from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+asyncpg://engage:engage@db:5432/engage"
    redis_url: str = "redis://redis:6379/0"

    app_secret_key: str = "change-me"
    jwt_issuer: str = "risheh-engage"
    jwt_audience: str = "risheh-engage-api"
    jwt_access_minutes: int = 60

    n8n_base_url: str = "http://n8n:5678"
    n8n_webhook_secret: str = "change-me"
    n8n_publish_webhook_url: str = "http://n8n:5678/webhook/risheh-engage-publish"
    n8n_outbound_webhook_url: str = "http://n8n:5678/webhook/risheh-engage-outbound"

    outbox_batch_size: int = 20
    outbox_max_attempts: int = 8
    outbox_lock_seconds: int = 120
    outbox_poll_seconds: int = 3

    telegram_bot_token: str = ""
    telegram_default_chat_id: str = ""

    # WooCommerce -> Blog -> Notify product pipeline
    woocommerce_webhook_secret: str = "change-me"
    product_auto_publish: bool = True
    product_max_revisions: int = 2
    product_job_max_attempts: int = 5
    n8n_product_webhook_url: str = "http://n8n:5678/webhook/risheh-product"

    ai_provider: str = "openai-compatible"
    ai_api_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    ai_timeout_seconds: float = 30.0

    cors_origins: str = "http://localhost:3000"
    metrics_enabled: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def production_safety(self):
        if self.environment.lower() != "production":
            return self
        unsafe = {"", "change-me", "local-dev-secret-change-me", "local-automation-secret"}
        if self.app_secret_key in unsafe or len(self.app_secret_key) < 32:
            raise ValueError("APP_SECRET_KEY must be a strong production secret")
        if self.n8n_webhook_secret in unsafe or len(self.n8n_webhook_secret) < 32:
            raise ValueError("N8N_WEBHOOK_SECRET must be a strong production secret")
        if self.woocommerce_webhook_secret in unsafe or len(self.woocommerce_webhook_secret) < 16:
            raise ValueError("WOOCOMMERCE_WEBHOOK_SECRET must be a strong shared secret")
        if "localhost" in self.cors_origins or "*" in self.cors_origins:
            raise ValueError("CORS_ORIGINS must be explicit production origins")
        if "@db:" in self.database_url or "engage:engage" in self.database_url:
            raise ValueError("DATABASE_URL must not use default development credentials")
        return self


settings = Settings()
