"""
app/exceptions.py
------------------
Small, specific exception types instead of generic ValueError/Exception
everywhere. Routes catch these and turn them into friendly flash messages.
"""


class ValidationError(Exception):
    """Raised when input data fails a business rule (e.g. missing a
    compulsory field, or zero contact persons submitted)."""


class PermissionDeniedError(Exception):
    """Raised when a user tries to do something their role doesn't allow
    (e.g. an employee editing a lead's compulsory fields)."""


class NotFoundError(Exception):
    """Raised when a requested record does not exist."""
