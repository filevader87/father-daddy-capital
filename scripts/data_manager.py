#!/usr/bin/env python3
"""
Data Management Script
----------------------
Handles externalization and on-demand fetching of historical data files.
Supports data lake integration and Git LFS for large datasets.
"""

import os
import json
import shutil
import argparse
from pathlib import Path
from typing import Dict, List, Optional
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DataManager:
    def __init__(self, config_path: str = "config/data_config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        
    def _load_config(self) -> Dict:
        """Load data management configuration."""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                return json.load(f)
        else:
            return {
                "data_sources": {
                    "historical": {
                        "local_path": "data/historical/",
                        "external_path": "s3://father-daddy-capital/data/historical/",
                        "retention_days": 30,
                        "file_patterns": ["*.csv", "*.json", "*.parquet"]
                    },
                    "market_data": {
                        "local_path": "data/market_data/",
                        "external_path": "s3://father-daddy-capital/data/market_data/",
                        "retention_days": 7,
                        "file_patterns": ["*.csv", "*.json"]
                    },
                    "trades": {
                        "local_path": "data/trades/",
                        "external_path": "s3://father-daddy-capital/data/trades/",
                        "retention_days": 90,
                        "file_patterns": ["*.csv", "*.json"]
                    }
                },
                "compression": {
                    "enabled": True,
                    "format": "gzip",
                    "min_size_mb": 1.0
                }
            }
    
    def _save_config(self):
        """Save current configuration."""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def list_data_files(self, source: Optional[str] = None) -> List[Dict]:
        """List data files in specified source or all sources."""
        data_files = []
        
        sources = [source] if source else self.config["data_sources"].keys()
        
        for source_name in sources:
            if source_name not in self.config["data_sources"]:
                logger.warning(f"Unknown data source: {source_name}")
                continue
                
            source_config = self.config["data_sources"][source_name]
            local_path = Path(source_config["local_path"])
            
            if not local_path.exists():
                continue
                
            for pattern in source_config["file_patterns"]:
                for file_path in local_path.rglob(pattern):
                    size_mb = file_path.stat().st_size / (1024 * 1024)
                    modified_time = datetime.fromtimestamp(file_path.stat().st_mtime)
                    
                    data_files.append({
                        "path": str(file_path),
                        "size_mb": size_mb,
                        "source": source_name,
                        "modified": modified_time,
                        "relative_path": str(file_path.relative_to(Path.cwd())) if file_path.is_relative_to(Path.cwd()) else str(file_path)
                    })
        
        return sorted(data_files, key=lambda x: x["size_mb"], reverse=True)
    
    def cleanup_old_data(self, dry_run: bool = True) -> List[Dict]:
        """Remove old data files based on retention policy."""
        removed_files = []
        
        for source_name, source_config in self.config["data_sources"].items():
            retention_days = source_config["retention_days"]
            cutoff_date = datetime.now() - timedelta(days=retention_days)
            local_path = Path(source_config["local_path"])
            
            if not local_path.exists():
                continue
                
            for pattern in source_config["file_patterns"]:
                for file_path in local_path.rglob(pattern):
                    modified_time = datetime.fromtimestamp(file_path.stat().st_mtime)
                    
                    if modified_time < cutoff_date:
                        file_info = {
                            "path": str(file_path),
                            "size_mb": file_path.stat().st_size / (1024 * 1024),
                            "source": source_name,
                            "modified": modified_time,
                            "age_days": (datetime.now() - modified_time).days
                        }
                        
                        if not dry_run:
                            try:
                                file_path.unlink()
                                logger.info(f"Removed old file: {file_path}")
                            except Exception as e:
                                logger.error(f"Failed to remove {file_path}: {e}")
                                continue
                        
                        removed_files.append(file_info)
        
        return removed_files
    
    def create_data_fetch_script(self, symbols: List[str] = None, output_path: str = "scripts/fetch_data.sh"):
        """Create a script to fetch specific data on-demand."""
        if symbols is None:
            symbols = ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "RNDRUSD", "XRPUSD", "ADAUSD"]
        
        script_content = "#!/bin/bash\n"
        script_content += "# Auto-generated data fetch script\n"
        script_content += "# Run this script to fetch specific data on-demand\n\n"
        
        # Add data source setup
        script_content += "# Setup data directories\n"
        script_content += "mkdir -p data/historical/crypto\n"
        script_content += "mkdir -p data/historical/stocks\n"
        script_content += "mkdir -p data/market_data\n"
        script_content += "mkdir -p data/trades\n\n"
        
        # Add symbol-specific fetches
        for symbol in symbols:
            script_content += f"# Fetch data for {symbol}\n"
            script_content += f"aws s3 cp s3://father-daddy-capital/data/historical/crypto/{symbol}.csv data/historical/crypto/ || echo 'No historical data for {symbol}'\n"
            script_content += f"aws s3 cp s3://father-daddy-capital/data/market_data/{symbol}.json data/market_data/ || echo 'No market data for {symbol}'\n"
            script_content += f"aws s3 cp s3://father-daddy-capital/data/trades/{symbol}.json data/trades/ || echo 'No trade data for {symbol}'\n\n"
        
        script_content += "echo 'Data fetch complete!'\n"
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            f.write(script_content)
        
        # Make executable on Unix systems
        if os.name != 'nt':
            os.chmod(output_path, 0o755)
        
        logger.info(f"Data fetch script created: {output_path}")
        return output_path
    
    def create_git_lfs_config(self, output_path: str = ".gitattributes"):
        """Create Git LFS configuration for large files."""
        lfs_config = """# Git LFS configuration for large files
# This file tells Git which files should be handled by Git LFS

# Data files
*.csv filter=lfs diff=lfs merge=lfs -text
*.parquet filter=lfs diff=lfs merge=lfs -text
*.json filter=lfs diff=lfs merge=lfs -text
data/**/*.csv filter=lfs diff=lfs merge=lfs -text
data/**/*.json filter=lfs diff=lfs merge=lfs -text

# Model files
*.joblib filter=lfs diff=lfs merge=lfs -text
*.pkl filter=lfs diff=lfs merge=lfs -text
*.h5 filter=lfs diff=lfs merge=lfs -text
*.pt filter=lfs diff=lfs merge=lfs -text
models/**/* filter=lfs diff=lfs merge=lfs -text

# Log files (optional - uncomment if you want to track large logs)
# logs/**/*.log filter=lfs diff=lfs merge=lfs -text
# logs/**/*.json filter=lfs diff=lfs merge=lfs -text

# State files (optional - uncomment if you want to track state)
# state/**/*.json filter=lfs diff=lfs merge=lfs -text
"""
        
        with open(output_path, 'w') as f:
            f.write(lfs_config)
        
        logger.info(f"Git LFS configuration created: {output_path}")
        return output_path

