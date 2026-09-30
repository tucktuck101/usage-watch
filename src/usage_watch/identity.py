"""Keyed hashes of identities (D5, "Hashed identities").

Every identity that D5 allows to be kept is stored as
HMAC-SHA256(install_secret, namespace + ":" + raw), truncated to 16 hex
characters. The secret is per install, so a key can't be reversed by guessing
and can't be linked across installs.

Redaction: nothing here logs, and no exception carries a raw value, a
normalised value or the secret. Messages name the argument, never its content.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from usage_watch import config
from usage_watch.errors import Problem

SECRET_BYTES = 32
SECRET_FILE = "install_secret"
KEY_CHARS = 16

NS_ACCOUNT = "account"  # account:<provider>, account aliases (D7)
NS_CHECKOUT = "checkout"  # checkout_id (D2)
NS_REPOSITORY = "repository"  # repository_id (D2)
NS_STREAM = "stream"  # stream_key when it isn't a session_key (D1, D3)
NS_REQUEST = "request"  # request:<provider>, provider_request_key (D1)


def install_secret(state_dir: Path | str | None = None) -> bytes:
    """Return the per-install secret, creating it on first use."""
    directory = Path(state_dir) if state_dir is not None else config.state_dir()
    path = directory / SECRET_FILE
    try:
        return _read_secret(path)
    except FileNotFoundError:
        pass
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{SECRET_FILE}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(SECRET_BYTES))
            fh.flush()
            os.fsync(fh.fileno())
        # A hard link is an atomic rename that refuses to clobber: if another
        # process created the secret first, keep theirs rather than replace a
        # secret that may already have been used to hash something.
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(directory)
    return _read_secret(path)


def _read_secret(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) != SECRET_BYTES:
        raise Problem(
            f"install secret {path} is {len(data)} bytes",
            expected=f"exactly {SECRET_BYTES} bytes, created by usage-watch on first run",
            fix=(
                f"restore {path} from a backup of the state directory.\n"
                "Deleting it makes a new secret, which changes every hashed identity "
                "(accounts, checkout_id, repository_id) and splits history."
            ),
        )
    return data


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def keyed_hash(namespace: str, raw: str | bytes | None, secret: bytes | None = None) -> str:
    """HMAC-SHA256(secret, namespace + ":" + raw) as 16 hex characters."""
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("keyed_hash: namespace is empty; refusing to hash into an unscoped key")
    if raw is None:
        raise ValueError(f"keyed_hash[{namespace}]: raw value is None; refusing to hash nothing")
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    elif isinstance(raw, (bytes, bytearray)):
        raw_bytes = bytes(raw)
    else:
        raise TypeError(f"keyed_hash[{namespace}]: raw value must be str or bytes, got {type(raw).__name__}")
    if not raw_bytes:
        raise ValueError(f"keyed_hash[{namespace}]: raw value is empty; refusing to hash nothing")
    if secret is None:
        secret = install_secret()
    if not secret:
        raise ValueError(f"keyed_hash[{namespace}]: secret is empty")
    message = namespace.encode("utf-8") + b":" + raw_bytes
    return hmac.new(secret, message, hashlib.sha256).hexdigest()[:KEY_CHARS]


def _provider_ns(base: str, provider: str) -> str:
    # A ':' in the provider would let two namespaces collide
    # ("account:a:b" + raw == "account:a" + "b:" + raw).
    if not isinstance(provider, str) or not provider or ":" in provider:
        raise ValueError(f"{base}: provider must be a non-empty name without ':'")
    return f"{base}:{provider}"


def account(provider: str, raw_id: str | bytes, secret: bytes | None = None) -> str:
    """Account alias hash (D7), namespace account:<provider>."""
    return keyed_hash(_provider_ns(NS_ACCOUNT, provider), raw_id, secret)


def checkout(common_dir_realpath: str | bytes, secret: bytes | None = None) -> str:
    """checkout_id (D2): hash of the real path of the git common directory."""
    return keyed_hash(NS_CHECKOUT, common_dir_realpath, secret)


def repository(remote_url: str, secret: bytes | None = None) -> str:
    """repository_id (D2): hash of the normalised origin remote."""
    return keyed_hash(NS_REPOSITORY, normalise_remote(remote_url), secret)


def stream(raw: str | bytes, secret: bytes | None = None) -> str:
    """stream_key when it isn't a session_key: a source file path or trace ID."""
    return keyed_hash(NS_STREAM, raw, secret)


def request(provider: str, raw_id: str | bytes, secret: bytes | None = None) -> str:
    """provider_request_key (D1), namespace request:<provider>."""
    return keyed_hash(_provider_ns(NS_REQUEST, provider), raw_id, secret)


# git's scp-like syntax: [user@]host:path, with no slash before the first colon.
_SCP = re.compile(r"^(?:[^@/]+@)?(?P<host>[^/:]+):(?P<path>.*)$")


def normalise_remote(remote_url: str) -> str:
    """Reduce a remote URL to "host/path", lowercased.

    Drops the scheme, any user:pass@ credentials, the port and a trailing
    ".git", so git@host:org/repo and https://host/org/repo are equal. A remote
    with no host (a local path, file://) is refused: there is nothing
    host-and-path shaped to key on.
    """
    if not isinstance(remote_url, str) or not remote_url.strip():
        raise ValueError("repository: remote URL is empty; refusing to hash nothing")
    url = remote_url.strip()
    if "://" in url:
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
        except ValueError:
            raise ValueError("repository: remote URL could not be parsed") from None
        path = parts.path
    else:
        m = _SCP.match(url)
        if not m:
            raise ValueError("repository: remote URL has no host (local path remotes are not keyed)")
        host, path = m.group("host"), m.group("path")
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[: -len(".git")].rstrip("/")
    if not host or not path:
        raise ValueError("repository: remote URL has no host or no path")
    return f"{host.lower()}/{path.lower()}"
