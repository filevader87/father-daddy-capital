#!/usr/bin/env python3
"""
Repository Setup Script
-----------------------
Initializes the Father Daddy Capital repository with footprint reduction configuration.
This script helps set up the repository for efficient development and deployment.
"""

import os
import sys
import subprocess
import argparse
from pathlib import Path

def run_command(command, description):
    """Run a command and handle errors."""
    print(f"🔄 {description}...")
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ {description} completed successfully")
            if result.stdout.strip():
                print(f"   Output: {result.stdout.strip()}")
        else:
            print(f"❌ {description} failed")
            if result.stderr.strip():
                print(f"   Error: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"❌ {description} failed with exception: {e}")
        return False
    return True

def check_git_status():
    """Check if this is a git repository."""
    return run_command("git status", "Checking git repository status")

def initialize_git():
    """Initialize git repository if not already done."""
    if not Path(".git").exists():
        print("📁 Git repository not found. Initializing...")
        if not run_command("git init", "Initializing git repository"):
            return False
        if not run_command("git add .", "Adding files to git"):
            return False
        if not run_command('git commit -m "Initial commit with footprint reduction"', "Making initial commit"):
            return False
    else:
        print("✅ Git repository already exists")
    return True

def setup_git_lfs():
    """Set up Git LFS for large file handling."""
    print("📦 Setting up Git LFS...")
    
    # Check if git-lfs is installed
    if not run_command("git lfs version", "Checking Git LFS installation"):
        print("⚠️  Git LFS not found. Please install it:")
        print("   - Windows: https://git-lfs.github.com/")
        print("   - macOS: brew install git-lfs")
        print("   - Linux: sudo apt-get install git-lfs")
        return False
    
    # Install Git LFS
    if not run_command("git lfs install", "Installing Git LFS"):
        return False
    
    # Track large files
    if not run_command("git lfs track '*.joblib'", "Tracking joblib files"):
        return False
    if not run_command("git lfs track '*.pkl'", "Tracking pickle files"):
        return False
    if not run_command("git lfs track '*.h5'", "Tracking HDF5 files"):
        return False
    if not run_command("git lfs track '*.csv'", "Tracking CSV files"):
        return False
    if not run_command("git lfs track '*.parquet'", "Tracking parquet files"):
        return False
    
    return True

def create_directories():
    """Create necessary directories."""
    directories = [
        "archive",
        "config",
        "data/historical/crypto",
        "data/historical/stocks",
        "data/market_data",
        "data/trades",
        "logs",
        "models/trained",
        "state",
        "profiles"
    ]
    
    print("📁 Creating directories...")
    for directory in directories:
        Path(directory).mkdir(parents=True, exist_ok=True)
        print(f"   ✅ Created: {directory}")
    
    return True

def create_config_files():
    """Create default configuration files."""
    print("⚙️  Creating configuration files...")
    
    # Create artifact config
    artifact_config = {
        "models": {
            "local_path": "models/",
            "external_path": "s3://father-daddy-capital/models/",
            "file_patterns": ["*.joblib", "*.pkl", "*.h5", "*.pt"]
        },
        "data": {
            "local_path": "data/",
            "external_path": "s3://father-daddy-capital/data/",
            "file_patterns": ["*.csv", "*.json", "*.parquet"]
        },
        "logs": {
            "local_path": "logs/",
            "external_path": "s3://father-daddy-capital/logs/",
            "file_patterns": ["*.log", "*.json"]
        }
    }
    
    import json
    os.makedirs("config", exist_ok=True)
    with open("config/artifact_config.json", "w") as f:
        json.dump(artifact_config, f, indent=2)
    print("   ✅ Created: config/artifact_config.json")
    
    return True

def run_management_scripts():
    """Run the management scripts to set up the repository."""
    print("🔧 Running management scripts...")
    
    # Run artifact externalization
    if not run_command("python scripts/externalize_artifacts.py --action list", "Analyzing artifacts"):
        return False
    
    # Run data manager
    if not run_command("python scripts/data_manager.py --action lfs-config", "Setting up Git LFS config"):
        return False
    
    # Run archive manager
    if not run_command("python scripts/archive_manager.py --action list", "Analyzing archive candidates"):
        return False
    
    return True

def create_readme():
    """Create a README for the repository setup."""
    readme_content = """# Father Daddy Capital - Repository Setup

This repository has been configured with comprehensive footprint reduction to optimize development and deployment.

## Quick Start

1. **Clone the repository** (now much smaller!)
2. **Fetch data on-demand**: `python scripts/data_manager.py --action fetch-script`
3. **Download models**: `python scripts/externalize_artifacts.py --action download-script`

## Key Features

- ✅ **Small repository size**: Large files externalized
- ✅ **On-demand data**: Fetch only what you need
- ✅ **External storage**: S3 integration ready
- ✅ **Git LFS**: Large file handling configured
- ✅ **Automated management**: Scripts for all operations

## Management Scripts

- `scripts/externalize_artifacts.py` - Manage model artifacts
- `scripts/data_manager.py` - Manage historical data
- `scripts/archive_manager.py` - Archive old files
- `scripts/setup_repository.py` - This setup script

## External Storage

Large files are stored in S3:
- Models: `s3://father-daddy-capital/models/`
- Data: `s3://father-daddy-capital/data/`
- Logs: `s3://father-daddy-capital/logs/`

## Development Workflow

1. **Code changes**: Normal git workflow
2. **Data updates**: Use management scripts
3. **Model updates**: Upload to S3, update placeholders
4. **Cleanup**: Regular data cleanup with retention policies

For more details, see `REPOSITORY_FOOTPRINT_REDUCTION.md`
"""
    
    with open("REPOSITORY_SETUP.md", "w") as f:
        f.write(readme_content)
    print("   ✅ Created: REPOSITORY_SETUP.md")
    
    return True

def main():
    parser = argparse.ArgumentParser(description="Set up repository with footprint reduction")
    parser.add_argument("--skip-git", action="store_true", help="Skip git initialization")
    parser.add_argument("--skip-lfs", action="store_true", help="Skip Git LFS setup")
    
    args = parser.parse_args()
    
    print("🚀 Setting up Father Daddy Capital repository...")
    print("=" * 60)
    
    success = True
    
    # Create directories
    if not create_directories():
        success = False
    
    # Create config files
    if not create_config_files():
        success = False
    
    # Git setup
    if not args.skip_git:
        if not initialize_git():
            success = False
        
        if not args.skip_lfs:
            if not setup_git_lfs():
                success = False
    
    # Run management scripts
    if not run_management_scripts():
        success = False
    
    # Create documentation
    if not create_readme():
        success = False
    
    print("=" * 60)
    if success:
        print("🎉 Repository setup completed successfully!")
        print("\nNext steps:")
        print("1. Configure S3 credentials for external storage")
        print("2. Upload existing artifacts: scripts/upload_artifacts.sh")
        print("3. Test data fetching: scripts/fetch_data.sh")
        print("4. Review REPOSITORY_SETUP.md for usage instructions")
    else:
        print("❌ Repository setup encountered errors. Please review the output above.")
        sys.exit(1)

if __name__ == "__main__":
    main() 