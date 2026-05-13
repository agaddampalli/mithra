"""
Shared pytest fixtures for both unit and integration test suites.

Unit fixtures provide in-memory or tmp-path backed instances of adapters.
Integration fixtures connect to the real Docker services and are skipped
automatically when the ENVIRONMENT variable is not set to 'test'.
"""

import pytest


# Fixtures are added here as each Phase 0 component is implemented.
