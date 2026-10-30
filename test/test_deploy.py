# -*- coding: utf-8 -*-
"""Deployment script checks — no Docker, no SSH, no network."""

import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "deploy.sh")


def _script():
    with open(SCRIPT, encoding="utf-8") as handle:
        return handle.read()


def test_deploy_script_parses_with_posix_sh():
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("sh is not installed")
    result = subprocess.run([sh, "-n", SCRIPT], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_deploy_script_pins_the_image_to_version_and_labels_it():
    script = _script()
    for marker in (
        'VERSION="$(cat VERSION',                       # version comes from the repo
        'IMAGE="raveplayer-web:$VERSION"',              # pinned image tag
        'LATEST_IMAGE="raveplayer-web:latest"',         # moving tag too
        'org.opencontainers.image.version=$VERSION',    # OCI labels
        'org.opencontainers.image.revision=$REVISION',
        'git rev-parse --short HEAD',                   # revision is the git sha
        '-t $IMAGE -t $LATEST_IMAGE .',
        'curl -fsS -m 4 http://127.0.0.1:$PORT/healthz',  # health check kept
        'cp -a $DEST/data /tmp/raveplayer-data',          # favourites preserved
    ):
        assert marker in script, "deploy.sh is missing %r" % marker
