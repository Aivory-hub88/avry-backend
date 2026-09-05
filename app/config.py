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

    # Discord deployable-agent configuration. One shared bot for every agent
    # type (unlike Telegram's per-agent-type option) — see discord_service.py.
    discord_bot_token: Optional[str] = None
    discord_application_id: Optional[str] = None
    discord_link_token_ttl_minutes: int = 10

    # Slack deployable-agent configuration
    slack_client_id: Optional[str] = None
    slack_client_secret: Optional[str] = None
    slack_signing_secret: Optional[str] = None
    # Where the OAuth callback sends the user after a successful install
    slack_post_install_redirect: str = "https://aivory.id/dashboard/agents?slack=connected"

    # Outbound account/security mail (password reset, account-cleanup warnings).
    # The container has always received these via docker-compose, but they were
    # never declared here — app/services/email_service.py's settings.smtp_host
    # raised AttributeError on every send, so password-reset mail has been
    # silently broken since it shipped.
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from_email: Optional[str] = None
    auth_from_email: Optional[str] = None
    password_reset_url_base: str = "https://aivory.uk/reset-password"
    admin_password_reset_url_base: str = "https://admin.aivory.id/admin/reset-password"
    password_reset_ttl_minutes: int = 60

    # Account cleanup (see app/services/account_cleanup.py). Off by default —
    # the poller still runs and logs what it would warn/delete, but sends no
    # mail and deletes nothing until this is explicitly turned on.
    account_cleanup_enabled: bool = False
    account_cleanup_interval_seconds: int = 1800

    # CORS configuration
    cors_origins: list[str] = ["*"]

    # ADR-006 Part B: AES-256-GCM key for encrypting tenant custom MCP server
    # auth-header values at rest (product.tenant_custom_mcp_servers). Its own
    # dedicated env var — deliberately decoupled from avry-careers'
    # ENCRYPTION_KEY (PII/CV files) and any other service's key, so rotating
    # one never touches the other.
    mcp_server_auth_encryption_key: Optional[str] = None

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
