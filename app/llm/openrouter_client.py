"""OpenRouter AI client for Aivory"""
import logging
import httpx
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


class OpenRouterClient:
    """Client for OpenRouter API"""
    
    def __init__(self, api_key: str = None, base_url: str = "https://openrouter.ai/api/v1"):
        self.base_url = base_url
        self.api_key = api_key
        
        if not self.api_key or not self.api_key.strip():
            logger.warning("OPENROUTER_API_KEY not configured - AI features will be unavailable")
        else:
            logger.info("OpenRouter API: Configured")
    
    async def chat_completion(
        self,
        messages: List[OpenRouterMessage],
        model: str,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: float = 60.0
    ) -> str:
        """
        Call OpenRouter chat completion API.
        
        Args:
            messages: List of messages (system + user + assistant)
            model: Model name (e.g., "deepseek/deepseek-chat", "google/gemini-2.0-flash-exp:free")
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            timeout: Request timeout in seconds
        
        Returns:
            Generated text content
        
        Raises:
            ConnectionError: If API is unreachable
            ValueError: If API returns error
        """
        if not self.api_key:
            raise ConnectionError("OpenRouter API key not configured")
        
        url = f"{self.base_url}/chat/completions"
        
        payload = {
            "model": model,
            "messages": [msg.dict() for msg in messages],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://aivory.ai",  # Optional: for rankings
            "X-Title": "Aivory AI Platform"  # Optional: for rankings
        }
        
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                logger.info(f"Calling OpenRouter API with model: {model}")
                response = await client.post(url, json=payload, headers=headers)
                
                if not response.is_success:
                    error_text = response.text
                    logger.error(f"OpenRouter API error: {error_text}")
                    
                    # Check if it's a rate limit error (429)
                    if response.status_code == 429 or "rate-limited" in error_text.lower() or "rate limit" in error_text.lower():
                        raise OpenRouterRateLimitError(f"Rate limit exceeded: {error_text}")
                    
                    raise ValueError(f"OpenRouter API error: {error_text}")
                
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                
                # Log usage if available
                if "usage" in data:
                    usage = data["usage"]
                    logger.info(f"OpenRouter usage - tokens: {usage.get('total_tokens', 0)}")
                
                logger.info("OpenRouter API call successful")
                return content
                
        except httpx.TimeoutException as e:
            logger.error(f"OpenRouter API timeout: {e}")
            raise ConnectionError(f"OpenRouter API timeout: {e}")
        except httpx.RequestError as e:
            logger.error(f"OpenRouter API connection error: {e}")
            raise ConnectionError(f"OpenRouter API connection error: {e}")
        except KeyError as e:
            logger.error(f"Unexpected OpenRouter API response format: {e}")
            raise ValueError(f"Unexpected API response format: {e}")
    
    async def get_models(self) -> List[Dict]:
        """
        Get list of available models from OpenRouter.
        
        Returns:
            List of model dictionaries
        """
        if not self.api_key:
            raise ConnectionError("OpenRouter API key not configured")
        
        url = f"{self.base_url}/models"
        
        headers = {
            "Authorization": f"Bearer {self.api_key}"
        }
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers)
                
                if not response.is_success:
                    raise ValueError(f"Failed to fetch models: {response.text}")
                
                data = response.json()
                return data.get("data", [])
                
        except Exception as e:
            logger.error(f"Error fetching models: {e}")
            raise
