"""
State Manager

Manages job state transitions and persistence.
Coordinates between Redis (for fast lookups) and PostgreSQL (for persistence).
"""
import logging
from typing import Optional, Dict, Any
from datetime import datetime
import redis.asyncio as redis
from sqlmodel import select
from .models import Job, JobStatus

logger = logging.getLogger(__name__)


class StateManager:
    """
    Manages job state transitions and provides fast state lookups.
    
    Uses Redis for fast state caching and PostgreSQL as source of truth.
    Ensures state consistency between cache and database.
    """
    
    def __init__(self, redis_client: redis.Redis, db):
        """
        Initialize state manager.
        
        Args:
            redis_client: Redis async client for caching
            db: Database instance (not just engine)
        """
        self.redis = redis_client
        self.db = db  # Database instance to get async sessions
        self.cache_prefix = "job:state:"
        self.cache_ttl = 3600  # 1 hour cache TTL
    
    async def get_job_state(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get job state (cached from Redis, fallback to DB).
        
        Args:
            job_id: The job ID
            
        Returns:
            Job state dict or None if not found
        """
        # Try Redis cache first
        try:
            cache_key = f"{self.cache_prefix}{job_id}"
            cached = await self.redis.get(cache_key)
            
            if cached:
                # Decode if bytes
                if isinstance(cached, bytes):
                    cached = cached.decode('utf-8')
                
                import json
                state = json.loads(cached)
                logger.debug(f"Job {job_id} state from cache")
                return state
        except Exception as e:
            logger.warning(f"Error reading from cache for job {job_id}: {e}")
        
        # Fallback to database
        try:
            async with self.db.session() as session:
                job = await session.get(Job, job_id)
                if not job:
                    return None
                
                state = {
                    "id": job.id,
                    "status": job.status,
                    "domain": job.domain,
                    "job_type": job.job_type,
                    "strategy": job.strategy,
                    "priority": job.priority,
                    "attempts": job.attempts,
                    "max_attempts": job.max_attempts,
                    "created_at": job.created_at.isoformat() if job.created_at else None,
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                    "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                    "error": job.error,
                }
                
                # Cache the result
                await self._cache_job_state(job_id, state)
                
                return state
                
        except Exception as e:
            logger.error(f"Error getting job state from DB for {job_id}: {e}")
            return None
    
    async def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        error: Optional[str] = None,
        **kwargs
    ) -> bool:
        """
        Update job status in both database and cache.
        
        Args:
            job_id: The job ID
            status: New status
            error: Optional error message
            **kwargs: Additional fields to update (e.g., started_at, completed_at)
            
        Returns:
            True if updated successfully, False otherwise
        """
        try:
            async with self.db.session() as session:
                job = await session.get(Job, job_id)
                if not job:
                    logger.error(f"Job {job_id} not found for status update")
                    return False
                
                # Update status
                job.status = status.value if isinstance(status, JobStatus) else status
                
                # Update optional fields
                if error is not None:
                    job.error = error
                
                if "started_at" in kwargs:
                    job.started_at = kwargs["started_at"]
                elif status == JobStatus.RUNNING and not job.started_at:
                    job.started_at = datetime.utcnow()
                
                if "completed_at" in kwargs:
                    job.completed_at = kwargs["completed_at"]
                elif status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
                    if not job.completed_at:
                        job.completed_at = datetime.utcnow()
                
                if "attempts" in kwargs:
                    job.attempts = kwargs["attempts"]
                
                session.add(job)
                await session.commit()
                await session.refresh(job)
                
                # Invalidate cache (will be refreshed on next read)
                await self._invalidate_cache(job_id)
                
                logger.info(f"Updated job {job_id} status to {status}")
                return True
                
        except Exception as e:
            logger.error(f"Error updating job status for {job_id}: {e}")
            return False
    
    async def increment_attempts(self, job_id: str) -> bool:
        """
        Increment job attempt counter.
        
        Args:
            job_id: The job ID
            
        Returns:
            True if updated successfully, False otherwise
        """
        try:
            async with self.db.session() as session:
                job = await session.get(Job, job_id)
                if not job:
                    return False
                
                job.attempts += 1
                session.add(job)
                await session.commit()
                
                # Invalidate cache
                await self._invalidate_cache(job_id)
                
                return True
                
        except Exception as e:
            logger.error(f"Error incrementing attempts for {job_id}: {e}")
            return False
    
    async def _cache_job_state(self, job_id: str, state: Dict[str, Any]) -> None:
        """Cache job state in Redis."""
        try:
            import json
            cache_key = f"{self.cache_prefix}{job_id}"
            await self.redis.setex(
                cache_key,
                self.cache_ttl,
                json.dumps(state)
            )
        except Exception as e:
            logger.warning(f"Error caching job state for {job_id}: {e}")
    
    async def _invalidate_cache(self, job_id: str) -> None:
        """Invalidate cached job state."""
        try:
            cache_key = f"{self.cache_prefix}{job_id}"
            await self.redis.delete(cache_key)
        except Exception as e:
            logger.warning(f"Error invalidating cache for {job_id}: {e}")
    
    async def get_jobs_by_status(self, status: JobStatus, limit: int = 100) -> list[Job]:
        """
        Get jobs by status from database.
        
        Args:
            status: The status to filter by
            limit: Maximum number of jobs to return
            
        Returns:
            List of Job objects
        """
        try:
            async with self.db.session() as session:
                statement = select(Job).where(
                    Job.status == status.value
                ).limit(limit)
                result = await session.execute(statement)
                return list(result.scalars().all())
        except Exception as e:
            logger.error(f"Error getting jobs by status {status}: {e}")
            return []



