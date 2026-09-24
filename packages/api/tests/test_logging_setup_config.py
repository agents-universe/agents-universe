"""LOG_LEVEL / LOG_FORMAT must be honored from Settings.

.env.example documents both keys, and BaseSettings merges the .env file
into Settings — but setup_logging used to read os.environ directly, so
uncommenting LOG_LEVEL=DEBUG in .env silently did nothing while the
Settings fields sat unconsumed.
"""
from __future__ import annotations

import logging


def test_setup_logging_consumes_settings(monkeypatch):
    from api import config, logging_setup

    class _Cfg:
        log_level = "DEBUG"
        log_format = "human"

    monkeypatch.setattr(config, "get_settings", lambda: _Cfg())

    root = logging.getLogger()
    orig_handlers = root.handlers[:]
    orig_level = root.level
    try:
        logging_setup.setup_logging()

        assert root.level == logging.DEBUG
        assert any(
            isinstance(handler.formatter, logging_setup.HumanFormatter)
            for handler in root.handlers
        ), "log_format=human must select HumanFormatter"
    finally:
        # setup_logging replaces the root handlers — put the session's
        # original ones back so later tests still log through them.
        root.handlers.clear()
        root.handlers.extend(orig_handlers)
        root.setLevel(orig_level)
