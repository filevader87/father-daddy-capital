#!/usr/bin/env python3
"""
Archive Manager Script
----------------------
Identifies and archives unused files, old scripts, and proof-of-concept code
to reduce repository footprint while preserving historical work.
"""

import os
import json
import shutil
import argparse
from pathlib import Path
from typing import Dict, List, Optional
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ArchiveManager:
    def __init__(self, config_path: str = "config/archive_config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        
    def _load_config(self) -> Dict:
        """Load archive configuration."""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                return json.load(f)
        else:
            return {
                "archive_path": "archive/",
                "patterns": {
                    "old_scripts": [
                        "*_old.py",
                        "*_backup.py",
                        "*_legacy.py",
                        "*_deprecated.py",
                        "*_v1.py",
                        "*_v2.py"
                    ],
                    "proof_of_concept": [
                        "*_poc.py",
                        "*_experiment.py",
                        "*_prototype.py",
                        "notebooks/*.ipynb"
                    ],
                    "temporary_files": [
                        "*.tmp",
                        "*.temp",
                        "*_temp.py",
                        "temp_*",
                        "tmp_*"
                    ],
                    "old_docs": [
                        "docs/old/*",
                        "docs/deprecated/*",
                        "*.md.bak",
                        "*.txt.bak"
                    ]
                },
                "exclude_patterns": [
                    "src/**/*",
                    "config/**/*",
                    "requirements*.txt",
                    "setup.py",
                    "deploy_loop.py",
                    "README.md",
                    "LICENSE",
                    ".gitignore",
                    ".gitattributes",
                    "tests/**/*",
                    "venv/**/*",
                    "build/**/*",
                    "__pycache__/**/*"
                ],
                "min_file_age_days": 30
            }
    
    def _save_config(self):
        """Save current configuration."""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def find_archive_candidates(self) -> Dict[str, List[Dict]]:
        """Find files that should be archived."""
        candidates = {
            "old_scripts": [],
            "proof_of_concept": [],
            "temporary_files": [],
            "old_docs": []
        }
        
        for category, patterns in self.config["patterns"].items():
            for pattern in patterns:
                for file_path in Path(".").rglob(pattern):
                    if self._should_exclude_file(file_path):
                        continue
                    
                    if file_path.is_file():
                        file_info = {
                            "path": str(file_path),
                            "size_mb": file_path.stat().st_size / (1024 * 1024),
                            "modified": datetime.fromtimestamp(file_path.stat().st_mtime),
                            "relative_path": str(file_path.relative_to(Path.cwd())) if file_path.is_relative_to(Path.cwd()) else str(file_path)
                        }
                        candidates[category].append(file_info)
        
        return candidates
    
    def _should_exclude_file(self, file_path: Path) -> bool:
        """Check if file should be excluded from archiving."""
        relative_path = str(file_path.relative_to(Path.cwd())) if file_path.is_relative_to(Path.cwd()) else str(file_path)
        
        for exclude_pattern in self.config["exclude_patterns"]:
            if self._pattern_matches(relative_path, exclude_pattern):
                return True
        
        return False
    
    def _pattern_matches(self, path: str, pattern: str) -> bool:
        """Check if path matches pattern."""
        import fnmatch
        return fnmatch.fnmatch(path, pattern)
    
    def create_archive(self, dry_run: bool = True) -> Dict[str, List[Dict]]:
        """Create archive of identified files."""
        candidates = self.find_archive_candidates()
        archived_files = {
            "old_scripts": [],
            "proof_of_concept": [],
            "temporary_files": [],
            "old_docs": []
        }
        
        archive_path = Path(self.config["archive_path"])
        
        for category, files in candidates.items():
            if not files:
                continue
                
            category_archive_path = archive_path / category
            if not dry_run:
                category_archive_path.mkdir(parents=True, exist_ok=True)
            
            for file_info in files:
                source_path = Path(file_info["path"])
                relative_path = file_info["relative_path"]
                
                # Create archive path preserving directory structure
                archive_file_path = category_archive_path / relative_path
                if not dry_run:
                    archive_file_path.parent.mkdir(parents=True, exist_ok=True)
                
                if not dry_run:
                    try:
                        shutil.copy2(source_path, archive_file_path)
                        source_path.unlink()  # Remove original file
                        logger.info(f"Archived: {relative_path}")
                    except Exception as e:
                        logger.error(f"Failed to archive {relative_path}: {e}")
                        continue
                
                archived_files[category].append(file_info)
        
        return archived_files
    
    def create_archive_index(self, archived_files: Dict[str, List[Dict]], output_path: str = "archive/README.md"):
        """Create an index of archived files."""
        index_content = """# Archive Index

This directory contains archived files from the Father Daddy Capital project.
These files have been moved here to reduce repository footprint while preserving historical work.

## Archive Categories

"""
        
        total_files = 0
        total_size = 0
        
        for category, files in archived_files.items():
            if not files:
                continue
                
            category_size = sum(f["size_mb"] for f in files)
            total_files += len(files)
            total_size += category_size
            
            index_content += f"### {category.replace('_', ' ').title()}\n"
            index_content += f"- **Files**: {len(files)}\n"
            index_content += f"- **Total Size**: {category_size:.2f} MB\n\n"
            
            for file_info in files:
                index_content += f"- `{file_info['relative_path']}` ({file_info['size_mb']:.2f} MB)\n"
            
            index_content += "\n"
        
        index_content += f"## Summary\n"
        index_content += f"- **Total Files**: {total_files}\n"
        index_content += f"- **Total Size**: {total_size:.2f} MB\n"
        index_content += f"- **Archive Date**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        index_content += """## Restoration

To restore files from archive:

1. Copy the desired files from the appropriate category directory
2. Place them in their original location
3. Update any import statements or references as needed

## Notes

- Files in this archive are not actively maintained
- Some files may be outdated or incompatible with current codebase
- Test thoroughly before restoring any files to production use
"""
        
        if not dry_run:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, 'w') as f:
                f.write(index_content)
        
        return output_path
    
    def create_restore_script(self, archived_files: Dict[str, List[Dict]], output_path: str = "scripts/restore_archive.sh"):
        """Create a script to restore archived files."""
        script_content = "#!/bin/bash\n"
        script_content += "# Archive restoration script\n"
        script_content += "# Run this script to restore archived files\n\n"
        
        for category, files in archived_files.items():
            if not files:
                continue
                
            script_content += f"# Restore {category.replace('_', ' ')}\n"
            for file_info in files:
                relative_path = file_info["relative_path"]
                archive_path = f"archive/{category}/{relative_path}"
                
                script_content += f"# Restore {relative_path}\n"
                script_content += f"mkdir -p $(dirname {relative_path})\n"
                script_content += f"cp {archive_path} {relative_path}\n"
                script_content += f"echo 'Restored: {relative_path}'\n\n"
        
        script_content += "echo 'Restoration complete!'\n"
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            f.write(script_content)
        
        # Make executable on Unix systems
        if os.name != 'nt':
            os.chmod(output_path, 0o755)
        
        return output_path

def main():
    parser = argparse.ArgumentParser(description="Archive unused files and old scripts")
    parser.add_argument("--action", choices=["list", "archive", "index"], 
                       default="list", help="Action to perform")
    parser.add_argument("--dry-run", action="store_true", help="Dry run for archiving")
    parser.add_argument("--config", default="config/archive_config.json", help="Config file path")
    
    args = parser.parse_args()
    
    archive_manager = ArchiveManager(args.config)
    
    if args.action == "list":
        candidates = archive_manager.find_archive_candidates()
        print(f"\nArchive candidates:")
        print("-" * 80)
        
        total_files = 0
        total_size = 0
        
        for category, files in candidates.items():
            if not files:
                continue
                
            category_size = sum(f["size_mb"] for f in files)
            total_files += len(files)
            total_size += category_size
            
            print(f"\n{category.replace('_', ' ').title()} ({len(files)} files, {category_size:.2f} MB):")
            for file_info in files:
                print(f"  {file_info['relative_path']:<50} {file_info['size_mb']:>8.2f} MB")
        
        print(f"\nTotal: {total_files} files, {total_size:.2f} MB")
        
    elif args.action == "archive":
        archived_files = archive_manager.create_archive(dry_run=args.dry_run)
        
        if args.dry_run:
            print(f"\nFiles that would be archived (dry run):")
        else:
            print(f"\nArchived files:")
        
        total_files = 0
        total_size = 0
        
        for category, files in archived_files.items():
            if not files:
                continue
                
            category_size = sum(f["size_mb"] for f in files)
            total_files += len(files)
            total_size += category_size
            
            print(f"\n{category.replace('_', ' ').title()} ({len(files)} files, {category_size:.2f} MB):")
            for file_info in files:
                print(f"  {file_info['relative_path']:<50} {file_info['size_mb']:>8.2f} MB")
        
        print(f"\nTotal: {total_files} files, {total_size:.2f} MB")
        
        if not args.dry_run and total_files > 0:
            # Create archive index
            index_path = archive_manager.create_archive_index(archived_files)
            print(f"\nArchive index created: {index_path}")
            
            # Create restore script
            restore_script = archive_manager.create_restore_script(archived_files)
            print(f"Restore script created: {restore_script}")
        
    elif args.action == "index":
        candidates = archive_manager.find_archive_candidates()
        index_path = archive_manager.create_archive_index(candidates)
        print(f"Archive index created: {index_path}")

if __name__ == "__main__":
    main() 