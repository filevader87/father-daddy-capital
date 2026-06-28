import numpy as np
import pandas as pd
from typing import Dict, List, Tuple
from dataclasses import dataclass
from prometheus_client import Counter, Histogram, Gauge
try:
    import tensorflow as tf
except ImportError:
    # Dummy TensorFlow stub for testing
    class _DummyTF:
        class Tensor:
            def __init__(self, value): self.value = value
            def numpy(self): return self.value
        class function:
            def __init__(self, fn): self.fn = fn
            def __call__(self, *args, **kwargs): return self.fn(*args, **kwargs)
        class distribute:
            class MirroredStrategy:
                def __init__(self): pass
                def scope(self): return self
                def __enter__(self): pass
                def __exit__(self, exc_type, exc, tb): pass
        @staticmethod
        def map_fn(fn, elems, fn_output_signature=None):
            return list(map(fn, elems))
        @staticmethod
        def constant(value):
            return _DummyTF.Tensor(value)
    tf = _DummyTF()
from sklearn.preprocessing import StandardScaler

# Prometheus metrics
optimization_runs = Counter('weight_optimization_runs_total', 'Total number of weight optimization runs')
optimization_duration = Histogram('weight_optimization_duration_seconds', 'Time taken for weight optimization')
fitness_scores = Gauge('weight_optimization_fitness', 'Fitness scores during optimization', ['generation'])
weight_changes = Gauge('indicator_weight_changes', 'Changes in indicator weights', ['indicator'])

@dataclass
class IndicatorWeights:
    """Container for indicator weights and their fitness scores."""
    vwap_weight: float
    adx_weight: float
    rsi_weight: float
    macd_weight: float
    fitness: float = 0.0
    
    def to_array(self) -> np.ndarray:
        return np.array([
            self.vwap_weight,
            self.adx_weight,
            self.rsi_weight,
            self.macd_weight
        ])
    
    @classmethod
    def from_array(cls, weights: np.ndarray, fitness: float = 0.0) -> 'IndicatorWeights':
        return cls(
            vwap_weight=weights[0],
            adx_weight=weights[1],
            rsi_weight=weights[2],
            macd_weight=weights[3],
            fitness=fitness
        )

