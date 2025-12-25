# 04-Control-Plane-Orchestrator/src/control_plane/job_orchestrator.py
import asyncio
import json
import uuid
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from enum import Enum
import hashlib

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
import redis.asyncio as redis

from .models import Job, JobExecution
from .queue_manager import QueueManager
from .state_manager import StateManager
from .idempotency_engine import IdempotencyEngine
from .executor_adapter import ExecutorAdapter

logger = logging.getLogger(__name__)

class JobStatus(Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class JobOrchestrator:
    def __init__(
        self,
        redis_client: redis.Redis,
        db,  # Database instance (not just engine)
        browser_pool=None,  # BrowserPool from Execution Engine
        db_session=None,  # AsyncSession for Execution Engine
        max_concurrent_jobs: int = 100,
        policy_enforcer=None,  # PolicyEnforcer instance
    ):
        self.redis = redis_client
        self.db = db  # Database instance to get async sessions
        self.db_engine = db.engine  # Keep for backward compatibility if needed
        self.max_concurrent_jobs = max_concurrent_jobs
        
        self.queue_manager = QueueManager(redis_client)
        self.state_manager = StateManager(redis_client, db)
        self.idempotency_engine = IdempotencyEngine(redis_client)
        self.policy_enforcer = policy_enforcer  # Policy enforcer for compliance
        
        # Execution Engine adapter (lazy initialization)
        self._executor_adapter = None
        self._browser_pool = browser_pool
        self._db_session = db_session
        
        self._running_jobs: Dict[str, asyncio.Task] = {}
        self._workers: List[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()
    
    def _get_executor_adapter(self) -> ExecutorAdapter:
        """Get or create executor adapter."""
        if self._executor_adapter is None:
            if not self._browser_pool:
                logger.warning("Browser pool not provided, Execution Engine may not work")
            if not self._db_session:
                logger.warning("DB session not provided, Execution Engine may not work")
            
            self._executor_adapter = ExecutorAdapter(
                redis_client=self.redis,
                db_session=self._db_session,
                browser_pool=self._browser_pool,
            )
        return self._executor_adapter
        
    async def create_job(
        self,
        domain: str,
        url: str,
        job_type: str,
        strategy: str,
        payload: Dict[str, Any],
        priority: int,
        idempotency_key: Optional[str] = None,
        timeout_seconds: int = 300,
        authorization_mode: str = "public",
        user_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> str:
        """
        Create a new job with idempotency check and policy enforcement.
        
        Args:
            authorization_mode: 'public', 'customer-authorized', or 'internal'
            user_id: User/API key ID for audit logging
            ip_address: Request IP address for audit logging
        """
        from ..compliance.models import AuthorizationMode as AuthMode
        
        # Generate job ID
        job_id = str(uuid.uuid4())
        
        # Check idempotency
        if idempotency_key:
            existing = await self.idempotency_engine.check(idempotency_key)
            if existing:
                logger.info(f"Idempotent job found: {existing}")
                return existing
        
        # Policy enforcement at job creation time
        if self.policy_enforcer:
            try:
                auth_mode = AuthMode(authorization_mode.lower())
                allowed, action, reason, context = await self.policy_enforcer.check_policy(
                    job_id=job_id,
                    domain=domain,
                    url=url,
                    strategy=strategy,
                    authorization_mode=auth_mode,
                    user_id=user_id,
                    ip_address=ip_address,
                )
                
                if not allowed:
                    # Policy violation - reject job
                    error_msg = f"Policy violation: {reason}"
                    logger.warning(f"Job {job_id} rejected by policy: {reason}")
                    raise ValueError(error_msg)
            except ValueError:
                raise  # Re-raise policy violations
            except Exception as e:
                logger.error(f"Error checking policy for job {job_id}: {e}")
                # Fail open in case of policy check errors (can be configured)
                # For now, we'll allow the job to proceed if policy check fails
        
        # Create job record
        job = Job(
            id=job_id,
            domain=domain,
            url=url,
            job_type=job_type,
            strategy=strategy,
            payload=json.dumps(payload),
            priority=priority,
            status=JobStatus.PENDING.value,
            max_attempts=3,
            timeout_seconds=timeout_seconds
        )
        
        # Use async session
        async with self.db.session() as session:
            session.add(job)
            await session.commit()
        
        # Prepare job data for Execution Engine worker
        job_data = {
            "id": job_id,
            "url": url,
            "type": job_type,
            "domain": domain,
            "strategy": strategy,
            "payload": payload,
            "priority": priority,
            "timeout_seconds": timeout_seconds
        }
        
        # Enqueue job with full data for Execution Engine worker
        await self.queue_manager.enqueue(
            job_id=job_id,
            priority=priority,
            domain=domain,
            job_data=job_data
        )
        
        # Store idempotency key if provided
        if idempotency_key:
            await self.idempotency_engine.store(idempotency_key, job_id)
        
        logger.info(f"Created job {job_id} for domain {domain}")
        return job_id
    
    async def process_job(self, job_id: str):
        """Process a single job with policy enforcement at execution time."""
        from ..compliance.models import AuthorizationMode as AuthMode
        
        # Get job from DB
        async with self.db.session() as session:
            job = await session.get(Job, job_id)
            if not job:
                logger.error(f"Job {job_id} not found")
                return
            
            # Policy enforcement at execution time (hard stop)
            if self.policy_enforcer:
                try:
                    # Increment concurrency counter
                    await self.policy_enforcer.increment_concurrency(job.domain)
                    
                    # Re-check policy at execution time (policy may have changed)
                    # Default to public if not specified
                    auth_mode = AuthMode.PUBLIC  # Can be stored in job metadata if needed
                    allowed, action, reason, context = await self.policy_enforcer.check_policy(
                        job_id=job_id,
                        domain=job.domain,
                        url=job.url,
                        strategy=job.strategy,
                        authorization_mode=auth_mode,
                    )
                    
                    if not allowed:
                        # Hard stop - cancel job
                        job.status = JobStatus.CANCELLED.value
                        job.error = f"Policy violation at execution: {reason}"
                        job.completed_at = datetime.utcnow()
                        await session.commit()
                        
                        # Decrement concurrency
                        await self.policy_enforcer.decrement_concurrency(job.domain)
                        
                        logger.warning(f"Job {job_id} cancelled due to policy violation: {reason}")
                        return
                except Exception as e:
                    logger.error(f"Error checking policy at execution for job {job_id}: {e}")
                    # Continue execution if policy check fails (fail open)
            
            # Update status
            job.status = JobStatus.RUNNING.value
            job.started_at = datetime.utcnow()
            job.attempts += 1
            await session.commit()
        
        # Execute job
        execution_id = str(uuid.uuid4())
        start_time = datetime.utcnow()
        
        try:
            # Call execution engine
            result = await self._execute_job(job)
            
            # Check if execution was successful
            success = result.get("success", False)
            error = result.get("error")
            
            # Process workflow results if this is a workflow job
            workflow_output = None
            if success and job.job_type == "navigate_extract":
                payload = json.loads(job.payload) if job.payload else {}
                workflow_type = payload.get("workflow_type")
                if workflow_type:
                    # Map workflow_type to workflow name
                    workflow_name_map = {
                        "page_change_detection": "page_change_detection",
                        "job_posting_monitor": "job_posting_monitor",
                        "uptime_smoke_check": "uptime_smoke_check"
                    }
                    workflow_name = workflow_name_map.get(workflow_type)
                    if workflow_name:
                        # Import here to avoid circular dependency
                        from ..workflows.workflow_executor import WorkflowExecutor
                        workflow_exec = WorkflowExecutor(self)
                        workflow_output = await workflow_exec.process_workflow_result(
                            workflow_name=workflow_name,
                            job_id=job_id,
                            job_result={
                                "success": success,
                                "data": result.get("data", {}),
                                "artifacts": result.get("artifacts", {}),
                                "payload": payload,
                                "execution_time": result.get("execution_time", 0.0)
                            }
                        )
            
            # Update job status based on execution result
            async with self.db.session() as session:
                job = await session.get(Job, job_id)
                
                if success:
                    job.status = JobStatus.COMPLETED.value
                    job.completed_at = datetime.utcnow()
                    # Use workflow output if available, otherwise use raw result
                    result_data = workflow_output if workflow_output else result.get("data", {})
                    job.result = json.dumps(result_data)
                    job.artifacts = json.dumps(result.get("artifacts", []))
                    job.error = None
                    
                    # Decrement concurrency counter
                    if self.policy_enforcer:
                        await self.policy_enforcer.decrement_concurrency(job.domain)
                else:
                    # Execution failed - check if we should retry
                    if job.attempts < job.max_attempts:
                        job.status = JobStatus.PENDING.value  # Retry
                        job.error = error
                        logger.warning(f"Job {job_id} failed, will retry (attempt {job.attempts}/{job.max_attempts})")
                        
                        # Decrement concurrency counter (will be incremented again on retry)
                        if self.policy_enforcer:
                            await self.policy_enforcer.decrement_concurrency(job.domain)
                    else:
                        job.status = JobStatus.FAILED.value
                        job.completed_at = datetime.utcnow()
                        job.error = error
                        logger.error(f"Job {job_id} failed after {job.attempts} attempts")
                        
                        # Decrement concurrency counter
                        if self.policy_enforcer:
                            await self.policy_enforcer.decrement_concurrency(job.domain)
                
                await session.commit()
            
            # Record execution
            await self._record_execution(
                execution_id=execution_id,
                job_id=job_id,
                attempt=job.attempts,
                status="success" if success else "failed",
                execution_time_ms=int((datetime.utcnow() - start_time).total_seconds() * 1000)
            )
            
            if success:
                logger.info(f"Job {job_id} completed successfully")
            else:
                logger.warning(f"Job {job_id} execution failed: {error}")
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Job {job_id} failed: {error_msg}")
            
            # Update job as failed or retry
            async with self.db.session() as session:
                job = await session.get(Job, job_id)
                job.error = error_msg
                
                if job.attempts >= job.max_attempts:
                    job.status = JobStatus.FAILED.value
                    job.completed_at = datetime.utcnow()
                    
                    # Decrement concurrency counter
                    if self.policy_enforcer:
                        await self.policy_enforcer.decrement_concurrency(job.domain)
                else:
                    job.status = JobStatus.PENDING.value
                    
                    # Decrement concurrency counter (will be incremented again on retry)
                    if self.policy_enforcer:
                        await self.policy_enforcer.decrement_concurrency(job.domain)
                    # Requeue with backoff and job data
                    job_data = {
                        "id": job_id,
                        "url": job.url,
                        "type": job.job_type,
                        "domain": job.domain,
                        "strategy": job.strategy,
                        "payload": json.loads(job.payload) if job.payload else {},
                        "priority": job.priority,
                        "timeout_seconds": job.timeout_seconds
                    }
                    await self.queue_manager.requeue(
                        job_id=job_id,
                        priority=job.priority,
                        domain=job.domain,
                        job_data=job_data,
                        delay_seconds=60 * job.attempts  # Exponential backoff
                    )
                
                await session.commit()
            
            # Record failed execution
            await self._record_execution(
                execution_id=execution_id,
                job_id=job_id,
                attempt=job.attempts,
                status="failed",
                error=error_msg,
                execution_time_ms=int((datetime.utcnow() - start_time).total_seconds() * 1000)
            )
        
        finally:
            # Remove from running jobs
            self._running_jobs.pop(job_id, None)
    
    async def _execute_job(self, job: Job) -> Dict[str, Any]:
        """
        Execute job using Execution Engine.
        
        This method:
        1. Gets the executor adapter
        2. Converts job format
        3. Executes via Execution Engine
        4. Returns result in Control Plane format
        """
        # Parse payload
        payload = json.loads(job.payload) if job.payload else {}
        
        try:
            # Get executor adapter
            adapter = self._get_executor_adapter()
            
            # Execute job via Execution Engine
            result = await adapter.execute_job(
                job_id=job.id,
                domain=job.domain,
                url=job.url,
                job_type=job.job_type,
                strategy=job.strategy,
                payload=payload,
            )
            
            # Return in expected format
            return {
                "success": result.get("success", False),
                "data": result.get("data", {}),
                "artifacts": result.get("artifacts", {}),
                "error": result.get("error"),
                "execution_time": result.get("execution_time", 0.0),
            }
            
        except Exception as e:
            logger.error(f"Job execution failed: {e}", exc_info=True)
            return {
                "success": False,
                "data": {},
                "artifacts": {},
                "error": str(e),
                "execution_time": 0.0,
            }
    
    async def _record_execution(self, execution_id: str, job_id: str, attempt: int,
                              status: str, error: Optional[str] = None,
                              execution_time_ms: Optional[int] = None):
        """Record job execution."""
        execution = JobExecution(
            id=execution_id,
            job_id=job_id,
            attempt=attempt,
            status=status,
            started_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
            execution_time_ms=execution_time_ms,
            error=error
        )
        
        async with self.db.session() as session:
            session.add(execution)
            await session.commit()
    
    async def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed job status."""
        async with self.db.session() as session:
            job = await session.get(Job, job_id)
            if not job:
                return None
            
            # Get queue position if pending/queued
            queue_position = None
            if job.status in [JobStatus.PENDING.value, JobStatus.QUEUED.value]:
                queue_position = await self.queue_manager.get_position(job_id)
            
            # Parse result if exists
            result = None
            if job.result:
                try:
                    result = json.loads(job.result)
                except:
                    result = {"raw": job.result}
            
            artifacts = []
            if job.artifacts:
                try:
                    artifacts = json.loads(job.artifacts)
                except:
                    artifacts = []
            
            return {
                "job_id": job.id,
                "status": job.status,
                "progress": 100.0 if job.completed_at else 0.0,
                "result": result,
                "error": job.error,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
                "artifacts": artifacts
            }
    
    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a job if not running."""
        async with self.db.session() as session:
            job = await session.get(Job, job_id)
            if not job:
                return False
            
            # Can only cancel pending/queued jobs
            if job.status == JobStatus.RUNNING.value:
                return False
            
            # Update status
            job.status = JobStatus.CANCELLED.value
            job.completed_at = datetime.utcnow()
            await session.commit()
        
        # Remove from queue
        await self.queue_manager.remove(job_id)
        
        # Cancel running task if exists
        if job_id in self._running_jobs:
            self._running_jobs[job_id].cancel()
        
        logger.info(f"Cancelled job {job_id}")
        return True
    
    async def start_worker(self, worker_id: str):
        """Start a worker that processes jobs from queue."""
        logger.info(f"Starting worker {worker_id}")
        
        # Initialize consumer group for this worker
        await self.queue_manager.initialize_consumer_group(worker_id)
        
        while not self._shutdown_event.is_set():
            try:
                # Get next job from queue
                job_id = await self.queue_manager.dequeue(timeout=5.0)
                if not job_id:
                    await asyncio.sleep(1)
                    continue
                
                # Check if we can run more jobs
                if len(self._running_jobs) >= self.max_concurrent_jobs:
                    await self.queue_manager.requeue(job_id, priority=2, domain="system", delay_seconds=5)
                    await asyncio.sleep(1)
                    continue
                
                # Process job
                task = asyncio.create_task(self.process_job(job_id))
                self._running_jobs[job_id] = task
                
                # Clean up task when done
                def cleanup(_):
                    self._running_jobs.pop(job_id, None)
                
                task.add_done_callback(cleanup)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}")
                await asyncio.sleep(5)
        
        logger.info(f"Worker {worker_id} stopped")
    
    async def get_queue_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        stats = await self.queue_manager.get_stats()
        
        async with self.db.session() as session:
            # Get job counts by status
            statement = select(Job.status, Job.domain)
            result = await session.execute(statement)
            rows = result.all()
            
            status_counts = {}
            domain_counts = {}
            
            for row in rows:
                status = row[0]  # First column: status
                domain = row[1]  # Second column: domain
                status_counts[status] = status_counts.get(status, 0) + 1
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
        
        return {
            "queue": stats,
            "jobs": {
                "total": len(rows),
                "by_status": status_counts,
                "by_domain": domain_counts
            },
            "running_jobs": len(self._running_jobs),
            "workers": len(self._workers)
        }
    
    async def get_queue_depth(self) -> int:
        """Get current queue depth."""
        return await self.queue_manager.get_depth()
    
    async def shutdown(self):
        """Shutdown orchestrator."""
        logger.info("Shutting down orchestrator...")
        
        # Signal workers to stop
        self._shutdown_event.set()
        
        # Cancel all workers
        for worker in self._workers:
            worker.cancel()
        
        # Wait for workers to complete
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        
        # Cancel running jobs
        for job_id, task in list(self._running_jobs.items()):
            task.cancel()
        
        # Wait for tasks to complete
        if self._running_jobs:
            await asyncio.gather(*self._running_jobs.values(), return_exceptions=True)
        
        logger.info("Orchestrator shutdown complete")