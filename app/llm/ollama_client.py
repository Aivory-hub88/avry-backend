"""Ollama LLM client with timeout and error handling"""
import httpx
import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Base exception for LLM-related errors"""
    pass


class OllamaClient:
    """Client for interacting with Ollama LLM service"""
    
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None
    ):
        self.base_url = base_url or settings.ollama_base_url
        self.model = model or settings.ollama_model
        self.timeout = timeout or settings.llm_timeout
    
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        timeout: Optional[float] = None
    ) -> str:
        """
        Generate a response from Ollama.
        
        Args:
            prompt: The prompt to send to the LLM
            system_prompt: Optional system prompt for context
            timeout: Optional timeout override (defaults to config)
        
        Returns:
            Generated text response
        
        Raises:
            TimeoutError: If generation exceeds timeout
            ConnectionError: If LLM service unavailable
            LLMError: If generation fails
        """
        request_timeout = timeout or self.timeout
        
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                payload = {
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": settings.llm_temperature,
                        "num_predict": settings.llm_max_tokens
                    }
                }
                
                if system_prompt:
                    payload["system"] = system_prompt
                
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json=payload
                )
                response.raise_for_status()
                return response.json()["response"]
                
        except httpx.TimeoutException as e:
            logger.warning(f"LLM request timed out after {request_timeout}s: {e}")
            raise TimeoutError(f"LLM request timed out after {request_timeout}s")
        except httpx.ConnectError as e:
            logger.warning(f"Failed to connect to LLM service at {self.base_url}: {e}")
            raise ConnectionError(f"LLM service unavailable at {self.base_url}")
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM request failed with status {e.response.status_code}: {e}")
            raise LLMError(f"LLM request failed: {e.response.status_code}")
        except Exception as e:
            logger.error(f"Unexpected error during LLM generation: {e}")
            raise LLMError(f"LLM generation failed: {str(e)}")
    
    async def chat(self, messages: list[dict], timeout: Optional[float] = None) -> str:
        """
        Chat with Ollama using message history.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            timeout: Optional timeout override
        
        Returns:
            Generated response content
        
        Raises:
            TimeoutError: If generation exceeds timeout
            ConnectionError: If LLM service unavailable
            LLMError: If generation fails
        """
        request_timeout = timeout or self.timeout
        
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "temperature": settings.llm_temperature,
                        "num_predict": settings.llm_max_tokens
                    }
                }
                
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json=payload
                )
                response.raise_for_status()
                return response.json()["message"]["content"]
                
        except httpx.TimeoutException as e:
            logger.warning(f"LLM chat timed out after {request_timeout}s: {e}")
            raise TimeoutError(f"LLM request timed out after {request_timeout}s")
        except httpx.ConnectError as e:
            logger.warning(f"Failed to connect to LLM service at {self.base_url}: {e}")
            raise ConnectionError(f"LLM service unavailable at {self.base_url}")
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM chat failed with status {e.response.status_code}: {e}")
            raise LLMError(f"LLM request failed: {e.response.status_code}")
        except Exception as e:
            logger.error(f"Unexpected error during LLM chat: {e}")
            raise LLMError(f"LLM chat failed: {str(e)}")
