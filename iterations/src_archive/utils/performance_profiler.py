"""
Performance Profiling and Benchmarking Module
---------------------------------------------
This module provides performance profiling and benchmarking capabilities using pyinstrument
to track runtime hotspots and enforce performance budgets.
"""

import time
import functools
import logging
import os
import json
from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass, asdict
from datetime import datetime
import statistics
from contextlib import contextmanager
import traceback

try:
    import pyinstrument
    from pyinstrument import Profiler
    PYINSTRUMENT_AVAILABLE = True
except ImportError:
    PYINSTRUMENT_AVAILABLE = False
    logging.warning("pyinstrument not available. Install with: pip install pyinstrument")
    # Create a dummy Profiler class for when pyinstrument is not available
    class Profiler:
        def __init__(self):
            pass
        def start(self):
            pass
        def stop(self):
            pass
        def output_html(self):
            return "<html><body><p>Profiling not available - install pyinstrument</p></body></html>"

logger = logging.getLogger(__name__)

@dataclass
class PerformanceBudget:
    """Performance budget configuration."""
    operation: str
    max_time_seconds: float
    max_memory_mb: Optional[float] = None
    max_cpu_percent: Optional[float] = None
    description: str = ""

@dataclass
class PerformanceResult:
    """Result of a performance measurement."""
    operation: str
    execution_time: float
    timestamp: str
    memory_usage_mb: Optional[float] = None
    cpu_percent: Optional[float] = None
    success: bool = True
    error: Optional[str] = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()

