#!/usr/bin/env python3
"""
Configuration Migration Script
----------------------------
Migrates old configuration files to the new unified configuration system.
"""

import os
import sys
from pathlib import Path

# Add src to Python path
sys.path.append(str(Path(__file__).parent.parent))

from src.config.loader import ConfigLoader

def main():
    """Main migration function."""
    print("Starting configuration migration...")
    
    # Initialize config loader
    loader = ConfigLoader()
    
    try:
        # Load and merge all configurations
        print("Loading and merging configurations...")
        config = loader.load_all()
        
        # Save unified configuration
        print("Saving unified configuration...")
        loader.save_unified_config()
        
        # Archive old configuration files
        print("Archiving old configuration files...")
        loader.migrate_old_configs()
        
        print("\nMigration completed successfully!")
        print("New unified configuration saved to: config/unified_config.json")
        print("Old configuration files archived to: config/archive/")
        
    except Exception as e:
        print(f"\nError during migration: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main() 