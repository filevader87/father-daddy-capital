
from concurrent.futures import ProcessPoolExecutor
import numpy as np

class GeneticOptimizer:
    def _parallel_fitness_evaluation(self, population, data, symbol):
        """Evaluate fitness of entire population in parallel only when necessary."""
        if len(population) > 10:  # Use a threshold to prevent unnecessary overhead
            with ProcessPoolExecutor(max_workers=self.n_jobs) as executor:
                fitness_scores = list(executor.map(lambda p: self._evaluate_fitness(p, data, symbol), population))
        else:
            fitness_scores = [self._evaluate_fitness(p, data, symbol) for p in population]
        return fitness_scores
    