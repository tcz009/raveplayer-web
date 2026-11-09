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
        'DATA_BACKUP="$DEST.data-backup"',              # host-side data backup (DEST expanded locally)
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
    # Regression: remote-only vars must be escaped inside the unquoted heredoc,
    # otherwise `set -u` aborts the local script before ssh runs.
    assert 'rm -rf "\\$DATA_BACKUP"' in script
    assert 'rm -rf "$DATA_BACKUP"' not in script


def test_deploy_script_keeps_secrets_off_the_docker_command_line():
    """W4: SOUNDCLOUD_CLIENT_ID must travel in an env file, not in argv."""
    script = _script()
    assert "--env-file" in script
    assert "-e SOUNDCLOUD_CLIENT_ID" not in script
    assert "/tmp/raveplayer-web.env" in script
    assert "rm -f" in script
    # The value must be expanded by the *deploying* shell (ssh does not forward
    # the local environment), and never written with a literal escaped dollar.
    assert '"${SOUNDCLOUD_CLIENT_ID:-}"' in script
    assert '"\\${SOUNDCLOUD_CLIENT_ID' not in script

# --------------------------------------------------------------------------- functional rollback (W3)


def _remote_block():
    """Extract the remote heredoc body, with heredoc escapes resolved."""
    script = _script()
    start = script.index("<<REMOTE") + len("<<REMOTE")
    end = script.index("\nREMOTE\n", start)
    block = script[start:end]
    assert "docker run" in block and "restore_previous" in block
    return block.replace("\\$", "$")


def _stub(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_remote(tmp_path, run_exit=0, health_exit=0, build_exit=0, old_container=True):
    """Run the extracted remote script with stubbed docker/curl/tar/chown/seq."""
    import textwrap

    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(exist_ok=True)

    docker_body = textwrap.dedent("""\
        #!/bin/sh
        echo "$*" >> "@LOG@"
        cmd="${1:-}"
        case "$cmd" in
          run)
            prev=""
            for arg in "$@"; do
              if [ "$prev" = "--env-file" ]; then
                if [ -f "$arg" ]; then
                  perms=$(stat -f '%Lp' "$arg" 2>/dev/null || stat -c '%a' "$arg" 2>/dev/null)
                  content=$(tr '\n' '|' < "$arg")
                  echo "envfile $perms $content" >> "@LOG@"
                else
                  echo "envfile-missing" >> "@LOG@"
                fi
              fi
              prev="$arg"
            done
            exit @RUN_EXIT@
            ;;
          inspect)
            exit @INSPECT_EXIT@
            ;;
          build)
            exit @BUILD_EXIT@
            ;;
          *)
            exit 0
            ;;
        esac
        """)
    docker_body = (docker_body
                   .replace("@LOG@", str(log))
                   .replace("@RUN_EXIT@", str(run_exit))
                   .replace("@INSPECT_EXIT@", "0" if old_container else "1")
                   .replace("@BUILD_EXIT@", str(build_exit)))
    _stub(stub_bin / "docker", docker_body)
    _stub(stub_bin / "curl", "#!/bin/sh\nexit %d\n" % health_exit)
    _stub(stub_bin / "tar", "#!/bin/sh\nexit 0\n")
    _stub(stub_bin / "chown", "#!/bin/sh\nexit 0\n")
    _stub(stub_bin / "seq", "#!/bin/sh\necho 1\n")

    block_path = tmp_path / "remote.sh"
    block_path.write_text(_remote_block(), encoding="utf-8")

    sh = shutil.which("sh")
    if not sh:
        pytest.skip("sh is not installed")
    env = dict(os.environ)
    env.update({
        "PATH": str(stub_bin) + os.pathsep + env.get("PATH", ""),
        "DEST": str(tmp_path / "deploy"),
        "PORT": "18097",
        "VERSION": "1.6.13",
        "REVISION": "deadbee",
        "IMAGE": "raveplayer-web:1.6.13",
        "LATEST_IMAGE": "raveplayer-web:latest",
        "ALLOW_PRIVATE": "0",
        "MAX_STREAMS": "6",
        "SOUNDCLOUD_CLIENT_ID": "secret-e2e-id",
    })
    env_file = "/tmp/raveplayer-web.env"
    auth_env_file = "/tmp/raveplayer-web-auth.env"
    try:
        for path in (env_file, auth_env_file):
            if os.path.exists(path):
                os.remove(path)
        with open(auth_env_file, "w", encoding="utf-8") as handle:
            handle.write("SECRET_KEY=test-secret\n"
                         "RAVEWEB_PASSWORD_HASH=owner-hash\n"
                         "RAVEWEB_FRIENDS_PASSWORD_HASH=friends-hash\n"
                         "RAVEWEB_ENV=test\n")
        os.chmod(auth_env_file, 0o600)
        result = subprocess.run([sh, "-eu", str(block_path)], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=90)
    finally:
        env_left = os.path.exists(env_file)
        for path in (env_file, auth_env_file):
            if os.path.exists(path):
                os.remove(path)
    calls = [line for line in log.read_text(encoding="utf-8").splitlines() if line]
    return result, calls, env_left


def test_remote_script_restarts_old_container_when_docker_run_fails(tmp_path):
    """W3: `docker run` failing after the old container was stopped must roll back."""
    result, calls, env_left = _run_remote(tmp_path, run_exit=1)
    assert result.returncode == 1, result.stderr
    assert any(call.startswith("stop ") and "raveplayer-web" in call for call in calls), calls
    assert any(call.startswith("start raveplayer-web") for call in calls), calls
    assert not any(call.startswith("rename ") for call in calls), calls
    # The env file existed (0600, with the secret) while docker run was attempted,
    # and is removed on the rollback path.
    env_lines = [call for call in calls if call.startswith("envfile ")]
    assert len(env_lines) == 1, calls
    assert env_lines[0].startswith("envfile 600 SECRET_KEY=test-secret|"), env_lines
    assert "RAVEWEB_PASSWORD_HASH=owner-hash" in env_lines[0]
    assert "RAVEWEB_FRIENDS_PASSWORD_HASH=friends-hash" in env_lines[0]
    assert "RAVEWEB_ENV=test" in env_lines[0]
    assert "SOUNDCLOUD_CLIENT_ID=secret-e2e-id" in env_lines[0]
    assert env_left is False
    assert "rolling back to the previous container" in result.stdout


def test_remote_script_rolls_back_when_the_new_container_is_unhealthy(tmp_path):
    result, calls, env_left = _run_remote(tmp_path, run_exit=0, health_exit=1)
    assert result.returncode == 1, result.stderr
    assert any(call.startswith("start raveplayer-web") for call in calls), calls
    assert not any(call.startswith("rename ") for call in calls), calls
    assert env_left is False


def test_remote_script_promotes_the_new_container_when_healthy(tmp_path):
    result, calls, env_left = _run_remote(tmp_path, run_exit=0, health_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(call.startswith("rename raveplayer-web-new raveplayer-web")
               for call in calls), calls
    assert not any(call.startswith("start raveplayer-web") for call in calls), calls
    assert env_left is False
    assert not (tmp_path / "deploy.data-backup").exists(), "backup kept after success"


def test_remote_script_does_not_stop_the_old_container_when_the_build_fails(tmp_path):
    result, calls, env_left = _run_remote(tmp_path, build_exit=1)
    assert result.returncode != 0
    assert not any(call.startswith("stop ") for call in calls), calls
    assert not any(call.startswith("run ") for call in calls), calls
    env_lines = [call for call in calls if call.startswith("envfile")]
    assert env_lines == []
    assert env_left is False
