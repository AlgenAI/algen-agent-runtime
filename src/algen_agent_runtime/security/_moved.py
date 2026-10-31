"""The document extractors are moving to ``algen-agent-tools`` (#81)."""

from __future__ import annotations

import importlib
import os
import warnings


def warn_moved(module: str) -> None:
    """Warn that an extractor module is deprecated, attributed to the code that imported it."""
    warnings.warn(
        f"{module} is deprecated and will be removed in a later 0.x release: the document "
        "extractors move to algen-agent-tools (algen_agent_tools.documents, "
        "AlgenAI/algen-agent-tools#36); import them from there once it ships them",
        DeprecationWarning,
        # Skip the import machinery and the extractors importing one another.
        skip_file_prefixes=(
            "<frozen importlib",
            os.path.dirname(importlib.__file__),
            os.path.dirname(__file__),
        ),
    )
