"""
Enforce gateway.py SRP: the gateway must not import provider-specific modules.

All component wiring belongs in mithra.bootstrap. If this test fails, the
violating import must be moved to bootstrap.py instead.
"""

import ast
import pathlib


# Namespaces that must never appear in gateway.py imports.
# Add new ones here as the codebase grows.
_FORBIDDEN = [
    "mithra.integrations",
    "mithra.adapters",
    "mithra.storage",
    "mithra.routing",
    "mithra.autonomy",
    "mithra.memory",
    "mithra.agent.langgraph_adapter",
]


def test_gateway_imports_no_implementation_modules():
    """gateway.py must only wire via mithra.bootstrap, not import implementations directly."""
    src = pathlib.Path("mithra/api/gateway.py").read_text()
    tree = ast.parse(src)

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            for forbidden in _FORBIDDEN:
                if module == forbidden or module.startswith(forbidden + "."):
                    violations.append(f"  line {node.lineno}: import {module}")

    assert not violations, (
        "gateway.py contains forbidden imports — move them to mithra/bootstrap.py:\n"
        + "\n".join(violations)
    )
