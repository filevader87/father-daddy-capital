import pytest
import numpy as np
import pandas as pd
from src.utils.feature_engineering import FeatureEngineer

@pytest.fixture
def sample_data():
    """Create sample market data for testing."""
    dates = pd.date_range(start='2023-01-01', periods=100, freq='D')
    data = pd.DataFrame({
        'open': np.random.randn(100).cumsum() + 100,
        'high': np.random.randn(100).cumsum() + 105,
        'low': np.random.randn(100).cumsum() + 95,
        'close': np.random.randn(100).cumsum() + 100,
        'volume': np.random.randint(1000, 10000, 100)
    }, index=dates)
    return data

@pytest.fixture
def feature_engineer():
    """Create a FeatureEngineer instance."""
    return FeatureEngineer(
        window_sizes=[5, 10, 20],
        indicators=['sma', 'ema', 'rsi', 'macd']
    )

def test_feature_engineer_initialization(feature_engineer):
    """Test FeatureEngineer initialization."""
    assert len(feature_engineer.window_sizes) == 3
    assert len(feature_engineer.indicators) == 4
    assert 'sma' in feature_engineer.indicators
    assert 'ema' in feature_engineer.indicators

def test_technical_indicators(feature_engineer, sample_data):
    """Test calculation of technical indicators."""
    features = feature_engineer.calculate_technical_indicators(sample_data)
    
    # Check SMA
    assert 'sma_5' in features.columns
    assert 'sma_10' in features.columns
    assert 'sma_20' in features.columns
    
    # Check EMA
    assert 'ema_5' in features.columns
    assert 'ema_10' in features.columns
    assert 'ema_20' in features.columns
    
    # Check RSI
    assert 'rsi_5' in features.columns
    assert 'rsi_10' in features.columns
    assert 'rsi_20' in features.columns
    
    # Check MACD
    assert 'macd' in features.columns
    assert 'macd_signal' in features.columns
    assert 'macd_hist' in features.columns

def test_feature_normalization(feature_engineer, sample_data):
    """Test feature normalization."""
    features = feature_engineer.calculate_technical_indicators(sample_data)
    normalized_features = feature_engineer.normalize_features(features)
    
    # Check normalization
    for col in normalized_features.columns:
        assert np.abs(normalized_features[col].mean()) < 1e-6  # Mean close to 0
        assert np.abs(normalized_features[col].std() - 1) < 1e-6  # Std close to 1

def test_feature_engineering_pipeline(feature_engineer, sample_data):
    """Test complete feature engineering pipeline."""
    processed_data = feature_engineer.process(sample_data)
    
    # Check output shape
    assert isinstance(processed_data, pd.DataFrame)
    assert len(processed_data) == len(sample_data)
    assert len(processed_data.columns) > len(sample_data.columns)
    
    # Check for NaN values
    assert not processed_data.isna().any().any()

def test_custom_indicator_handling(feature_engineer, sample_data):
    """Test handling of custom indicators."""
    # Add custom indicator
    def custom_indicator(data):
        return data['close'] / data['open']
    
    feature_engineer.add_custom_indicator('custom_ratio', custom_indicator)
    
    # Process data
    processed_data = feature_engineer.process(sample_data)
    
    # Check custom indicator
    assert 'custom_ratio' in processed_data.columns
    assert not processed_data['custom_ratio'].isna().any()

def test_missing_data_handling(feature_engineer):
    """Test handling of missing data."""
    # Create data with missing values
    dates = pd.date_range(start='2023-01-01', periods=100, freq='D')
    data = pd.DataFrame({
        'open': np.random.randn(100).cumsum() + 100,
        'high': np.random.randn(100).cumsum() + 105,
        'low': np.random.randn(100).cumsum() + 95,
        'close': np.random.randn(100).cumsum() + 100,
        'volume': np.random.randint(1000, 10000, 100)
    }, index=dates)
    
    # Introduce missing values
    data.loc[10:20, 'close'] = np.nan
    data.loc[30:40, 'volume'] = np.nan
    
    # Process data
    processed_data = feature_engineer.process(data)
    
    # Check handling of missing values
    assert not processed_data.isna().any().any()

def test_different_window_sizes(feature_engineer, sample_data):
    """Test feature engineering with different window sizes."""
    # Test with various window sizes
    window_sizes = [3, 7, 14, 21, 50]
    feature_engineer.window_sizes = window_sizes
    
    # Process data
    processed_data = feature_engineer.process(sample_data)
    
    # Check indicators for each window size
    for window in window_sizes:
        assert f'sma_{window}' in processed_data.columns
        assert f'ema_{window}' in processed_data.columns
        assert f'rsi_{window}' in processed_data.columns

def test_indicator_parameter_variations(feature_engineer, sample_data):
    """Test feature engineering with different indicator parameters."""
    # Test RSI with different overbought/oversold levels
    feature_engineer.rsi_overbought = 80
    feature_engineer.rsi_oversold = 20
    
    # Process data
    processed_data = feature_engineer.process(sample_data)
    
    # Check RSI values
    rsi_columns = [col for col in processed_data.columns if col.startswith('rsi_')]
    for col in rsi_columns:
        assert processed_data[col].min() >= 0
        assert processed_data[col].max() <= 100

def test_data_type_validation(feature_engineer):
    """Test data type validation."""
    # Test with invalid data types
    invalid_data = pd.DataFrame({
        'open': ['invalid'] * 100,
        'high': ['invalid'] * 100,
        'low': ['invalid'] * 100,
        'close': ['invalid'] * 100,
        'volume': ['invalid'] * 100
    })
    
    with pytest.raises(ValueError):
        feature_engineer.process(invalid_data)
    
    # Test with mixed data types
    mixed_data = pd.DataFrame({
        'open': np.random.randn(100),
        'high': np.random.randn(100),
        'low': np.random.randn(100),
        'close': np.random.randn(100),
        'volume': ['invalid'] * 100
    })
    
    with pytest.raises(ValueError):
        feature_engineer.process(mixed_data)

def test_performance_benchmark(feature_engineer, sample_data):
    """Test feature engineering performance."""
    import time
    
    # Measure processing time
    start_time = time.time()
    processed_data = feature_engineer.process(sample_data)
    end_time = time.time()
    
    # Check if processing time is reasonable
    processing_time = end_time - start_time
    assert processing_time < 1.0  # Should process within 1 second
    
    # Check memory usage
    memory_usage = processed_data.memory_usage(deep=True).sum()
    assert memory_usage < 1024 * 1024 * 100  # Should use less than 100MB 