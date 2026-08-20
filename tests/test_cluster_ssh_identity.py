# SPDX-License-Identifier: Apache-2.0
"""Tests for shared SSH identity defaults and managed key permission tightening."""

from __future__ import annotations

import os
import stat
import subprocess

import pytest

from omlx.cluster.ssh_identity import _MANAGED_IDENTITY, _SSH_PORT, default_ssh_user


def test_ssh_identity_constants():
    assert _MANAGED_IDENTITY == "~/.ssh/omlx_cluster"
    assert _SSH_PORT == 22
    assert default_ssh_user().strip() != ""


def test_default_ssh_user_falls_back_to_env_on_oserror(monkeypatch):
    import omlx.cluster.ssh_identity as mod

    def raise_oserror():
        raise OSError("no such user")

    monkeypatch.setattr(mod.getpass, "getuser", raise_oserror)
    monkeypatch.setattr(os, "environ", {"USER": "envuser", "LOGNAME": "luser"})
    assert default_ssh_user() == "envuser"
    monkeypatch.setattr(os, "environ", {"LOGNAME": "onlylogname"})
    assert default_ssh_user() == "onlylogname"


def test_default_ssh_user_falls_back_to_empty_when_no_env(monkeypatch):
    import omlx.cluster.ssh_identity as mod

    def raise_oserror():
        raise OSError("no such user")

    monkeypatch.setattr(mod.getpass, "getuser", raise_oserror)
    monkeypatch.setattr(os, "environ", {})
    with pytest.raises(RuntimeError, match="cannot determine SSH user"):
        default_ssh_user()


def test_generate_ssh_key_pair_tightens_lax_existing_private_key(tmp_path, monkeypatch):
    from omlx.cluster.ssh_keys import generate_ssh_key_pair

    # Skip when ssh-keygen is unavailable (CI without openssh-client).
    if not shutil_which("ssh-keygen"):
        pytest.skip("ssh-keygen not installed")

    key_path = tmp_path / "omlx_cluster"
    pub_path = tmp_path / "omlx_cluster.pub"
    # Create a private key + matching public key with lax (world-readable) perms.
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"],
        check=True,
    )
    pub_path.write_text(
        "ssh-ed25519 "
        + "AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGU="
        + " test@example.com\n"
    )
    os.chmod(key_path, 0o644)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o644

    pair = generate_ssh_key_pair(key_path=key_path)
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600
    assert pair.public_key.endswith("test@example.com")


def test_ensure_key_permissions_raises_when_chmod_fails_on_lax_file(tmp_path, monkeypatch):
    from omlx.cluster.ssh_keys import ensure_key_permissions

    key_path = tmp_path / "omlx_cluster"
    key_path.write_text("fake-key")
    os.chmod(key_path, 0o644)

    real_chmod = os.chmod

    def failing_chmod(path, mode):
        if str(path) == str(key_path):
            raise OSError("permission denied")
        return real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", failing_chmod)
    with pytest.raises(RuntimeError, match="group/world-readable"):
        ensure_key_permissions(key_path)


def test_ensure_key_permissions_noop_when_already_correct(tmp_path):
    from omlx.cluster.ssh_keys import ensure_key_permissions

    key_path = tmp_path / "omlx_cluster"
    key_path.write_text("fake-key")
    os.chmod(key_path, 0o600)

    ensure_key_permissions(key_path)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def shutil_which(cmd):
    import shutil

    return shutil.which(cmd)
