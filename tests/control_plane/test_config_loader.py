import unittest
import tempfile
import yaml
import os
from pathlib import Path
from src.config.execution.config_loader import ConfigLoader, ConfigurationError

class TestConfigLoader(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / 'test_config.yml'
        
        # Create a valid test configuration
        self.valid_config = {
            'monitoring': {
                'log_dir': 'logs',
                'log_level': 'INFO',
                'metrics_port': 8000,
                'prometheus_job': 'test_job',
                'alert_thresholds': {
                    'drawdown_percent': 10.0,
                    'slippage_percent': 0.5
                },
                'min_regime_confidence': 0.7,
                'max_drawdown_threshold': 0.15,
                'strategy_switch_cooldown': 300,
                'performance_alert_threshold': 0.1
            },
            'execution': {
                'max_retries': 3,
                'retry_delay_seconds': 1,
                'timeout_seconds': 30,
                'batch_size': 100,
                'max_orders_per_second': 10,
                'market_data_source': 'live',
                'order_types': ['market', 'limit']
            },
            'risk': {
                'max_position_size': 0.1,
                'max_leverage': 2.0,
                'max_drawdown': 0.15,
                'position_limits': {
                    'stock': 0.2,
                    'sector': 0.3
                },
                'stop_loss': {
                    'enabled': True,
                    'percent': 0.05
                },
                'take_profit': {
                    'enabled': True,
                    'percent': 0.1
                }
            },
            'portfolio': {
                'initial_cash': 1000000,
                'base_currency': 'USD',
                'rebalance': {
                    'enabled': True,
                    'frequency': 'daily'
                }
            },
            'market_data': {
                'providers': [
                    {
                        'name': 'test',
                        'type': 'rest',
                        'url': 'http://test:8080'
                    }
                ],
                'cache': {
                    'enabled': True,
                    'ttl_seconds': 60
                }
            },
            'reporting': {
                'enabled': True,
                'frequency': 'hourly',
                'metrics': ['latency', 'errors'],
                'formats': ['json'],
                'destinations': ['prometheus']
            }
        }
        
        # Write valid config to file
        with open(self.config_path, 'w') as f:
            yaml.dump(self.valid_config, f)
            
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir)
        
    def test_load_valid_config(self):
        """Test loading a valid configuration."""
        loader = ConfigLoader(self.config_path)
        self.assertIsNotNone(loader.monitoring)
        self.assertIsNotNone(loader.execution)
        self.assertIsNotNone(loader.risk)
        self.assertIsNotNone(loader.portfolio)
        self.assertIsNotNone(loader.market_data)
        self.assertIsNotNone(loader.reporting)
        
    def test_missing_required_field(self):
        """Test loading config with missing required field."""
        # Remove required field
        invalid_config = self.valid_config.copy()
        del invalid_config['monitoring']['log_dir']
        
        # Write invalid config
        with open(self.config_path, 'w') as f:
            yaml.dump(invalid_config, f)
            
        with self.assertRaises(ConfigurationError):
            ConfigLoader(self.config_path)
            
    def test_invalid_values(self):
        """Test loading config with invalid values."""
        # Set invalid values
        invalid_config = self.valid_config.copy()
        invalid_config['risk']['max_position_size'] = 2.0  # Should be <= 1
        
        # Write invalid config
        with open(self.config_path, 'w') as f:
            yaml.dump(invalid_config, f)
            
        loader = ConfigLoader(self.config_path)
        self.assertFalse(loader.validate())
        
    def test_reload_config(self):
        """Test reloading configuration."""
        loader = ConfigLoader(self.config_path)
        original_port = loader.monitoring.metrics_port
        
        # Modify config
        self.valid_config['monitoring']['metrics_port'] = 9000
        with open(self.config_path, 'w') as f:
            yaml.dump(self.valid_config, f)
            
        # Reload and verify
        loader.reload()
        self.assertEqual(loader.monitoring.metrics_port, 9000)
        self.assertNotEqual(loader.monitoring.metrics_port, original_port)
        
    def test_get_config(self):
        """Test getting complete configuration."""
        loader = ConfigLoader(self.config_path)
        config = loader.get_config()
        
        self.assertIn('monitoring', config)
        self.assertIn('execution', config)
        self.assertIn('risk', config)
        self.assertIn('portfolio', config)
        self.assertIn('market_data', config)
        self.assertIn('reporting', config)
        
    def test_validation_rules(self):
        """Test specific validation rules."""
        loader = ConfigLoader(self.config_path)
        
        # Test position size validation
        loader.risk.max_position_size = 1.5
        self.assertFalse(loader.validate())
        loader.risk.max_position_size = 0.1
        
        # Test leverage validation
        loader.risk.max_leverage = 0.5
        self.assertFalse(loader.validate())
        loader.risk.max_leverage = 2.0
        
        # Test initial cash validation
        loader.portfolio.initial_cash = -1000
        self.assertFalse(loader.validate())
        loader.portfolio.initial_cash = 1000000
        
        # Test market data source validation
        loader.execution.market_data_source = 'invalid'
        self.assertFalse(loader.validate())
        loader.execution.market_data_source = 'live'
        
        # Final validation should pass
        self.assertTrue(loader.validate())

if __name__ == '__main__':
    unittest.main() 