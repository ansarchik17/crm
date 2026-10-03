class ProviderError(Exception):
    """Sanitized external provider failure: never carries response bodies or credentials."""
