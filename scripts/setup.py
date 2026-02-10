#!/usr/bin/env python3
"""
Setup script for lets-go-rss skill
Automatically checks and installs dependencies
"""

import sys
import subprocess
import os
from pathlib import Path

def check_and_install_dependencies():
    """Check and install required dependencies"""

    print("🔍 Checking dependencies...")

    # Get skill directory
    skill_dir = Path(__file__).parent.parent
    requirements_file = skill_dir / "requirements.txt"

    if not requirements_file.exists():
        print("⚠️  requirements.txt not found")
        return False

    # Read requirements
    with open(requirements_file, 'r') as f:
        requirements = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    # Check each requirement
    missing_packages = []

    for requirement in requirements:
        package_name = requirement.split('==')[0].split('>=')[0].split('<=')[0]
        import_name = package_name.replace('-', '_')  # pip name → import name
        try:
            __import__(import_name)
            print(f"  ✓ {package_name}")
        except ImportError:
            missing_packages.append(requirement)
            print(f"  ✗ {package_name} (missing)")

    # Install missing packages
    if missing_packages:
        print(f"\n📦 Installing {len(missing_packages)} missing packages...")
        try:
            subprocess.check_call([
                sys.executable, "-m", "pip", "install", "--quiet"
            ] + missing_packages)
            print("✅ All dependencies installed successfully")
            return True
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to install dependencies: {e}")
            return False
    else:
        print("\n✅ All dependencies are already installed")
        return True

def initialize_database():
    """Initialize database if it doesn't exist"""
    skill_dir = Path(__file__).parent.parent
    assets_dir = skill_dir / "assets"
    db_path = assets_dir / "rss_database.db"

    if not db_path.exists():
        print("\n🔧 Initializing database...")
        sys.path.insert(0, str(Path(__file__).parent))
        from database import RSSDatabase

        # Create database
        db = RSSDatabase(str(db_path))
        print("✅ Database initialized")
    else:
        print("\n✅ Database already exists")

def main():
    """Main setup function"""
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║              Let's Go RSS - Setup & Environment Check                ║")
    print("╚══════════════════════════════════════════════════════════════════════╝\n")

    # Check and install dependencies
    if not check_and_install_dependencies():
        print("\n❌ Setup failed: Could not install dependencies")
        return False

    # Initialize database if needed
    try:
        initialize_database()
    except Exception as e:
        print(f"\n⚠️  Database initialization warning: {e}")

    print("\n╔══════════════════════════════════════════════════════════════════════╗")
    print("║                      ✅ Setup Complete!                               ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
