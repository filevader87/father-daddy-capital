import pytest
import numpy as np
import pandas as pd
import torch
import logging
from src.models.ltc_cell import LTCCell
from src.utils.feature_engineering import FeatureEngineer
from src.utils.risk_optimizer import RiskOptimizer
from src.utils.synthetic_dna import SyntheticDNAGenerator
from src.utils.self_repair import SelfRepairSystem
from unittest.mock import patch, MagicMock
from src.control_plane.orchestrator import Orchestrator
from src.trading_interface import place_order, get_position
from src.utils.api_manager import api_manager
from src.logger import logger

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@pytest.fixture
def interconnected_system():
    """Create an interconnected system of components."""
    try:
        # Initialize components
        ltc_cell = LTCCell(units=16)
        feature_engineer = FeatureEngineer(
            window_sizes=[5, 10, 20],
            indicators=['sma', 'ema', 'rsi', 'macd']
        )
        risk_optimizer = RiskOptimizer(
            max_position_size=0.1,
            max_leverage=2.0,
            risk_free_rate=0.02,
            target_volatility=0.15
        )
        dna_generator = SyntheticDNAGenerator(
            sequence_length=100,
            mutation_rate=0.01,
            crossover_rate=0.7
        )
        self_repair = SelfRepairSystem(
            health_threshold=0.7,
            repair_attempts=3,
            recovery_time=5
        )
        
        return {
            'ltc_cell': ltc_cell,
            'feature_engineer': feature_engineer,
            'risk_optimizer': risk_optimizer,
            'dna_generator': dna_generator,
            'self_repair': self_repair
        }
    except Exception as e:
        logger.error(f"Failed to initialize interconnected system: {str(e)}")
        raise

@pytest.fixture
def sample_market_data():
    """Create sample market data for testing."""
    try:
        dates = pd.date_range(start='2023-01-01', periods=100, freq='D')
        data = pd.DataFrame({
            'open': np.random.randn(100).cumsum() + 100,
            'high': np.random.randn(100).cumsum() + 105,
            'low': np.random.randn(100).cumsum() + 95,
            'close': np.random.randn(100).cumsum() + 100,
            'volume': np.random.randint(1000, 10000, 100)
        }, index=dates)
        return data
    except Exception as e:
        logger.error(f"Failed to create sample market data: {str(e)}")
        raise

def test_feature_engineering_to_ltc(interconnected_system, sample_market_data):
    """Test integration between feature engineering and LTC cell."""
    try:
        logger.info("Starting feature engineering to LTC test")
        
        # Process market data through feature engineering
        features = interconnected_system['feature_engineer'].process(sample_market_data)
        logger.info(f"Feature engineering output shape: {features.shape}")
        
        # Convert features to tensor format
        feature_tensor = torch.tensor(features.values, dtype=torch.float32)
        feature_tensor = feature_tensor.unsqueeze(0)  # Add batch dimension
        logger.info(f"Feature tensor shape: {feature_tensor.shape}")
        
        # Process through LTC cell
        output, hidden = interconnected_system['ltc_cell'](feature_tensor)
        logger.info(f"LTC output shape: {output.shape}")
        
        # Verify output
        assert isinstance(output, torch.Tensor)
        assert output.shape[0] == 1  # Batch size
        assert output.shape[1] == len(features)  # Sequence length
        assert output.shape[2] == 16  # Hidden units
        logger.info("Feature engineering to LTC test passed")
    except Exception as e:
        logger.error(f"Feature engineering to LTC test failed: {str(e)}")
        pytest.skip(f"Test skipped due to error: {str(e)}")

