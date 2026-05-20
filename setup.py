from setuptools import setup, find_packages

setup(
    name="trading_system",
    version="0.1.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "numpy>=1.21.0",
        "pandas>=1.3.0",
        "torch>=2.0.0",
        "scikit-learn>=0.24.0",
        "pytest>=6.0.0",
        "pytest-asyncio>=0.15.0",
        "ta>=0.7.0",
        "cvxpy>=1.1.0",
        "scipy>=1.7.0",
        "matplotlib>=3.4.0",
        "seaborn>=0.11.0",
        "yfinance>=0.1.63",
        "prometheus-client>=0.12.0",
        "pyyaml>=5.4.1",

        "python-dotenv>=0.19.0",
        "requests>=2.25.0",
        "psutil>=5.8.0",
        "pytest-cov>=2.12.0",
        "black>=21.7b0",
        "flake8>=3.9.2",
        "mypy>=0.910",
        "types-requests>=2.25.0",
        "types-psutil>=5.8.0",
        "discord.py>=2.3.2",
        "solana>=0.26.0",
        "TA-Lib>=0.6.3"
    ],
    python_requires=">=3.8",
) 