class PerformanceProfiler:
    """High-performance profiling and benchmarking system."""
    
    def __init__(self, 
                 enable_profiling: bool = True,
                 profile_output_dir: str = "profiles",
                 performance_budgets: Optional[List[PerformanceBudget]] = None):
        """Initialize performance profiler.
        
        Args:
            enable_profiling: Whether to enable pyinstrument profiling
            profile_output_dir: Directory to save profile reports
            performance_budgets: List of performance budgets to enforce
        """
        self.enable_profiling = enable_profiling and PYINSTRUMENT_AVAILABLE
        self.profile_output_dir = profile_output_dir
        self.performance_budgets = performance_budgets or []
        self.results: List[PerformanceResult] = []
        
        # Create output directory
        os.makedirs(profile_output_dir, exist_ok=True)
        
        # Performance budgets for common operations
        self.default_budgets = [
            PerformanceBudget("data_ingest", 2.0, description="Data ingestion should complete within 2 seconds"),
            PerformanceBudget("model_inference", 0.05, description="Model inference should complete within 50ms"),
            PerformanceBudget("feature_engineering", 1.0, description="Feature engineering should complete within 1 second"),
            PerformanceBudget("backtest_single", 5.0, description="Single backtest should complete within 5 seconds"),
            PerformanceBudget("backtest_batch", 30.0, description="Batch backtest should complete within 30 seconds"),
            PerformanceBudget("signal_generation", 0.1, description="Signal generation should complete within 100ms"),
            PerformanceBudget("order_execution", 1.0, description="Order execution should complete within 1 second"),
        ]
        
        # Merge default budgets with custom budgets
        budget_dict = {b.operation: b for b in self.default_budgets}
        for budget in self.performance_budgets:
            budget_dict[budget.operation] = budget
        
        self.performance_budgets = list(budget_dict.values())
        
        logger.info(f"Initialized PerformanceProfiler with {len(self.performance_budgets)} budgets")
    
    @contextmanager
    def profile_operation(self, operation: str, metadata: Optional[Dict[str, Any]] = None):
        """Context manager for profiling operations.
        
        Args:
            operation: Name of the operation being profiled
            metadata: Additional metadata for the operation
        """
        start_time = time.time()
        start_memory = self._get_memory_usage()
        start_cpu = self._get_cpu_usage()
        
        profiler = None
        if self.enable_profiling:
            profiler = Profiler()
            profiler.start()
        
        try:
            yield
            
            # Record successful execution
            execution_time = time.time() - start_time
            memory_usage = self._get_memory_usage() - start_memory if start_memory else None
            cpu_usage = self._get_cpu_usage() - start_cpu if start_cpu else None
            
            result = PerformanceResult(
                operation=operation,
                execution_time=execution_time,
                timestamp=datetime.now().isoformat(),
                memory_usage_mb=memory_usage,
                cpu_percent=cpu_usage,
                metadata=metadata or {}
            )
            
            self.results.append(result)
            
            # Check performance budget
            self._check_performance_budget(result)
            
            # Save profile if enabled
            if profiler:
                self._save_profile(profiler, operation)
            
            logger.debug(f"Operation '{operation}' completed in {execution_time:.3f}s")
            
        except Exception as e:
            # Record failed execution
            execution_time = time.time() - start_time
            result = PerformanceResult(
                operation=operation,
                execution_time=execution_time,
                timestamp=datetime.now().isoformat(),
                success=False,
                error=str(e),
                metadata=metadata or {}
            )
            
            self.results.append(result)
            logger.error(f"Operation '{operation}' failed after {execution_time:.3f}s: {e}")
            raise
    
    def profile_function(self, operation: str = None, metadata: Optional[Dict[str, Any]] = None):
        """Decorator for profiling functions.
        
        Args:
            operation: Name of the operation (defaults to function name)
            metadata: Additional metadata for the operation
        """
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                op_name = operation or func.__name__
                with self.profile_operation(op_name, metadata):
                    return func(*args, **kwargs)
            return wrapper
        return decorator
    
    def benchmark_function(self, 
                          func: Callable, 
                          args: tuple = (), 
                          kwargs: dict = None,
                          iterations: int = 100,
                          operation: str = None) -> Dict[str, Any]:
        """Benchmark a function with multiple iterations.
        
        Args:
            func: Function to benchmark
            args: Arguments to pass to function
            kwargs: Keyword arguments to pass to function
            iterations: Number of iterations to run
            operation: Name of the operation (defaults to function name)
            
        Returns:
            Dictionary with benchmark results
        """
        if kwargs is None:
            kwargs = {}
        
        op_name = operation or func.__name__
        times = []
        
        logger.info(f"Benchmarking '{op_name}' with {iterations} iterations")
        
        for i in range(iterations):
            with self.profile_operation(f"{op_name}_iteration_{i}"):
                start_time = time.time()
                result = func(*args, **kwargs)
                execution_time = time.time() - start_time
                times.append(execution_time)
        
        # Calculate statistics
        stats = {
            'operation': op_name,
            'iterations': iterations,
            'mean_time': statistics.mean(times),
            'median_time': statistics.median(times),
            'min_time': min(times),
            'max_time': max(times),
            'std_time': statistics.stdev(times) if len(times) > 1 else 0,
            'total_time': sum(times),
            'times': times
        }
        
        # Check performance budget
        budget = self._get_budget_for_operation(op_name)
        if budget:
            if stats['mean_time'] > budget.max_time_seconds:
                logger.warning(f"Performance budget exceeded for '{op_name}': "
                             f"{stats['mean_time']:.3f}s > {budget.max_time_seconds}s")
        
        return stats
    
    def _check_performance_budget(self, result: PerformanceResult):
        """Check if a performance result exceeds its budget."""
        budget = self._get_budget_for_operation(result.operation)
        if not budget:
            return
        
        violations = []
        
        if result.execution_time > budget.max_time_seconds:
            violations.append(f"Time: {result.execution_time:.3f}s > {budget.max_time_seconds}s")
        
        if budget.max_memory_mb and result.memory_usage_mb:
            if result.memory_usage_mb > budget.max_memory_mb:
                violations.append(f"Memory: {result.memory_usage_mb:.1f}MB > {budget.max_memory_mb}MB")
        
        if budget.max_cpu_percent and result.cpu_percent:
            if result.cpu_percent > budget.max_cpu_percent:
                violations.append(f"CPU: {result.cpu_percent:.1f}% > {budget.max_cpu_percent}%")
        
        if violations:
            error_msg = f"Performance budget violated for '{result.operation}': {', '.join(violations)}"
            logger.error(error_msg)
            
            # In CI environment, this would fail the build
            if os.getenv('CI') == 'true':
                raise PerformanceBudgetViolation(error_msg)
    
    def _get_budget_for_operation(self, operation: str) -> Optional[PerformanceBudget]:
        """Get performance budget for an operation."""
        for budget in self.performance_budgets:
            if budget.operation == operation:
                return budget
        return None
    
    def _get_memory_usage(self) -> Optional[float]:
        """Get current memory usage in MB."""
        try:
            import psutil
            process = psutil.Process()
            return process.memory_info().rss / 1024 / 1024  # Convert to MB
        except ImportError:
            return None
    
    def _get_cpu_usage(self) -> Optional[float]:
        """Get current CPU usage percentage."""
        try:
            import psutil
            process = psutil.Process()
            return process.cpu_percent()
        except ImportError:
            return None
    
    def _save_profile(self, profiler: Profiler, operation: str):
        """Save profile report to file."""
        try:
            profiler.stop()
            
            # Generate filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{operation}_{timestamp}.html"
            filepath = os.path.join(self.profile_output_dir, filename)
            
            # Save HTML report
            with open(filepath, 'w') as f:
                f.write(profiler.output_html())
            
            logger.debug(f"Profile saved to {filepath}")
            
        except Exception as e:
            logger.error(f"Failed to save profile: {e}")
    
    def get_performance_summary(self) -> Dict[str, Any]:
        """Get summary of all performance results."""
        if not self.results:
            return {"message": "No performance results available"}
        
        # Group results by operation
        operation_results = {}
        for result in self.results:
            if result.operation not in operation_results:
                operation_results[result.operation] = []
            operation_results[result.operation].append(result)
        
        # Calculate statistics for each operation
        summary = {}
        for operation, results in operation_results.items():
            successful_results = [r for r in results if r.success]
            failed_results = [r for r in results if not r.success]
            
            if successful_results:
                times = [r.execution_time for r in successful_results]
                summary[operation] = {
                    'total_executions': len(results),
                    'successful_executions': len(successful_results),
                    'failed_executions': len(failed_results),
                    'success_rate': len(successful_results) / len(results),
                    'mean_time': statistics.mean(times),
                    'median_time': statistics.median(times),
                    'min_time': min(times),
                    'max_time': max(times),
                    'std_time': statistics.stdev(times) if len(times) > 1 else 0,
                    'total_time': sum(times)
                }
            else:
                summary[operation] = {
                    'total_executions': len(results),
                    'successful_executions': 0,
                    'failed_executions': len(failed_results),
                    'success_rate': 0.0,
                    'error': 'All executions failed'
                }
        
        return summary
    
    def save_results(self, filename: str = None):
        """Save performance results to JSON file."""
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"performance_results_{timestamp}.json"
        
        filepath = os.path.join(self.profile_output_dir, filename)
        
        # Convert results to serializable format
        serializable_results = []
        for result in self.results:
            serializable_results.append(asdict(result))
        
        data = {
            'timestamp': datetime.now().isoformat(),
            'summary': self.get_performance_summary(),
            'results': serializable_results,
            'budgets': [asdict(b) for b in self.performance_budgets]
        }
        
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
        
        logger.info(f"Performance results saved to {filepath}")
        return filepath
    
    def clear_results(self):
        """Clear all performance results."""
        self.results.clear()
        logger.info("Performance results cleared")
    
    def add_performance_budget(self, budget: PerformanceBudget):
        """Add a new performance budget."""
        # Remove existing budget for this operation if it exists
        self.performance_budgets = [b for b in self.performance_budgets if b.operation != budget.operation]
        self.performance_budgets.append(budget)
        logger.info(f"Added performance budget for '{budget.operation}': {budget.max_time_seconds}s")

class PerformanceBudgetViolation(Exception):
    """Exception raised when performance budget is violated."""
    pass

# Global profiler instance
_performance_profiler = None

def get_performance_profiler() -> PerformanceProfiler:
    """Get or create global performance profiler instance."""
    global _performance_profiler
    
    if _performance_profiler is None:
        _performance_profiler = PerformanceProfiler()
    
    return _performance_profiler

def profile_operation(operation: str = None, metadata: Optional[Dict[str, Any]] = None):
    """Decorator for profiling operations using the global profiler."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            profiler = get_performance_profiler()
            op_name = operation or func.__name__
            with profiler.profile_operation(op_name, metadata):
                return func(*args, **kwargs)
        return wrapper
    return decorator

def benchmark_operation(iterations: int = 100, operation: str = None):
    """Decorator for benchmarking operations."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            profiler = get_performance_profiler()
            op_name = operation or func.__name__
            return profiler.benchmark_function(func, args, kwargs, iterations, op_name)
        return wrapper
    return decorator 