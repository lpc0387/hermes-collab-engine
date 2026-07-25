#!/usr/bin/env python3
"""Wraps leader_circle.py with API key injection. Usage: python3 run_leader.py [args]"""
import os, sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJ_DIR = os.path.dirname(_THIS_DIR)

# Inject API key
os.environ['OPENCODE_GO_API_KEY'] = open('/tmp/upstream_key.txt').read().strip()

# Set up paths so leader_circle can find src/
sys.path.insert(0, os.path.join(_PROJ_DIR, 'src'))

# Run leader_circle with __file__ set properly
_leader_py = os.path.join(_THIS_DIR, 'leader_circle.py')
with open(_leader_py) as f:
    _code = f.read()
_globals = {'__file__': _leader_py, '__name__': '__main__', '__doc__': None}
# Merge in current module's builtins
import builtins
_globals['__builtins__'] = builtins
exec(compile(_code, _leader_py, 'exec'), _globals)
