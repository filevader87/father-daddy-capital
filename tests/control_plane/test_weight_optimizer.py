import unittest
import pandas as pd
import numpy as np
import pytest
from src.control_plane.weight_optimizer import WeightOptimizer, IndicatorWeights

@pytest.mark.skip(reason="Requires tensorflow which is not available")
class TestWeightOptimizer(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures."""
        self.optimizer = WeightOptimizer(
            population_size=20,  # Smaller population for testing
            generations=5,       # Fewer generations for testing
            mutation_rate=0.1,
            crossover_rate=0.8,
            tournament_size=3,
            elite_size=1
        )
        
        # Create sample market data
        dates = pd.date_range(start='2023-01-01', periods=100, freq='D')
        self.market_data = pd.DataFrame({
            'close': np.random.random(100) * 100 + 100,
            'vwap': np.random.random(100) * 100 + 100,
            'adx': np.random.random(100) * 100,
            'rsi': np.random.random(100) * 100,
            'macd': np.random.random(100) * 10 - 5,
            'macd_signal': np.random.random(100) * 10 - 5
        }, index=dates)
        
    def test_initialization(self):
        """Test population initialization."""
        population = self.optimizer._initialize_population()
        
        self.assertEqual(len(population), self.optimizer.population_size)
        
        for individual in population:
            weights = individual.to_array()
            self.assertEqual(len(weights), 4)
            self.assertAlmostEqual(np.sum(weights), 1.0)
            self.assertTrue(all(w >= 0 for w in weights))
            
    def test_fitness_calculation(self):
        """Test fitness calculation."""
        weights = IndicatorWeights(
            vwap_weight=0.25,
            adx_weight=0.25,
            rsi_weight=0.25,
            macd_weight=0.25
        )
        
        fitness = self.optimizer._calculate_fitness(
            weights,
            self.market_data,
            self.market_data['close'].pct_change()
        )
        
        self.assertIsInstance(fitness, float)
        
    def test_tournament_selection(self):
        """Test tournament selection."""
        population = [
            IndicatorWeights.from_array(np.array([0.25] * 4), fitness=1.0),
            IndicatorWeights.from_array(np.array([0.25] * 4), fitness=2.0),
            IndicatorWeights.from_array(np.array([0.25] * 4), fitness=3.0)
        ]
        
        selected = self.optimizer._tournament_selection(population)
        self.assertIsInstance(selected, IndicatorWeights)
        
    def test_crossover(self):
        """Test crossover operation."""
        parent1 = IndicatorWeights.from_array(np.array([0.1, 0.2, 0.3, 0.4]))
        parent2 = IndicatorWeights.from_array(np.array([0.4, 0.3, 0.2, 0.1]))
        
        child1, child2 = self.optimizer._crossover(parent1, parent2)
        
        self.assertIsInstance(child1, IndicatorWeights)
        self.assertIsInstance(child2, IndicatorWeights)
        
        # Check weights sum to 1
        self.assertAlmostEqual(np.sum(child1.to_array()), 1.0)
        self.assertAlmostEqual(np.sum(child2.to_array()), 1.0)
        
    def test_mutation(self):
        """Test mutation operation."""
        individual = IndicatorWeights.from_array(np.array([0.25] * 4))
        mutated = self.optimizer._mutate(individual)
        
        self.assertIsInstance(mutated, IndicatorWeights)
        self.assertAlmostEqual(np.sum(mutated.to_array()), 1.0)
        self.assertTrue(all(w >= 0 for w in mutated.to_array()))
        
    def test_optimization(self):
        """Test full optimization process."""
        best_weights = self.optimizer.optimize(
            self.market_data,
            lookback_window=50
        )
        
        self.assertIsInstance(best_weights, IndicatorWeights)
        self.assertAlmostEqual(np.sum(best_weights.to_array()), 1.0)
        self.assertTrue(all(w >= 0 for w in best_weights.to_array()))
        self.assertTrue(best_weights.fitness > float('-inf'))
        
    def test_parallel_fitness(self):
        """Test parallel fitness calculation."""
        population = self.optimizer._initialize_population()
        weight_arrays = np.array([p.to_array() for p in population])
        
        fitness_values = self.optimizer.strategy.run(
            self.optimizer._parallel_fitness,
            args=(weight_arrays, self.market_data, self.market_data['close'].pct_change())
        )
        
        self.assertEqual(len(fitness_values), self.optimizer.population_size)
        self.assertTrue(all(isinstance(f, float) for f in fitness_values))

if __name__ == '__main__':
    unittest.main() 