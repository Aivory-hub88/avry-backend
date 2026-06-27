"""OpenRouter AI client for Aivory — with key rotation support (requests-based)"""
import os
import logging
import requests
from typing import List, Dict, Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class OpenRouterMessage(BaseModel):
    """Message format for OpenRouter API"""
    role: str  # "system", "user", or "assistant"
    content: str


class OpenRouterRateLimitError(Exception):
    """Raised when OpenRouter API returns rate limit error"""
    pass


def _load_api_keys() -> List[str]:
    """
    Load OpenRouter API keys from environment.
    Supports rotation via OPENROUTER_API_KEY, OPENROUTER_API_KEY_2, OPENROUTER_API_KEY_3.
    """
    keys = []
    primary = os.getenv("OPENROUTER_API_KEY", "").strip()
    if primary:
        keys.append(primary)
    key2 = os.getenv("OPENROUTER_API_KEY_2", "").strip()
    if key2:
        keys.append(key2)
    key3 = os.getenv("OPENROUTER_API_KEY_3", "").strip()
    if key3:
        keys.append(key3)
    return keys


class OpenRouterClient:
    """Client for OpenRouter API with round-robin key rotation."""

    def __init__(self, api_key: str = None, base_url: str = "https://openrouter.ai/api/v1"):
        self.base_url = base_url

        # Build key pool: explicit key param > env vars
        if api_key and api_key.strip():
            self._keys = [api_key.strip()]
        else:
            self._keys = _load_api_keys()

        self._key_index = 0

        if not self._keys:
            logger.warning("OPENROUTER_API_KEY not configured - AI features will be unavailable")
        else:
            count = len(self._keys)
            status = "enabled" if count > 1 else "disabled"
            logger.info(f"OpenRouter API: Configured with {count} key(s) (rotation {status})")

    @property
    def api_key(self) -> Optional[str]:
        """Current active key."""
        if not self._keys:
            return None
        return self._keys[self._key_index % len(self._keys)]

    def _rotate_key(self) -> Optional[str]:
        """Advance to next key in the pool."""
        if len(self._keys) <= 1:
            return None
        self._key_index = (self._key_index + 1) % len(self._keys)
        logger.info(f"OpenRouter: Rotated to key #{self._key_index + 1}/{len(self._keys)}")
        return self._keys[self._key_index]

    async def chat_completion(
        self,
        messages: List[OpenRouterMessage],
        model: str,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: float = 60.0
    ) -> str:
        """
        Call OpenRouter chat completion API with automatic key rotation on rate limit.
        Uses requests (sync) internally — safe for FastAPI with small payloads.
        """
        if not self._keys:
            raise ConnectionError("OpenRouter API key not configured")

        url = f"{self.base_url}/chat/completions"

        payload = {
            "model": model,
            "messages": [msg.dict() for msg in messages],
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        attempts = len(self._keys)
        last_error = None

        for attempt in range(attempts):
            current_key = self.api_key
            key_label = f"key #{(self._key_index % len(self._keys)) + 1}"

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {current_key}",
                "HTTP-Referer": "https://aivory.ai",
                "X-Title": "Aivory AI Platform"
            }

            try:
                logger.info(f"Calling OpenRouter API with model: {model} ({key_label})")
                response = requests.post(url, json=payload, headers=headers, timeout=timeout)

                if response.status_code == 200:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]

                    if "usage" in data:
                        usage = data["usage"]
                        logger.info(f"OpenRouter usage - tokens: {usage.get('total_tokens', 0)}")

                    logger.info(f"OpenRouter API call successful ({key_label})")
                    return content

                error_text = response.text
                logger.error(f"OpenRouter API error ({key_label}): {response.status_code} - {error_text[:200]}")

                # Rate limit — try next key
                if response.status_code == 429 or "rate-limited" in error_text.lower() or "rate limit" in error_text.lower():
                    last_error = OpenRouterRateLimitError(f"Rate limit on {key_label}")
                    logger.warning(f"Rate limit hit on {key_label}, rotating...")
                    if self._rotate_key() is None:
                        raise last_error
                    continue

                # Other API error — don't rotate
                raise ValueError(f"OpenRouter API error: {error_text[:300]}")

            except requests.Timeout as e:
                logger.error(f"OpenRouter API timeout: {e}")
                raise ConnectionError(f"OpenRouter API timeout: {e}")
            except requests.RequestException as e:
                logger.error(f"OpenRouter API connection error: {e}")
                raise ConnectionError(f"OpenRouter API connection error: {e}")
            except (OpenRouterRateLimitError, ValueError, ConnectionError):
                raise
            except KeyError as e:
                logger.error(f"Unexpected OpenRouter API response format: {e}")
                raise ValueError(f"Unexpected API response format: {e}")

        # All keys exhausted
        raise last_error or OpenRouterRateLimitError("All API keys rate-limited")

    async def get_models(self) -> List[Dict]:
        """Get list of available models from OpenRouter."""
        if not self._keys:
            raise ConnectionError("OpenRouter API key not configured")

        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            response = requests.get(url, headers=headers, timeout=30.0)
            if response.status_code != 200:
                raise ValueError(f"Failed to fetch models: {response.text}")
            data = response.json()
            return data.get("data", [])
        except Exception as e:
            logger.error(f"Error fetching models: {e}")
            raise
