import time
import functools
from typing import Any, Callable, Dict, Optional
import numpy as np
from src.utils.logger import get_logger
from src.config import TradingConfig as config

logger = get_logger(__name__)

class PerformanceOptimizer:
    def __init__(self):
        self.profiling_data: Dict[str, Dict] = {}
        self.cache: Dict[str, Any] = {}
        self.cache_ttl: Dict[str, float] = {}
        
    def profile(self, func: Callable) -> Callable:
        """Decorator to profile function execution time"""
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            result = func(*args, **kwargs)
            end_time = time.time()
            
            execution_time = end_time - start_time
            func_name = func.__name__
            
            if func_name not in self.profiling_data:
                self.profiling_data[func_name] = {
                    'total_time': 0.0,
                    'calls': 0,
                    'avg_time': 0.0,
                    'max_time': 0.0,
                    'min_time': float('inf')
                }
                
            stats = self.profiling_data[func_name]
            stats['total_time'] += execution_time
            stats['calls'] += 1
            stats['avg_time'] = stats['total_time'] / stats['calls']
            stats['max_time'] = max(stats['max_time'], execution_time)
            stats['min_time'] = min(stats['min_time'], execution_time)
            
            if execution_time > config.get('performance.slow_threshold', 1.0):
                logger.warning(f"Slow function call: {func_name} took {execution_time:.2f}s")
                
            return result
        return wrapper
        
    def cache_result(self, ttl: int = 300) -> Callable:
        """Decorator to cache function results with TTL"""
        def decorator(func: Callable) -> Callable:
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                cache_key = f"{func.__name__}:{str(args)}:{str(kwargs)}"
                
                # Check cache
                if cache_key in self.cache:
                    if time.time() - self.cache_ttl[cache_key] < ttl:
                        return self.cache[cache_key]
                    else:
                        del self.cache[cache_key]
                        del self.cache_ttl[cache_key]
                        
                # Execute and cache
                result = func(*args, **kwargs)
                self.cache[cache_key] = result
                self.cache_ttl[cache_key] = time.time()
                return result
            return wrapper
        return decorator
        
    def optimize_dataframe(self, df: 'pd.DataFrame') -> 'pd.DataFrame':
        """Optimize pandas DataFrame memory usage"""
        for col in df.columns:
            col_type = df[col].dtype
            
            if col_type == 'object':
                num_unique = df[col].nunique()
                if num_unique / len(df) < 0.5:
                    df[col] = df[col].astype('category')
                    
            elif col_type == 'float64':
                if (df[col] % 1 == 0).all():
                    df[col] = df[col].astype('int32')
                else:
                    df[col] = df[col].astype('float32')
                    
            elif col_type == 'int64':
                if df[col].min() >= 0:
                    if df[col].max() < 255:
                        df[col] = df[col].astype('uint8')
                    elif df[col].max() < 65535:
                        df[col] = df[col].astype('uint16')
                    elif df[col].max() < 4294967295:
                        df[col] = df[col].astype('uint32')
                else:
                    if df[col].min() > -128 and df[col].max() < 127:
                        df[col] = df[col].astype('int8')
                    elif df[col].min() > -32768 and df[col].max() < 32767:
                        df[col] = df[col].astype('int16')
                    elif df[col].min() > -2147483648 and df[col].max() < 2147483647:
                        df[col] = df[col].astype('int32')
                        
        return df
        
    def batch_process(self, data: list, batch_size: int, 
                     process_func: Callable) -> list:
        """Process data in batches to optimize memory usage"""
        results = []
        for i in range(0, len(data), batch_size):
            batch = data[i:i + batch_size]
            batch_results = process_func(batch)
            results.extend(batch_results)
        return results
        
    def get_performance_report(self) -> Dict[str, Dict]:
        """Get performance profiling report"""
        return {
            func_name: {
                'total_calls': stats['calls'],
                'total_time': stats['total_time'],
                'avg_time': stats['avg_time'],
                'max_time': stats['max_time'],
                'min_time': stats['min_time']
            }
            for func_name, stats in self.profiling_data.items()
        }
        
    def clear_cache(self):
        """Clear all cached data"""
        self.cache.clear()
        self.cache_ttl.clear()
        
    def reset_profiling(self):
        """Reset profiling data"""
        self.profiling_data.clear()

# Singleton instance
performance_optimizer = PerformanceOptimizer() 