"""Configuration module for Aivory application"""
import os
import sys
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import field_validator, ValidationError, ConfigDict
from dotenv import load_dotenv

# Load unified .env from project root (covers all services)
load_dotenv(".env.local")  # legacy — takes precedence if present
load_dotenv(".env")        # unified config


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # Allow extra fields to be ignored (for shared .env across services)
    model_config = ConfigDict(extra='ignore')
    
    # Server configuration
    app_name: str = "Aivory AI Readiness Platform"
    app_version: str = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 8081
    
    # LLM configuration (Ollama - legacy)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "mistralai/Mistral-7B-Instruct"
    llm_timeout: float = 5.0
    llm_max_tokens: int = 500
    llm_temperature: float = 0.7
    
    # OpenRouter AI configuration (PRIMARY)
    openrouter_api_key: Optional[str] = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    
    # Midtrans Payment Gateway configuration
    midtrans_server_key: Optional[str] = None
    midtrans_client_key: Optional[str] = None
    midtrans_is_production: bool = False
    
    # n8n Integration configuration
    n8n_base_url: str = "http://43.156.108.96:5678"
    n8n_timeout: float = 10.0
    n8n_max_retries: int = 3

    # Telegram deployable-agent configuration
    telegram_bot_token: Optional[str] = None
    telegram_bot_username: Optional[str] = None  # without @, used to build t.me deep links
    telegram_webhook_secret: Optional[str] = None  # sent back by Telegram as X-Telegram-Bot-Api-Secret-Token
    telegram_link_token_ttl_minutes: int = 10
    # Optional downstream gateway that answers agent messages (e.g. zeroclaw bridge).
    # When unset, bound chats get a static acknowledgement reply.
    telegram_agent_gateway_url: Optional[str] = None
    
    # CORS configuration
    cors_origins: list[str] = ["*"]
    
    def validate_paid_tier_config(self) -> None:
        """
        Validate that required configuration for paid tiers is present.
        This should be called before processing any paid diagnostic requests.
        """
        if not self.openrouter_api_key or not self.openrouter_api_key.strip():
            raise ValueError(
                "OPENROUTER_API_KEY is required for paid diagnostic tiers. "
                "Please set it in .env.local file."
            )


# Global settings instance
try:
    settings = Settings()
    print(f"[OK] Configuration loaded successfully")
    print(f"  - App: {settings.app_name} v{settings.app_version}")
    print(f"  - OpenRouter API: {'Configured' if settings.openrouter_api_key else 'Not configured'}")
except ValidationError as e:
    print(f"[ERROR] Configuration validation failed:")
    for error in e.errors():
        field = '.'.join(str(loc) for loc in error['loc'])
        print(f"  - {field}: {error['msg']}")
    print("\nPlease check your .env.local file and ensure all required variables are set correctly.")
    sys.exit(1)
except Exception as e:
    print(f"[ERROR] Failed to load configuration: {str(e)}")
    sys.exit(1)