class WeightOptimizer:
    """
    Genetic algorithm-based optimizer for indicator weights.
    Uses TensorFlow for parallel fitness evaluation.
    """
    
    def __init__(self,
                 population_size: int = 100,
                 generations: int = 50,
                 mutation_rate: float = 0.1,
                 crossover_rate: float = 0.8,
                 tournament_size: int = 5,
                 elite_size: int = 2):
        """
        Initialize the weight optimizer.
        
        Args:
            population_size: Size of the genetic population
            generations: Number of generations to evolve
            mutation_rate: Probability of mutation
            crossover_rate: Probability of crossover
            tournament_size: Size of tournament selection
            elite_size: Number of elite individuals to preserve
        """
        self.population_size = population_size
        self.generations = generations
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.tournament_size = tournament_size
        self.elite_size = elite_size
        
        # TensorFlow setup for parallel computation
        self.strategy = tf.distribute.MirroredStrategy()
        
    def _initialize_population(self) -> List[IndicatorWeights]:
        """Initialize random population of weights."""
        population = []
        for _ in range(self.population_size):
            weights = np.random.random(4)
            weights = weights / np.sum(weights)  # Normalize to sum to 1
            population.append(IndicatorWeights.from_array(weights))
        return population
    
    def _calculate_fitness(self,
                         weights: IndicatorWeights,
                         market_data: pd.DataFrame,
                         returns: pd.Series) -> float:
        """
        Calculate fitness score for a set of weights.
        
        Args:
            weights: Indicator weights to evaluate
            market_data: Historical market data
            returns: Asset returns
            
        Returns:
            Fitness score (Sharpe ratio)
        """
        # Calculate weighted signal
        vwap_signal = (market_data['close'] > market_data['vwap']).astype(float)
        adx_signal = (market_data['adx'] > 25).astype(float)
        rsi_signal = ((market_data['rsi'] < 30) | (market_data['rsi'] > 70)).astype(float)
        macd_signal = (market_data['macd'] > market_data['macd_signal']).astype(float)
        
        combined_signal = (
            weights.vwap_weight * vwap_signal +
            weights.adx_weight * adx_signal +
            weights.rsi_weight * rsi_signal +
            weights.macd_weight * macd_signal
        )
        
        # Calculate strategy returns
        strategy_returns = combined_signal.shift(1) * returns
        sharpe_ratio = np.sqrt(252) * strategy_returns.mean() / strategy_returns.std()
        
        return float(sharpe_ratio)
    
    @tf.function
    def _parallel_fitness(self,
                         weight_arrays: tf.Tensor,
                         market_data: tf.Tensor,
                         returns: tf.Tensor) -> tf.Tensor:
        """TensorFlow function for parallel fitness calculation."""
        return tf.map_fn(
            lambda w: self._calculate_fitness(
                IndicatorWeights.from_array(w),
                market_data,
                returns
            ),
            weight_arrays
        )
    
    def _tournament_selection(self,
                            population: List[IndicatorWeights]) -> IndicatorWeights:
        """Select parent using tournament selection."""
        tournament = np.random.choice(population, self.tournament_size)
        return max(tournament, key=lambda x: x.fitness)
    
    def _crossover(self,
                   parent1: IndicatorWeights,
                   parent2: IndicatorWeights) -> Tuple[IndicatorWeights, IndicatorWeights]:
        """Perform crossover between two parents."""
        if np.random.random() > self.crossover_rate:
            return parent1, parent2
            
        weights1 = parent1.to_array()
        weights2 = parent2.to_array()
        
        # Two-point crossover
        points = sorted(np.random.choice(4, 2, replace=False))
        temp = weights1[points[0]:points[1]].copy()
        weights1[points[0]:points[1]] = weights2[points[0]:points[1]]
        weights2[points[0]:points[1]] = temp
        
        # Normalize weights
        weights1 = weights1 / np.sum(weights1)
        weights2 = weights2 / np.sum(weights2)
        
        return (
            IndicatorWeights.from_array(weights1),
            IndicatorWeights.from_array(weights2)
        )
    
    def _mutate(self, individual: IndicatorWeights) -> IndicatorWeights:
        """Apply mutation to an individual."""
        weights = individual.to_array()
        
        for i in range(len(weights)):
            if np.random.random() < self.mutation_rate:
                weights[i] += np.random.normal(0, 0.1)
                
        # Ensure weights are positive and normalized
        weights = np.maximum(weights, 0)
        weights = weights / np.sum(weights)
        
        return IndicatorWeights.from_array(weights)
    
    def optimize(self,
                market_data: pd.DataFrame,
                lookback_window: int = 252) -> IndicatorWeights:
        """
        Optimize indicator weights using genetic algorithm.
        
        Args:
            market_data: Historical market data with indicators
            lookback_window: Days of data to use for optimization
            
        Returns:
            Optimized indicator weights
        """
        with optimization_duration.time():
            optimization_runs.inc()
            
            # Prepare data
            returns = market_data['close'].pct_change()
            market_data = market_data.tail(lookback_window)
            returns = returns.tail(lookback_window)
            
            # Initialize population
            population = self._initialize_population()
            
            # Evolution loop
            best_fitness = float('-inf')
            best_weights = None
            
            for generation in range(self.generations):
                # Evaluate fitness in parallel
                weight_arrays = tf.constant([p.to_array() for p in population])
                fitness_values = self.strategy.run(
                    self._parallel_fitness,
                    args=(weight_arrays, market_data, returns)
                )
                
                # Update population fitness
                for ind, fitness in zip(population, fitness_values):
                    ind.fitness = fitness
                
                # Track best solution
                generation_best = max(population, key=lambda x: x.fitness)
                if generation_best.fitness > best_fitness:
                    best_fitness = generation_best.fitness
                    best_weights = generation_best
                
                # Record metrics
                fitness_scores.labels(generation=str(generation)).set(best_fitness)
                
                # Selection and reproduction
                new_population = []
                
                # Elitism
                sorted_population = sorted(population, key=lambda x: x.fitness, reverse=True)
                new_population.extend(sorted_population[:self.elite_size])
                
                # Create rest of new population
                while len(new_population) < self.population_size:
                    parent1 = self._tournament_selection(population)
                    parent2 = self._tournament_selection(population)
                    child1, child2 = self._crossover(parent1, parent2)
                    child1 = self._mutate(child1)
                    child2 = self._mutate(child2)
                    new_population.extend([child1, child2])
                
                population = new_population[:self.population_size]
            
            # Update weight change metrics
            for i, weight in enumerate(['vwap', 'adx', 'rsi', 'macd']):
                weight_changes.labels(indicator=weight).set(best_weights.to_array()[i])
            
            return best_weights 