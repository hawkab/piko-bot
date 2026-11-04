"""Lazy public exports: embedding AgentRunner needs no personal tools or channels."""
from importlib import import_module

_EXPORTS = {'AgentLoop': '.loop', 'ContextBuilder': '.context', 'MemoryStore': '.memory', 'SkillsLoader': '.skills'}
__all__ = list(_EXPORTS)

def __getattr__(name):
    if name not in _EXPORTS: raise AttributeError(name)
    return getattr(import_module(_EXPORTS[name], __name__), name)