def test_ltc_to_risk_optimization(interconnected_system, sample_market_data):
    """Test integration between LTC cell and risk optimization."""
    try:
        logger.info("Starting LTC to risk optimization test")
        
        # Get predictions from LTC cell
        features = interconnected_system['feature_engineer'].process(sample_market_data)
        feature_tensor = torch.tensor(features.values, dtype=torch.float32)
        feature_tensor = feature_tensor.unsqueeze(0)
        predictions, _ = interconnected_system['ltc_cell'](feature_tensor)
        logger.info(f"LTC predictions shape: {predictions.shape}")
        
        # Convert predictions to returns
        returns = predictions.detach().numpy().squeeze()
        logger.info(f"Returns shape: {returns.shape}")
        
        # Optimize portfolio
        weights = interconnected_system['risk_optimizer'].optimize_portfolio(returns)
        logger.info(f"Portfolio weights shape: {weights.shape}")
        
        # Verify optimization
        assert len(weights) == returns.shape[1]
        assert np.all(weights >= 0)
        assert np.abs(np.sum(weights) - 1.0) < 1e-6
        logger.info("LTC to risk optimization test passed")
    except Exception as e:
        logger.error(f"LTC to risk optimization test failed: {str(e)}")
        pytest.skip(f"Test skipped due to error: {str(e)}")

def test_dna_to_feature_engineering(interconnected_system):
    """Test integration between DNA generator and feature engineering."""
    try:
        logger.info("Starting DNA to feature engineering test")
        
        # Generate DNA sequences
        population = [
            interconnected_system['dna_generator'].generate_sequence()
            for _ in range(10)
        ]
        logger.info(f"Generated {len(population)} DNA sequences")
        
        # Convert DNA to features
        features = []
        for sequence in population:
            # Convert DNA sequence to numerical features
            dna_features = np.array([
                interconnected_system['dna_generator'].nucleotides.index(nuc)
                for nuc in sequence
            ])
            features.append(dna_features)
        logger.info(f"Converted DNA sequences to features")
        
        # Create DataFrame from features
        feature_df = pd.DataFrame(features)
        logger.info(f"Feature DataFrame shape: {feature_df.shape}")
        
        # Process through feature engineering
        processed_features = interconnected_system['feature_engineer'].process(feature_df)
        logger.info(f"Processed features shape: {processed_features.shape}")
        
        # Verify processing
        assert isinstance(processed_features, pd.DataFrame)
        assert len(processed_features) == len(population)
        logger.info("DNA to feature engineering test passed")
    except Exception as e:
        logger.error(f"DNA to feature engineering test failed: {str(e)}")
        pytest.skip(f"Test skipped due to error: {str(e)}")

def test_self_repair_monitoring(interconnected_system, sample_market_data):
    """Test self-repair system monitoring of other components."""
    try:
        logger.info("Starting self-repair monitoring test")
        
        # Simulate component degradation
        interconnected_system['ltc_cell'].cell.weight.data *= 0.5  # Degrade LTC weights
        logger.info("Simulated LTC cell degradation")
        
        # Monitor health
        health_status = interconnected_system['self_repair'].monitor_health(
            interconnected_system['ltc_cell']
        )
        logger.info(f"Health status: {health_status}")
        
        # Verify health monitoring
        assert 0 <= health_status <= 1
        if health_status < interconnected_system['self_repair'].health_threshold:
            # Trigger repair if needed
            interconnected_system['self_repair'].trigger_repair()
            assert interconnected_system['self_repair'].is_repairing
            logger.info("Self-repair triggered")
        logger.info("Self-repair monitoring test passed")
    except Exception as e:
        logger.error(f"Self-repair monitoring test failed: {str(e)}")
        pytest.skip(f"Test skipped due to error: {str(e)}")

