#!/usr/bin/env python3
import os
import sys
import zipfile
import datetime
from pathlib import Path

def create_backup(source_dir='.', exclude_dirs=None, exclude_files=None):
    """
    Create a zip backup of the repository, excluding specified directories and files.
    
    Args:
        source_dir (str): Directory to backup
        exclude_dirs (list): List of directories to exclude
        exclude_files (list): List of files to exclude
    """
    if exclude_dirs is None:
        exclude_dirs = [
            '__pycache__',
            '.git',
            'venv',
            'env',
            'logs',
            'data',
            'dist',
            'build',
            '.pytest_cache',
            '.coverage',
            'htmlcov'
        ]
    
    if exclude_files is None:
        exclude_files = [
            '.DS_Store',
            '*.pyc',
            '*.pyo',
            '*.pyd',
            '*.so',
            '*.dll',
            '*.zip',
            '*.tar.gz'
        ]
    
    # Create backup directory if it doesn't exist
    backup_dir = Path('backups')
    backup_dir.mkdir(exist_ok=True)
    
    # Generate backup filename with timestamp
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_file = backup_dir / f'backup_{timestamp}.zip'
    
    print(f"Creating backup: {backup_file}")
    
    try:
        with zipfile.ZipFile(backup_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(source_dir):
                # Skip excluded directories
                dirs[:] = [d for d in dirs if d not in exclude_dirs]
                
                for file in files:
                    file_path = Path(root) / file
                    rel_path = file_path.relative_to(source_dir)
                    
                    # Skip excluded files
                    if any(rel_path.match(pattern) for pattern in exclude_files):
                        continue
                    
                    # Skip backup directory
                    if str(rel_path).startswith('backups/'):
                        continue
                    
                    try:
                        zipf.write(file_path, rel_path)
                        print(f"Added: {rel_path}")
                    except Exception as e:
                        print(f"Error adding {rel_path}: {e}")
        
        print(f"Backup created successfully: {backup_file}")
        return str(backup_file)
    
    except Exception as e:
        print(f"Error creating backup: {e}")
        if backup_file.exists():
            backup_file.unlink()
        return None

if __name__ == "__main__":
    backup_file = create_backup()
    if backup_file:
        print(f"Backup completed successfully: {backup_file}")
    else:
        print("Backup failed")
        sys.exit(1) 