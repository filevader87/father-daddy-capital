from setuptools import setup, find_packages

setup(
    name="trading_system",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "numpy>=1.21.0",
        "pandas>=1.3.0",
        "prometheus_client>=0.12.0",
        "pyyaml>=5.4.1",
        "structlog>=21.1.0",
        "websockets>=10.0",
        "aiohttp>=3.8.0",
        "pytest>=6.2.5",
        "pytest-asyncio>=0.16.0",
    ],
    extras_require={
        "dev": [
            "black",
            "flake8",
            "mypy",
            "pytest-cov",
        ]
    },
    python_requires=">=3.8",
    author="Your Name",
    author_email="your.email@example.com",
    description="A high-performance trading system with market regime detection",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Financial and Insurance Industry",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
    ],
) 