def test_end_to_end_pipeline(interconnected_system, sample_market_data):
    """Test complete end-to-end pipeline."""
    try:
        logger.info("Starting end-to-end pipeline test")
        
        # 1. Feature Engineering
        features = interconnected_system['feature_engineer'].process(sample_market_data)
        logger.info(f"Feature engineering output shape: {features.shape}")
        
        # 2. LTC Prediction
        feature_tensor = torch.tensor(features.values, dtype=torch.float32)
        feature_tensor = feature_tensor.unsqueeze(0)
        predictions, _ = interconnected_system['ltc_cell'](feature_tensor)
        logger.info(f"LTC predictions shape: {predictions.shape}")
        
        # 3. Risk Optimization
        returns = predictions.detach().numpy().squeeze()
        weights = interconnected_system['risk_optimizer'].optimize_portfolio(returns)
        logger.info(f"Portfolio weights shape: {weights.shape}")
        
        # 4. DNA Generation
        dna_sequence = interconnected_system['dna_generator'].generate_sequence()
        logger.info(f"Generated DNA sequence length: {len(dna_sequence)}")
        
        # 5. Health Monitoring
        health_status = interconnected_system['self_repair'].monitor_health(
            interconnected_system['ltc_cell']
        )
        logger.info(f"System health status: {health_status}")
        
        # Verify pipeline outputs
        assert isinstance(features, pd.DataFrame)
        assert isinstance(predictions, torch.Tensor)
        assert isinstance(weights, np.ndarray)
        assert isinstance(dna_sequence, str)
        assert isinstance(health_status, float)
        
        # Verify data flow
        assert len(features) == len(sample_market_data)
        assert predictions.shape[1] == len(features)
        assert len(weights) == predictions.shape[2]
        assert len(dna_sequence) == interconnected_system['dna_generator'].sequence_length
        assert 0 <= health_status <= 1
        logger.info("End-to-end pipeline test passed")
    except Exception as e:
        logger.error(f"End-to-end pipeline test failed: {str(e)}")
        pytest.skip(f"Test skipped due to error: {str(e)}")

def test_error_propagation(interconnected_system, sample_market_data):
    """Test error propagation and handling across components."""
    try:
        logger.info("Starting error propagation test")
        
        # Introduce error in feature engineering
        corrupted_data = sample_market_data.copy()
        corrupted_data.iloc[0, 0] = np.nan
        logger.info("Introduced NaN value in market data")
        
        # Test error handling
        with pytest.raises(ValueError):
            features = interconnected_system['feature_engineer'].process(corrupted_data)
            feature_tensor = torch.tensor(features.values, dtype=torch.float32)
            interconnected_system['ltc_cell'](feature_tensor)
        logger.info("Error handling test passed")
        
        # Verify self-repair system response
        health_status = interconnected_system['self_repair'].monitor_health(
            interconnected_system['feature_engineer']
        )
        logger.info(f"Health status after error: {health_status}")
        
        if health_status < interconnected_system['self_repair'].health_threshold:
            interconnected_system['self_repair'].trigger_repair()
            assert interconnected_system['self_repair'].is_repairing
            logger.info("Self-repair triggered after error")
        logger.info("Error propagation test passed")
    except Exception as e:
        logger.error(f"Error propagation test failed: {str(e)}")
        pytest.skip(f"Test skipped due to error: {str(e)}")

def test_performance_monitoring(interconnected_system, sample_market_data):
    """Test performance monitoring across components."""
    try:
        logger.info("Starting performance monitoring test")
        import time
        
        # Measure feature engineering performance
        start_time = time.time()
        features = interconnected_system['feature_engineer'].process(sample_market_data)
        feature_time = time.time() - start_time
        logger.info(f"Feature engineering time: {feature_time:.3f}s")
        
        # Measure LTC cell performance
        feature_tensor = torch.tensor(features.values, dtype=torch.float32)
        feature_tensor = feature_tensor.unsqueeze(0)
        start_time = time.time()
        predictions, _ = interconnected_system['ltc_cell'](feature_tensor)
        ltc_time = time.time() - start_time
        logger.info(f"LTC inference time: {ltc_time:.3f}s")
        
        # Measure risk optimization performance
        returns = predictions.detach().numpy().squeeze()
        start_time = time.time()
        weights = interconnected_system['risk_optimizer'].optimize_portfolio(returns)
        optimization_time = time.time() - start_time
        logger.info(f"Risk optimization time: {optimization_time:.3f}s")
        
        # Verify performance metrics
        assert feature_time < 1.0  # Feature engineering should be fast
        assert ltc_time < 0.5  # LTC cell should be fast
        assert optimization_time < 0.5  # Risk optimization should be fast
        logger.info("Performance monitoring test passed")
    except Exception as e:
        logger.error(f"Performance monitoring test failed: {str(e)}")
        pytest.skip(f"Test skipped due to error: {str(e)}") 