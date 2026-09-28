"""Errors raised by the data layer and sync code. Routes map these to HTTP responses."""


class DataError(Exception):
    """Base class. `code` is a stable machine-readable string used in JSON responses."""
    code = 'data_error'
    status = 500

    def __init__(self, message='', **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


class NotFound(DataError):
    code = 'not_found'
    status = 404


class InvalidInput(DataError):
    code = 'invalid_input'
    status = 400


class NotPresent(DataError):
    """A Bible/Shirt/Book/HW/Dues flag was set for a kid who isn't present."""
    code = 'not_present'
    status = 409


class Conflict(DataError):
    status = 409

    def __init__(self, code, message='', **extra):
        super().__init__(message, **extra)
        self.code = code


class FutureDate(DataError):
    """Writes for dates after today (Chicago) are rejected: the old UTC bug recorded kids a week ahead."""
    code = 'future_date'
    status = 422
