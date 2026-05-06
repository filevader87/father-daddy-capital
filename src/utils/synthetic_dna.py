import numpy as np
from typing import List, Tuple, Optional
import random

class SyntheticDNAGenerator:
    """Generates synthetic DNA sequences for genetic algorithms."""
    
    def __init__(
        self,
        sequence_length: int = 100,
        mutation_rate: float = 0.01,
        crossover_rate: float = 0.7,
        population_size: int = 100,
        nucleotides: List[str] = ['A', 'C', 'G', 'T']
    ):
        """
        Initialize the synthetic DNA generator.
        
        Args:
            sequence_length: Length of DNA sequences to generate
            mutation_rate: Probability of mutation per nucleotide
            crossover_rate: Probability of crossover between sequences
            population_size: Size of the population to maintain
            nucleotides: List of valid nucleotides
        """
        self.sequence_length = sequence_length
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.population_size = population_size
        self.nucleotides = nucleotides
        
        # Validate inputs
        self._validate_inputs()
        
        # Initialize population
        self.population = [
            self.generate_sequence()
            for _ in range(population_size)
        ]
    
    def _validate_inputs(self):
        """Validate initialization parameters."""
        if self.sequence_length <= 0:
            raise ValueError("sequence_length must be > 0")
        if not 0 <= self.mutation_rate <= 1:
            raise ValueError("mutation_rate must be between 0 and 1")
        if not 0 <= self.crossover_rate <= 1:
            raise ValueError("crossover_rate must be between 0 and 1")
        if self.population_size <= 0:
            raise ValueError("population_size must be > 0")
        if not self.nucleotides:
            raise ValueError("nucleotides list cannot be empty")
    
    def generate_sequence(self) -> str:
        """Generate a random DNA sequence."""
        return ''.join(random.choice(self.nucleotides) for _ in range(self.sequence_length))
    
    def mutate(self, sequence: str) -> str:
        """Apply random mutations to a sequence."""
        sequence_list = list(sequence)
        mutated = False
        for i in range(len(sequence_list)):
            if random.random() < self.mutation_rate:
                choices = [n for n in self.nucleotides if n != sequence_list[i]]
                sequence_list[i] = random.choice(choices)
                mutated = True
        if not mutated and sequence_list:
            i = random.randrange(len(sequence_list))
            choices = [n for n in self.nucleotides if n != sequence_list[i]]
            sequence_list[i] = random.choice(choices)
        return ''.join(sequence_list)
    
    def crossover(self, sequence1: str, sequence2: str) -> Tuple[str, str]:
        """Perform crossover between two sequences."""
        if random.random() < self.crossover_rate:
            point = random.randint(1, len(sequence1) - 1)
            offspring1 = sequence1[:point] + sequence2[point:]
            offspring2 = sequence2[:point] + sequence1[point:]
            return offspring1, offspring2
        return sequence1, sequence2
    
    def evolve_population(self, population_or_fitness_func, generations: Optional[int] = None) -> List[str]:
        """
        Evolve the population using genetic algorithm.
        
        Args:
            fitness_func: Function that takes a sequence and returns its fitness score
        
        Returns:
            List[str]: New population after evolution
        """
        if callable(population_or_fitness_func):
            fitness_func = population_or_fitness_func
            generations = 1 if generations is None else generations
        else:
            self.population = list(population_or_fitness_func)
            self.population_size = len(self.population)
            fitness_func = self.calculate_fitness
            generations = 1 if generations is None else generations

        for _ in range(generations):
            self.population = self._evolve_one_generation(fitness_func)
        return self.population

    def _evolve_one_generation(self, fitness_func) -> List[str]:
        # Calculate fitness for each sequence
        fitness_scores = [fitness_func(seq) for seq in self.population]
        
        # Select parents using tournament selection
        def tournament_select():
            tournament_size = 3
            tournament = random.sample(list(enumerate(fitness_scores)), tournament_size)
            winner = max(tournament, key=lambda x: x[1])[0]
            return self.population[winner]
        
        # Create new population
        new_population = []
        elite_count = max(1, self.population_size // 20)  # Keep top 5%
        
        # Add elite sequences
        sorted_indices = np.argsort(fitness_scores)[-elite_count:]
        new_population.extend(self.population[i] for i in sorted_indices)
        
        # Fill rest of population with offspring
        while len(new_population) < self.population_size:
            parent1 = tournament_select()
            parent2 = tournament_select()
            
            offspring1, offspring2 = self.crossover(parent1, parent2)
            offspring1 = self.mutate(offspring1)
            offspring2 = self.mutate(offspring2)
            
            new_population.append(offspring1)
            if len(new_population) < self.population_size:
                new_population.append(offspring2)
        
        self.population = new_population
        return new_population

    def calculate_fitness(self, sequence: str) -> float:
        """Calculate simple normalized fitness from nucleotide balance."""
        if not sequence:
            return 0.0
        counts = np.array([sequence.count(nuc) for nuc in self.nucleotides], dtype=float)
        expected = len(sequence) / len(self.nucleotides)
        imbalance = np.mean(np.abs(counts - expected)) / expected
        return float(max(0.0, min(1.0, 1.0 - imbalance)))

    def measure_diversity(self, population: List[str]) -> float:
        """Measure normalized mean pairwise Hamming distance."""
        if len(population) < 2:
            return 0.0

        total = 0
        count = 0
        for i in range(len(population)):
            for j in range(i + 1, len(population)):
                total += sum(a != b for a, b in zip(population[i], population[j])) / max(1, len(population[i]))
                count += 1
        return float(total / count)
    
    def get_best_sequence(self, fitness_func) -> Tuple[str, float]:
        """Get the best sequence in the population."""
        fitness_scores = [fitness_func(seq) for seq in self.population]
        best_idx = np.argmax(fitness_scores)
        return self.population[best_idx], fitness_scores[best_idx]
    
    def calculate_diversity(self) -> float:
        """Calculate population diversity."""
        if not self.population:
            return 0.0
        
        def hamming_distance(seq1, seq2):
            return sum(a != b for a, b in zip(seq1, seq2))
        
        total_distance = 0
        count = 0
        for i in range(len(self.population)):
            for j in range(i + 1, len(self.population)):
                total_distance += hamming_distance(
                    self.population[i],
                    self.population[j]
                )
                count += 1
        
        return total_distance / count if count > 0 else 0.0
    
    def get_population_stats(self) -> dict:
        """Get statistics about the current population."""
        diversity = self.calculate_diversity()
        nucleotide_freq = {nuc: 0 for nuc in self.nucleotides}
        
        for sequence in self.population:
            for nuc in sequence:
                nucleotide_freq[nuc] += 1
        
        total_nucs = self.sequence_length * self.population_size
        nucleotide_freq = {
            nuc: count / total_nucs
            for nuc, count in nucleotide_freq.items()
        }
        
        return {
            'diversity': diversity,
            'population_size': len(self.population),
            'sequence_length': self.sequence_length,
            'nucleotide_frequencies': nucleotide_freq
        } 
