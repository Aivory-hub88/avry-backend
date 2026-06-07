#!/usr/bin/env python
"""
Import validation test for AVRY-backend service.
Verifies all Python modules can be imported without errors.
"""

import sys
import os

# Add app directory to path
sys.path.insert(0, os.path.dirname(__file__))

def test_imports():
    """Test all imports"""
    print("=" * 60)
    print("IMPORT TESTS - AVRY-Backend")
    print("=" * 60)
    
    modules_to_test = [
        "main",
        "app.config",
        "app.routes.auth",
        "app.models.user",
        "app.models.user_tier",
        "app.services.auth_service",
        "app.services.tier_service",
        "app.services.audit_logger",
        "app.database.db_service",
    ]
    
    passed = 0
    failed = 0
    errors = []
    
    # Try loading config first
    try:
        from app.config import AppConfig
        print(f"✓ Configuration loaded successfully")
        print(f"  - App: Aivory AI Readiness Platform v1.0.0")
    except Exception as e:
        print(f"⚠ Configuration loading (non-critical): {e}")
    
    # Test each module
    for module in modules_to_test:
        try:
            __import__(module)
            print(f"✓ {module:<40} OK")
            passed += 1
        except ImportError as e:
            print(f"✗ {module:<40} IMPORT ERROR")
            print(f"  Error: {e}")
            errors.append((module, str(e)))
            failed += 1
        except Exception as e:
            print(f"✗ {module:<40} ERROR")
            print(f"  Error: {e}")
            errors.append((module, str(e)))
            failed += 1
    
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    if errors:
        print("\nErrors:")
        for module, error in errors:
            print(f"  {module}: {error}")
    
    return failed == 0

if __name__ == "__main__":
    success = test_imports()
    sys.exit(0 if success else 1)
