import pytest
import numpy as np
from src.utils.synthetic_dna import SyntheticDNAGenerator

@pytest.fixture
def dna_generator():
    """Create a SyntheticDNAGenerator instance."""
    return SyntheticDNAGenerator(
        sequence_length=100,
        mutation_rate=0.01,
        crossover_rate=0.7
    )

def test_dna_generator_initialization(dna_generator):
    """Test SyntheticDNAGenerator initialization."""
    assert dna_generator.sequence_length == 100
    assert dna_generator.mutation_rate == 0.01
    assert dna_generator.crossover_rate == 0.7
    assert len(dna_generator.nucleotides) == 4

def test_sequence_generation(dna_generator):
    """Test DNA sequence generation."""
    sequence = dna_generator.generate_sequence()
    
    # Check sequence properties
    assert len(sequence) == dna_generator.sequence_length
    assert all(nuc in dna_generator.nucleotides for nuc in sequence)
    assert isinstance(sequence, str)

def test_mutation(dna_generator):
    """Test DNA mutation."""
    original_sequence = dna_generator.generate_sequence()
    mutated_sequence = dna_generator.mutate(original_sequence)
    
    # Check mutation properties
    assert len(mutated_sequence) == len(original_sequence)
    assert all(nuc in dna_generator.nucleotides for nuc in mutated_sequence)
    
    # Count differences
    differences = sum(1 for a, b in zip(original_sequence, mutated_sequence) if a != b)
    assert differences > 0  # Should have some mutations

def test_crossover(dna_generator):
    """Test DNA crossover."""
    parent1 = dna_generator.generate_sequence()
    parent2 = dna_generator.generate_sequence()
    child1, child2 = dna_generator.crossover(parent1, parent2)
    
    # Check crossover properties
    assert len(child1) == len(parent1)
    assert len(child2) == len(parent2)
    assert all(nuc in dna_generator.nucleotides for nuc in child1)
    assert all(nuc in dna_generator.nucleotides for nuc in child2)
    
    # Check inheritance
    assert any(a == b for a, b in zip(child1, parent1))
    assert any(a == b for a, b in zip(child1, parent2))

def test_fitness_calculation(dna_generator):
    """Test fitness calculation."""
    sequence = dna_generator.generate_sequence()
    fitness = dna_generator.calculate_fitness(sequence)
    
    # Check fitness properties
    assert isinstance(fitness, float)
    assert 0 <= fitness <= 1

def test_population_evolution(dna_generator):
    """Test population evolution."""
    population_size = 10
    generations = 5
    
    # Initialize population
    population = [dna_generator.generate_sequence() for _ in range(population_size)]
    
    # Evolve population
    evolved_population = dna_generator.evolve_population(population, generations)
    
    # Check evolution properties
    assert len(evolved_population) == population_size
    assert all(len(seq) == dna_generator.sequence_length for seq in evolved_population)
    assert all(all(nuc in dna_generator.nucleotides for nuc in seq) for seq in evolved_population)

def test_diversity_measurement(dna_generator):
    """Test population diversity measurement."""
    population = [
        dna_generator.generate_sequence() for _ in range(5)
    ]
    
    diversity = dna_generator.measure_diversity(population)
    
    # Check diversity properties
    assert isinstance(diversity, float)
    assert 0 <= diversity <= 1 