def main():
    parser = argparse.ArgumentParser(description="Manage data files and externalization")
    parser.add_argument("--action", choices=["list", "cleanup", "fetch-script", "lfs-config"], 
                       default="list", help="Action to perform")
    parser.add_argument("--source", help="Specific data source to operate on")
    parser.add_argument("--dry-run", action="store_true", help="Dry run for cleanup")
    parser.add_argument("--symbols", nargs="+", help="Symbols for data fetch script")
    
    args = parser.parse_args()
    
    data_manager = DataManager()
    
    if args.action == "list":
        data_files = data_manager.list_data_files(args.source)
        print(f"\nData files:")
        print("-" * 80)
        for file_info in data_files:
            print(f"{file_info['relative_path']:<50} {file_info['size_mb']:>8.2f} MB ({file_info['source']})")
        print(f"\nTotal: {len(data_files)} files")
        
    elif args.action == "cleanup":
        removed_files = data_manager.cleanup_old_data(dry_run=args.dry_run)
        if args.dry_run:
            print(f"\nFiles that would be removed (dry run):")
        else:
            print(f"\nRemoved files:")
        print("-" * 80)
        for file_info in removed_files:
            print(f"{file_info['path']:<50} {file_info['size_mb']:>8.2f} MB ({file_info['age_days']} days old)")
        print(f"\nTotal: {len(removed_files)} files")
        
    elif args.action == "fetch-script":
        symbols = args.symbols or ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "RNDRUSD", "XRPUSD", "ADAUSD"]
        script_path = data_manager.create_data_fetch_script(symbols)
        print(f"Data fetch script created: {script_path}")
        
    elif args.action == "lfs-config":
        config_path = data_manager.create_git_lfs_config()
        print(f"Git LFS configuration created: {config_path}")

if __name__ == "__main__":
    main() 