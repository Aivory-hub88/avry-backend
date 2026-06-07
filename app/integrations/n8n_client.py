"""n8n Integration Client for Aivory"""
import logging
import time
from typing import Dict, Any, Optional
import httpx
from datetime import datetime

logger = logging.getLogger(__name__)


class N8nClient:
    """Client for n8n workflow automation integration"""
    
    def __init__(self, base_url: str, timeout: float = 10.0, max_retries: int = 3):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.max_retries = max_retries
        self._connection_status = None
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Check n8n connection health.
        
        Returns:
            {
                "status": "connected" | "failed",
                "latency_ms": float,
                "timestamp": str (ISO format)
            }
        """
        start_time = time.time()
        timestamp = datetime.utcnow().isoformat()
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/healthz")
                
                latency_ms = (time.time() - start_time) * 1000
                
                if response.status_code == 200:
                    self._connection_status = "connected"
                    logger.info(f"n8n health check: CONNECTED (latency: {latency_ms:.2f}ms)")
                    return {
                        "status": "connected",
                        "latency_ms": round(latency_ms, 2),
                        "timestamp": timestamp
                    }
                else:
                    self._connection_status = "failed"
                    logger.error(f"n8n health check failed: HTTP {response.status_code}")
                    return {
                        "status": "failed",
                        "latency_ms": round(latency_ms, 2),
                        "timestamp": timestamp,
                        "error": f"HTTP {response.status_code}"
                    }
                    
        except httpx.TimeoutException as e:
            latency_ms = (time.time() - start_time) * 1000
            self._connection_status = "failed"
            logger.error(f"n8n health check timeout: {e}")
            return {
                "status": "failed",
                "latency_ms": round(latency_ms, 2),
                "timestamp": timestamp,
                "error": "Connection timeout"
            }
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            self._connection_status = "failed"
            logger.error(f"n8n health check error: {e}")
            return {
                "status": "failed",
                "latency_ms": round(latency_ms, 2),
                "timestamp": timestamp,
                "error": str(e)
            }
    
    async def trigger_webhook(
        self,
        webhook_path: str,
        payload: Dict[str, Any],
        method: str = "POST"
    ) -> Dict[str, Any]:
        """
        Trigger n8n webhook with retry logic.
        
        Args:
            webhook_path: Webhook path (e.g., "/webhook/aivory-diagnostic")
            payload: JSON payload to send
            method: HTTP method (POST, GET, etc.)
        
        Returns:
            Response data from n8n
        
        Raises:
            ConnectionError: If all retry attempts fail
        """
        url = f"{self.base_url}{webhook_path}"
        
        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    logger.info(f"Triggering n8n webhook (attempt {attempt}/{self.max_retries}): {url}")
                    
                    if method.upper() == "POST":
                        response = await client.post(url, json=payload)
                    elif method.upper() == "GET":
                        response = await client.get(url, params=payload)
                    else:
                        response = await client.request(method, url, json=payload)
                    
                    if response.is_success:
                        logger.info(f"n8n webhook triggered successfully: {url}")
                        try:
                            return response.json()
                        except:
                            return {"status": "success", "raw_response": response.text}
                    else:
                        logger.warning(f"n8n webhook returned HTTP {response.status_code}")
                        if attempt < self.max_retries:
                            await self._exponential_backoff(attempt)
                            continue
                        else:
                            raise ConnectionError(f"n8n webhook failed: HTTP {response.status_code}")
                            
            except httpx.TimeoutException as e:
                logger.warning(f"n8n webhook timeout (attempt {attempt}/{self.max_retries})")
                if attempt < self.max_retries:
                    await self._exponential_backoff(attempt)
                    continue
                else:
                    raise ConnectionError(f"n8n webhook timeout after {self.max_retries} attempts")
                    
            except httpx.RequestError as e:
                logger.warning(f"n8n webhook connection error (attempt {attempt}/{self.max_retries}): {e}")
                if attempt < self.max_retries:
                    await self._exponential_backoff(attempt)
                    continue
                else:
                    raise ConnectionError(f"n8n webhook connection failed after {self.max_retries} attempts: {e}")
        
        raise ConnectionError(f"n8n webhook failed after {self.max_retries} attempts")
    
    async def _exponential_backoff(self, attempt: int):
        """Exponential backoff delay between retries"""
        import asyncio
        delay = min(2 ** attempt, 8)  # Max 8 seconds
        logger.info(f"Retrying in {delay} seconds...")
        await asyncio.sleep(delay)
    
    @property
    def is_connected(self) -> Optional[bool]:
        """Get cached connection status"""
        return self._connection_status == "connected" if self._connection_status else None
