# -*- coding: utf-8 -*-
"""Run the web app locally for perf probes with per-request timing visible.

Production never emits ``req ... ms=`` lines because Flask's ``app.logger``
defaults to WARNING and gunicorn has no access log; this wrapper turns the
existing app.py timing line on for a local benchmark (no app.py change).

    RAVEWEB_DATA=/tmp/raveweb-perf/data SECRET_KEY=perf \
    RAVEWEB_PASSWORD=perf-password RAVEWEB_ENV=test \
    .venv/bin/python scripts/perf_server.py --port 8098
"""

import argparse
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app as app_module  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8098)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    app_module.app.logger.setLevel(logging.INFO)
    app_module.app.run(host=args.host, port=args.port, threaded=True,
                       use_reloader=False)


if __name__ == "__main__":
    main()
