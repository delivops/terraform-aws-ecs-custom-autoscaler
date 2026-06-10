import importlib
import os
import sys
import types

# Make the lambda/ package root importable (handler, adapters).
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# A region so boto3 client construction at import time doesn't raise.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Stub heavy/optional deps only if they aren't installed, so importing the
# handler (and its adapters) never requires redis/requests/boto3 just to unit
# test the pure evaluate() logic.
for _name in ("boto3", "redis", "requests"):
    try:
        importlib.import_module(_name)
    except ImportError:
        _stub = types.ModuleType(_name)
        if _name == "boto3":
            _stub.client = lambda *a, **k: None
        sys.modules[_name] = _stub
