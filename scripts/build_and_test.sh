#!/bin/bash

# Create necessary directories
mkdir -p logs data/state

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

# Run tests
echo "Running tests..."
pytest tests/ -v

# Check if tests passed
if [ $? -eq 0 ]; then
    echo "All tests passed successfully!"
else
    echo "Tests failed. Please check the errors above."
    exit 1
fi 