"""
glitchtip_integration – re-export surface so agents that reach for
`from glitchtip_integration.models import ...` get something importable
instead of ImportError → exec() crash → SyntaxError.

GlitchTip webhook + integration logic lives in core (core.views, core.models).
No separate Django app; this shim exists purely to satisfy the import path
agents and shell commands sometimes emit.
"""