#!/usr/bin/env python3
"""
Artifact Externalization Script
-------------------------------
This script handles moving large artifacts (models, data files) to external storage
to reduce repository footprint. It supports:
- Model checkpoint upload to MLflow/W&B
- Data file upload to S3/Git LFS
- On-demand download capabilities
"""

import os
import json
import shutil
import argparse
from pathlib import Path
from typing import Dict, List, Optional
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ArtifactExternalizer:
    def __init__(self, config_path: str = "config/artifact_config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        
    def _load_config(self) -> Dict:
        """Load externalization configuration."""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                return json.load(f)
        else:
            # Default configuration
            return {
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
    
    def _save_config(self):
        """Save current configuration."""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def list_large_files(self, min_size_mb: float = 1) -> List[Dict]:
        """List files larger than specified size."""
        large_files = []
        
        for category, settings in self.config.items():
            local_path = Path(settings["local_path"])
            if not local_path.exists():
                continue
                
            for pattern in settings["file_patterns"]:
                for file_path in local_path.rglob(pattern):
                    size_mb = file_path.stat().st_size / (1024 * 1024)
                    if size_mb >= min_size_mb:
                                            large_files.append({
                        "path": str(file_path),
                        "size_mb": size_mb,
                        "category": category,
                        "relative_path": str(file_path.relative_to(Path.cwd())) if file_path.is_relative_to(Path.cwd()) else str(file_path)
                    })
        
        return sorted(large_files, key=lambda x: x["size_mb"], reverse=True)
    
    def create_upload_script(self, output_path: str = "scripts/upload_artifacts.sh"):
        """Create a shell script for uploading artifacts."""
        large_files = self.list_large_files()
        
        script_content = "#!/bin/bash\n"
        script_content += "# Auto-generated artifact upload script\n"
        script_content += "# Run this script to upload large artifacts to external storage\n\n"
        
        for file_info in large_files:
            category = file_info["category"]
            external_path = self.config[category]["external_path"]
            relative_path = file_info["relative_path"]
            
            script_content += f"# Upload {relative_path} ({file_info['size_mb']:.2f} MB)\n"
            script_content += f"aws s3 cp {relative_path} {external_path}{relative_path}\n\n"
        
        # Make script executable
        script_content += "echo 'Upload complete!'\n"
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            f.write(script_content)
        
        # Make executable on Unix systems
        if os.name != 'nt':  # Not Windows
            os.chmod(output_path, 0o755)
        
        logger.info(f"Upload script created: {output_path}")
        return output_path
    
    def create_download_script(self, output_path: str = "scripts/download_artifacts.sh"):
        """Create a shell script for downloading artifacts on-demand."""
        large_files = self.list_large_files()
        
        script_content = "#!/bin/bash\n"
        script_content += "# Auto-generated artifact download script\n"
        script_content += "# Run this script to download artifacts from external storage\n\n"
        
        for file_info in large_files:
            category = file_info["category"]
            external_path = self.config[category]["external_path"]
            relative_path = file_info["relative_path"]
            
            script_content += f"# Download {relative_path} ({file_info['size_mb']:.2f} MB)\n"
            script_content += f"aws s3 cp {external_path}{relative_path} {relative_path}\n\n"
        
        script_content += "echo 'Download complete!'\n"
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            f.write(script_content)
        
        # Make executable on Unix systems
        if os.name != 'nt':  # Not Windows
            os.chmod(output_path, 0o755)
        
        logger.info(f"Download script created: {output_path}")
        return output_path
    
    def create_placeholder_files(self):
        """Create placeholder files for externalized artifacts."""
        large_files = self.list_large_files()
        
        for file_info in large_files:
            file_path = Path(file_info["path"])
            placeholder_path = file_path.with_suffix(file_path.suffix + ".external")
            
            placeholder_content = f"""# Externalized Artifact Placeholder
# Original file: {file_info['relative_path']}
# Size: {file_info['size_mb']:.2f} MB
# Category: {file_info['category']}
# External location: {self.config[file_info['category']]['external_path']}
# 
# To download this file, run: scripts/download_artifacts.sh
# To upload this file, run: scripts/upload_artifacts.sh
"""
            
            with open(placeholder_path, 'w') as f:
                f.write(placeholder_content)
            
            logger.info(f"Created placeholder: {placeholder_path}")

def main():
    parser = argparse.ArgumentParser(description="Externalize large artifacts")
    parser.add_argument("--action", choices=["list", "upload-script", "download-script", "placeholders"], 
                       default="list", help="Action to perform")
    parser.add_argument("--min-size", type=float, default=1, help="Minimum file size in MB")
    parser.add_argument("--config", default="config/artifact_config.json", help="Config file path")
    
    args = parser.parse_args()
    
    externalizer = ArtifactExternalizer(args.config)
    
    if args.action == "list":
        large_files = externalizer.list_large_files(args.min_size)
        print(f"\nLarge files (>= {args.min_size} MB):")
        print("-" * 80)
        for file_info in large_files:
            print(f"{file_info['relative_path']:<50} {file_info['size_mb']:>8.2f} MB")
        print(f"\nTotal: {len(large_files)} files")
        
    elif args.action == "upload-script":
        script_path = externalizer.create_upload_script()
        print(f"Upload script created: {script_path}")
        
    elif args.action == "download-script":
        script_path = externalizer.create_download_script()
        print(f"Download script created: {script_path}")
        
    elif args.action == "placeholders":
        externalizer.create_placeholder_files()
        print("Placeholder files created for externalized artifacts")

if __name__ == "__main__":
    main() 