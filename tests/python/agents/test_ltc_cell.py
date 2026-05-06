import pytest
import numpy as np
import torch
from src.models.ltc_cell import LTCCell
import tempfile
import os

def test_ltc_cell_initialization(ltc_cell):
    """Test LTC cell initialization."""
    assert ltc_cell.units == 16
    assert isinstance(ltc_cell.cell, torch.nn.Module)
    assert ltc_cell.cell.hidden_size == 16

def test_ltc_cell_forward_pass(ltc_cell):
    """Test LTC cell forward pass."""
    batch_size = 32
    sequence_length = 10
    input_size = 5
    
    # Create random input data
    x = torch.randn(batch_size, sequence_length, input_size)
    
    # Forward pass
    output, hidden = ltc_cell(x)
    
    # Check output shapes
    assert output.shape == (batch_size, sequence_length, ltc_cell.units)
    assert hidden.shape == (batch_size, ltc_cell.units)

def test_ltc_cell_gradients(ltc_cell):
    """Test LTC cell gradient computation."""
    batch_size = 32
    sequence_length = 10
    input_size = 5
    
    # Create random input data
    x = torch.randn(batch_size, sequence_length, input_size)
    
    # Forward pass
    output, _ = ltc_cell(x)
    
    # Compute loss and backward pass
    loss = output.sum()
    loss.backward()
    
    # Check gradients
    for param in ltc_cell.cell.parameters():
        assert param.grad is not None
        assert not torch.isnan(param.grad).any()

def test_ltc_cell_state_management(ltc_cell):
    """Test LTC cell state management."""
    batch_size = 32
    sequence_length = 10
    input_size = 5
    
    # Create random input data
    x = torch.randn(batch_size, sequence_length, input_size)
    
    # Forward pass with initial state
    initial_state = torch.zeros(batch_size, ltc_cell.units)
    output, hidden = ltc_cell(x, initial_state)
    
    # Check state propagation
    assert hidden.shape == (batch_size, ltc_cell.units)
    assert not torch.equal(hidden, initial_state)  # State should have changed

def test_ltc_cell_device_handling(ltc_cell):
    """Test LTC cell device handling."""
    if torch.cuda.is_available():
        # Move to GPU
        ltc_cell.cell = ltc_cell.cell.cuda()
        
        # Create input on GPU
        x = torch.randn(32, 10, 5).cuda()
        
        # Forward pass
        output, hidden = ltc_cell(x)
        
        # Check device
        assert output.device.type == 'cuda'
        assert hidden.device.type == 'cuda'

def test_ltc_cell_edge_cases(ltc_cell):
    """Test LTC cell edge cases."""
    # Test empty sequence
    x_empty = torch.randn(32, 0, 5)
    with pytest.raises(ValueError):
        ltc_cell(x_empty)
    
    # Test single-element sequence
    x_single = torch.randn(32, 1, 5)
    output, hidden = ltc_cell(x_single)
    assert output.shape == (32, 1, ltc_cell.units)
    assert hidden.shape == (32, ltc_cell.units)

def test_ltc_cell_model_saving(ltc_cell):
    """Test LTC cell model saving and loading."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Save model
        model_path = os.path.join(tmpdir, 'ltc_cell.pt')
        ltc_cell.save(model_path)
        assert os.path.exists(model_path)
        
        # Load model
        new_ltc_cell = LTCCell(units=16)
        new_ltc_cell.load(model_path)
        
        # Verify loaded model
        x = torch.randn(32, 10, 5)
        output1, _ = ltc_cell(x)
        output2, _ = new_ltc_cell(x)
        assert torch.allclose(output1, output2)

def test_ltc_cell_different_input_sizes(ltc_cell):
    """Test LTC cell with different input sizes."""
    # Test various batch sizes
    for batch_size in [1, 16, 64, 128]:
        x = torch.randn(batch_size, 10, 5)
        output, hidden = ltc_cell(x)
        assert output.shape == (batch_size, 10, ltc_cell.units)
        assert hidden.shape == (batch_size, ltc_cell.units)
    
    # Test various sequence lengths
    for seq_length in [1, 5, 20, 50]:
        x = torch.randn(32, seq_length, 5)
        output, hidden = ltc_cell(x)
        assert output.shape == (32, seq_length, ltc_cell.units)
        assert hidden.shape == (32, ltc_cell.units)
    
    # Test various input dimensions
    for input_size in [1, 3, 10, 20]:
        x = torch.randn(32, 10, input_size)
        output, hidden = ltc_cell(x)
        assert output.shape == (32, 10, ltc_cell.units)
        assert hidden.shape == (32, ltc_cell.units)

def test_ltc_cell_error_handling(ltc_cell):
    """Test LTC cell error handling."""
    # Test invalid input shape
    x_invalid = torch.randn(32, 10)  # Missing last dimension
    with pytest.raises(ValueError):
        ltc_cell(x_invalid)
    
    # Test NaN input
    x_nan = torch.randn(32, 10, 5)
    x_nan[0, 0, 0] = float('nan')
    with pytest.raises(ValueError):
        ltc_cell(x_nan)
    
    # Test infinite input
    x_inf = torch.randn(32, 10, 5)
    x_inf[0, 0, 0] = float('inf')
    with pytest.raises(ValueError):
        ltc_cell(x_inf) 