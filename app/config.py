from pydantic_settings import BaseSettings, SettingsConfigDict # Import erweitert

class Settings(BaseSettings):
    DATABASE_URL: str
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 525600 # 1 Jahr (365 * 24 * 60)

    # Stripe Integration
    STRIPE_SECRET_KEY: str
    STRIPE_WEBHOOK_SECRET: str
    STRIPE_PRICE_ID_STARTER_MONTHLY: str
    STRIPE_PRICE_ID_PRO_MONTHLY: str
    STRIPE_PRICE_ID_ENTERPRISE_MONTHLY: str
    
    STRIPE_PRICE_ID_STARTER_YEARLY: str
    STRIPE_PRICE_ID_PRO_YEARLY: str
    STRIPE_PRICE_ID_ENTERPRISE_YEARLY: str

    CRON_SECRET: str

    # Neue Schreibweise für Pydantic v2
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()