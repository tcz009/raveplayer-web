# -*- coding: utf-8 -*-
"""Gunicorn entry point for local perf runs: same app, INFO request timing on.

    gunicorn -w 1 -k gthread --threads 8 -b 127.0.0.1:8098 perf_wsgi:app

``PYTHONPATH=scripts:.`` so this module and ``app`` both import.
"""

import logging

import app as app_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
app_module.app.logger.setLevel(logging.INFO)

app = app_module.app
