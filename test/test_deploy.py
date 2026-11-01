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
        'cp -a $DEST/data "\\$DATA_BACKUP"',            # data kept on the host
    ):
        assert marker in script, "deploy.sh is missing %r" % marker

def test_deploy_script_keeps_a_rollback_target_and_restarts_it():
    """W4: a failed start must not leave the player down."""
    script = _script()
    for marker in (
        "raveplayer-web-new",                          # new container starts aside
        'DATA_BACKUP="\\$DEST.data-backup"',          # host-side data backup
        "rolling back to the previous container",
        "docker rm -f raveplayer-web-new",             # new one discarded
        "docker start raveplayer-web",                 # old one restarted
        "docker rename raveplayer-web-new raveplayer-web",  # promote on success
        "docker stop -t 20 raveplayer-web",            # stopped, not removed
    ):
        assert marker in script, "deploy.sh is missing the rollback step %r" % marker
    # The old container must survive until the new one is healthy: stop first,
    # remove only on the success path.
    assert script.index("docker stop -t 20 raveplayer-web") < script.index(
        "docker rm -f raveplayer-web-new")
    assert "rm -rf /tmp/raveplayer-data" not in script, "data must not live only in /tmp"


def test_deploy_script_keeps_secrets_off_the_docker_command_line():
    """W4: SOUNDCLOUD_CLIENT_ID must travel in an env file, not in argv."""
    script = _script()
    assert "--env-file" in script
    assert "-e SOUNDCLOUD_CLIENT_ID" not in script
    assert "/tmp/raveplayer-web.env" in script
    assert "rm -f" in script
