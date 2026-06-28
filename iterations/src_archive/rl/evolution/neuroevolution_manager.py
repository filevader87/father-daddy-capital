import random
import uuid

class NeuroEvolutionManager:
    def __init__(self, agent_class, actions, population_size=5):
        self.agent_class = agent_class
        self.actions = actions
        self.population_size = population_size
        self.population = [self._create_random_agent(generation=0) for _ in range(population_size)]

    def _create_random_agent(self, parent_id=None, generation=0):
        return self.agent_class(
            actions=self.actions,
            alpha=random.uniform(0.05, 0.2),
            gamma=random.uniform(0.85, 0.99),
            epsilon=random.uniform(0.1, 1.0),
            epsilon_decay=random.uniform(0.98, 0.999),
            adaptive=True,
            agent_id=str(uuid.uuid4()),
            parent_id=parent_id,
            generation=generation
        )

    def evolve(self, reward_scores):
        ranked = sorted(zip(self.population, reward_scores), key=lambda x: x[1], reverse=True)
        top_half = [agent for agent, _ in ranked[:len(ranked)//2]]

        new_population = []
        generation = top_half[0].generation + 1 if top_half else 0
        for _ in range(self.population_size):
            parent = random.choice(top_half)
            child = self._create_random_agent(parent_id=parent.agent_id, generation=generation)
            new_population.append(child)

        self.population = new_population
        return self.population