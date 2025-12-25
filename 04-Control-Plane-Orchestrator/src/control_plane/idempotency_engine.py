"""
Idempotency Engine

Prevents duplicate job creation by tracking idempotency keys.
Uses Redis for fast lookups with configurable TTL.
"""
import logging
from typing import Optional
import redis.asyncio as redis

logger = logging.getLogger(__name__)


class IdempotencyEngine:
    """
    Manages idempotency keys to prevent duplicate job creation.
    
    Stores idempotency_key -> job_id mappings in Redis with TTL.
    This ensures that if the same job is submitted multiple times
    (e.g., due to network retries), only one job is created.
    """
    
    def __init__(self, redis_client: redis.Redis, ttl_seconds: int = 86400):
        """
        Initialize idempotency engine.
        
        Args:
            redis_client: Redis async client
            ttl_seconds: Time-to-live for idempotency keys (default: 24 hours)
        """
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds
        self.key_prefix = "idempotency:"
    
    async def check(self, idempotency_key: str) -> Optional[str]:
        """
        Check if an idempotency key already exists.
        
        Args:
            idempotency_key: The idempotency key to check
            
        Returns:
            Existing job_id if key exists, None otherwise
        """
        if not idempotency_key:
            return None
        
        try:
            redis_key = f"{self.key_prefix}{idempotency_key}"
            existing_job_id = await self.redis.get(redis_key)
            
            if existing_job_id:
                # Decode bytes to string if needed
                if isinstance(existing_job_id, bytes):
                    existing_job_id = existing_job_id.decode('utf-8')
                
                logger.info(
                    f"Idempotency key found: {idempotency_key} -> {existing_job_id}"
                )
                return existing_job_id
            
            return None
            
        except Exception as e:
            logger.error(f"Error checking idempotency key {idempotency_key}: {e}")
            # On error, allow the request to proceed (fail open)
            return None
    
    async def store(self, idempotency_key: str, job_id: str) -> bool:
        """
        Store an idempotency key -> job_id mapping.
        
        Args:
            idempotency_key: The idempotency key
            job_id: The job ID to associate with the key
            
        Returns:
            True if stored successfully, False otherwise
        """
        if not idempotency_key or not job_id:
            return False
        
        try:
            redis_key = f"{self.key_prefix}{idempotency_key}"
            
            # Store with TTL
            await self.redis.setex(
                redis_key,
                self.ttl_seconds,
                job_id
            )
            
            logger.debug(
                f"Stored idempotency key: {idempotency_key} -> {job_id} "
                f"(TTL: {self.ttl_seconds}s)"
            )
            return True
            
        except Exception as e:
            logger.error(
                f"Error storing idempotency key {idempotency_key}: {e}"
            )
            return False
    
    async def delete(self, idempotency_key: str) -> bool:
        """
        Delete an idempotency key (for testing/cleanup).
        
        Args:
            idempotency_key: The idempotency key to delete
            
        Returns:
            True if deleted, False otherwise
        """
        if not idempotency_key:
            return False
        
        try:
            redis_key = f"{self.key_prefix}{idempotency_key}"
            deleted = await self.redis.delete(redis_key)
            return deleted > 0
        except Exception as e:
            logger.error(f"Error deleting idempotency key {idempotency_key}: {e}")
            return False
    
    async def exists(self, idempotency_key: str) -> bool:
        """
        Check if an idempotency key exists (without returning the job_id).
        
        Args:
            idempotency_key: The idempotency key to check
            
        Returns:
            True if key exists, False otherwise
        """
        if not idempotency_key:
            return False
        
        try:
            redis_key = f"{self.key_prefix}{idempotency_key}"
            exists = await self.redis.exists(redis_key)
            return exists > 0
        except Exception as e:
            logger.error(f"Error checking idempotency key existence {idempotency_key}: {e}")
            return False

