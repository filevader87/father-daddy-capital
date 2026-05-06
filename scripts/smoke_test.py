#!/usr/bin/env python3

import os
import sys
import requests
import psycopg2
import redis
from datetime import datetime

def check_solana_connection():
    """Check connection to Solana RPC"""
    try:
        response = requests.get(os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"))
        return response.status_code == 200
    except Exception as e:
        print(f"Solana connection failed: {e}")
        return False

def check_jupiter_api():
    """Check connection to Jupiter API"""
    try:
        # Using price API which is more reliable
        response = requests.get(os.getenv("JUPITER_API_URL", "https://price.jup.ag/v4/price"))
        return response.status_code == 200
    except Exception as e:
        print(f"Jupiter API connection failed: {e}")
        return False

def check_redis():
    """Check Redis connection"""
    try:
        r = redis.Redis(host='localhost', port=6379, db=0)
        r.ping()
        return True
    except Exception as e:
        print(f"Redis connection failed: {e}")
        return False

def check_postgres():
    """Check PostgreSQL connection"""
    try:
        conn = psycopg2.connect(
            dbname="trading_db",
            user="trading_user",
            password=os.getenv("DB_PASSWORD", "trading_password"),
            host="localhost"
        )
        conn.close()
        return True
    except Exception as e:
        print(f"PostgreSQL connection failed: {e}")
        return False

def check_logs():
    """Check if log file exists and is writable"""
    log_file = os.getenv("LOG_FILE", "logs/system.log")
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        with open(log_file, 'a') as f:
            f.write(f"Smoke test at {datetime.now()}\n")
        return True
    except Exception as e:
        print(f"Log file check failed: {e}")
        return False

def check_config_files():
    """Check if required config files exist"""
    config_files = [
        "config/trading_config.json",
        "config/main_config.json",
        ".env"
    ]
    try:
        for config_file in config_files:
            if not os.path.exists(config_file):
                print(f"Config file missing: {config_file}")
                return False
        return True
    except Exception as e:
        print(f"Config files check failed: {e}")
        return False

def check_health_endpoint():
    """Check if health endpoint is accessible"""
    try:
        response = requests.get("http://localhost:8080/healthz", timeout=5)
        return response.status_code == 200
    except Exception as e:
        print(f"Health endpoint check failed: {e}")
        return False

def check_disk_space():
    """Check available disk space"""
    try:
        import shutil
        total, used, free = shutil.disk_usage(".")
        free_gb = free // (1024**3)
        return free_gb > 1  # At least 1GB free
    except Exception as e:
        print(f"Disk space check failed: {e}")
        return False

def main():
    checks = {
        "Solana Connection": check_solana_connection,
        "Jupiter API": check_jupiter_api,
        "Redis": check_redis,
        "PostgreSQL": check_postgres,
        "Log File": check_logs,
        "Config Files": check_config_files,
        "Health Endpoint": check_health_endpoint,
        "Disk Space": check_disk_space
    }

    all_passed = True
    for name, check in checks.items():
        print(f"Checking {name}...")
        if check():
            print(f"✓ {name} is healthy")
        else:
            print(f"✗ {name} check failed")
            all_passed = False

    if not all_passed:
        sys.exit(1)
    print("All system checks passed!")

if __name__ == "__main__":
    main() 