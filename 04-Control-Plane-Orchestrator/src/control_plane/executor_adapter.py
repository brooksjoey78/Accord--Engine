"""
Execution Engine Adapter

Bridges Control Plane with Execution Engine.
Handles executor instantiation, job format conversion, and result mapping.
"""
import sys
import os
from typing import Dict, Any, Optional
import logging

# Add Execution Engine to path (for local development)
# In containerized deployments, Execution Engine runs as separate service
execution_engine_path = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "..", "01-Core-Execution-Engine", "src"
)
EXECUTION_ENGINE_AVAILABLE = os.path.exists(execution_engine_path)
if EXECUTION_ENGINE_AVAILABLE and execution_engine_path not in sys.path:
    sys.path.insert(0, execution_engine_path)

logger = logging.getLogger(__name__)

if not EXECUTION_ENGINE_AVAILABLE:
    logger.warning(
        "Execution Engine code not found at expected path. "
        "In containerized deployments, Execution Engine worker handles job execution via Redis Streams."
    )


class ExecutorAdapter:
    """
    Adapter between Control Plane and Execution Engine.
    
    Responsibilities:
    1. Create appropriate executor based on strategy
    2. Convert Control Plane Job format to Execution Engine format
    3. Execute job and handle results
    4. Map Execution Engine results back to Control Plane format
    """
    
    def __init__(
        self,
        redis_client,
        db_session,  # AsyncSession from database
        browser_pool,
    ):
        self.redis = redis_client
        self.db_session = db_session
        self.browser_pool = browser_pool
        self._executor_cache = {}  # Cache executors by strategy
    
    def _get_executor(self, strategy: str):
        """
        Get or create executor for the given strategy.
        
        Strategies:
        - 'vanilla': Basic execution
        - 'stealth': Stealth execution with evasion
        - 'assault': Maximum evasion
        """
        if strategy in self._executor_cache:
            return self._executor_cache[strategy]
        
        try:
            # Import Execution Engine components
            from core.standard_executor import StandardExecutor
            from core.enhanced_executor import EnhancedExecutor
            from strategies import StrategyExecutor
            
            # Use StrategyExecutor to get the right executor
            strategy_executor = StrategyExecutor(
                browser_pool=self.browser_pool,
                redis_client=self.redis,
                prometheus_client=None  # Can add metrics later
            )
            
            # Create a mock job to determine executor type
            # StrategyExecutor needs a job object, but we'll create executor directly
            if strategy == "assault":
                from strategies.assault_executor import AssaultExecutor
                executor = AssaultExecutor(
                    browser_pool=self.browser_pool,
                    redis_client=self.redis
                )
            elif strategy == "stealth":
                from strategies.stealth_executor import StealthExecutor
                executor = StealthExecutor(
                    browser_pool=self.browser_pool,
                    redis_client=self.redis
                )
            else:  # vanilla or default
                from strategies.vanilla_executor import VanillaExecutor
                executor = VanillaExecutor(
                    browser_pool=self.browser_pool,
                    redis_client=self.redis
                )
            
            self._executor_cache[strategy] = executor
            logger.info(f"Created {strategy} executor")
            return executor
            
        except ImportError as e:
            if not EXECUTION_ENGINE_AVAILABLE:
                # In containerized mode, Execution Engine worker handles execution via Redis
                logger.warning(
                    "Execution Engine code not available. "
                    "In containerized deployments, Execution Engine worker consumes from Redis Streams."
                )
                raise RuntimeError(
                    "Execution Engine code not available in container. "
                    "Jobs are executed by the Execution Engine worker service via Redis Streams. "
                    "Ensure execution-engine container is running."
                ) from e
            else:
                logger.error(f"Failed to import Execution Engine: {e}")
                raise
    
    def _convert_job_to_execution_format(
        self,
        job_id: str,
        domain: str,
        url: str,
        job_type: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Convert Control Plane Job format to Execution Engine format.
        
        Control Plane format:
        - id, domain, url, job_type, strategy, payload (JSON string)
        
        Execution Engine format:
        - id, type, target: {domain, url, ip}, parameters: {...}
        """
        # Parse payload if it's a string
        if isinstance(payload, str):
            import json
            try:
                payload = json.loads(payload)
            except:
                payload = {}
        
        # Build execution engine job_data
        job_data = {
            "id": job_id,
            "type": job_type,
            "target": {
                "domain": domain,
                "url": url,
                "ip": payload.get("ip", ""),  # Can extract from payload if needed
            },
            "parameters": payload,  # Pass through all payload data
        }
        
        return job_data
    
    async def execute_job(
        self,
        job_id: str,
        domain: str,
        url: str,
        job_type: str,
        strategy: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Execute a job using the Execution Engine.
        
        Args:
            job_id: Job identifier
            domain: Target domain
            url: Target URL
            job_type: Type of job (navigate_extract, authenticate, etc.)
            strategy: Execution strategy (vanilla, stealth, assault)
            payload: Job payload data
            
        Returns:
            Execution result in Control Plane format:
            {
                "success": bool,
                "data": dict,
                "artifacts": dict,
                "error": str | None,
                "execution_time": float
            }
        """
        try:
            # Get executor for strategy
            executor = self._get_executor(strategy)
            
            # Convert job format
            job_data = self._convert_job_to_execution_format(
                job_id=job_id,
                domain=domain,
                url=url,
                job_type=job_type,
                payload=payload,
            )
            
            # Execute job
            logger.info(f"Executing job {job_id} with {strategy} strategy")
            result = await executor.execute(job_data)
            
            # Convert result to Control Plane format
            return self._convert_result_to_control_plane_format(result)
            
        except Exception as e:
            logger.error(f"Job execution failed: {e}", exc_info=True)
            return {
                "success": False,
                "data": {},
                "artifacts": {},
                "error": str(e),
                "execution_time": 0.0,
            }
    
    def _convert_result_to_control_plane_format(self, result) -> Dict[str, Any]:
        """
        Convert Execution Engine result to Control Plane format.
        
        Execution Engine returns JobResult or ExecutionResult:
        - JobResult: status (JobStatus enum), data, artifacts, error, execution_time
        - ExecutionResult: success (bool), data, error, timing
        
        Control Plane expects:
        - success: bool
        - data: dict
        - artifacts: dict
        - error: str | None
        - execution_time: float
        """
        # Handle JobResult (from StandardExecutor)
        if hasattr(result, "status"):
            # JobResult from core/executor.py
            success = result.status.value in ["success", "completed"]
            return {
                "success": success,
                "data": result.data or {},
                "artifacts": result.artifacts or {},
                "error": result.error,
                "execution_time": result.execution_time or 0.0,
            }
        
        # Handle ExecutionResult (from strategies)
        elif hasattr(result, "success"):
            # ExecutionResult from strategies
            execution_time = 0.0
            if hasattr(result, "timing") and result.timing:
                execution_time = result.timing.get("total_ms", 0.0) / 1000.0  # Convert ms to seconds
            
            return {
                "success": result.success,
                "data": result.data or {},
                "artifacts": getattr(result, "artifacts", {}),
                "error": getattr(result, "error", None),
                "execution_time": execution_time,
            }
        
        # Fallback
        else:
            return {
                "success": False,
                "data": {},
                "artifacts": {},
                "error": "Unknown result format",
                "execution_time": 0.0,
            }

