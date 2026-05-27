#!/usr/bin/env python3
"""
Drive the CUDA vanity engine, verify every keypair it reports with a trusted
ed25519 library, and store the good ones in MongoDB.

The GPU does the heavy lifting (find a seed whose Solana address ends in the
configured suffix). This script NEVER trusts the GPU blindly: for every match it
re-derives the public key from the seed locally with `solders` (the same
ed25519 implementation Solana uses) and re-checks both that the address matches
what the GPU reported AND that it ends with the suffix. Only then is it stored.

Stored document shape (as requested):
    {
        "public_key":  "<base58 address, ends with the suffix>",
        "private_key": "<base58 of the 64-byte keypair: wallet-import format>",
        "isused":      false,
        # extras for convenience / debugging:
        "secret_key_json": [<64 ints>],   # solana-keygen file format
        "suffix":      "pump",
        "created_at":  <UTC datetime>,
    }

Configuration is via environment variables (see .env.example):
    MONGODB_URI          (required unless DRY_RUN=1)  e.g. mongodb+srv://...
    MONGODB_DB           default: solana
    MONGODB_COLLECTION   default: pump_keys
    SUFFIX               default: pump   (MUST match what the engine was built with)
    TARGET_COUNT         default: 1      (0 = run until interrupted)
    VANITY_BIN           default: ./engine/src/release/cuda_ed25519_vanity
    DRY_RUN              if "1", skip MongoDB and just print verified keys
"""

import datetime
import os
import shutil
import signal
import subprocess
import sys

import base58
from solders.keypair import Keypair

HERE = os.path.dirname(os.path.abspath(__file__))
_BIN_NAME = "cuda_ed25519_vanity.exe" if os.name == "nt" else "cuda_ed25519_vanity"
DEFAULT_BIN = os.path.join(HERE, "engine", "src", "release", _BIN_NAME)


def _load_dotenv():
    """Load KEY=VALUE pairs from a sibling .env file (no dependency needed).

    Cross-platform convenience so Windows users don't need `source .env`. Real
    environment variables already set take precedence over the file. Supports:
      KEY=value                           # plain value
      KEY="value"                         # quoted (preserves spaces)
      KEY=value   # trailing comment      # comment is stripped
      KEY="value"   # trailing comment    # closing quote ends the value
      KEY='val with # literal hash'       # # inside quotes is part of the value
    """
    path = os.path.join(HERE, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.lstrip()
            if val[:1] in ('"', "'"):
                # Quoted value: take everything up to the matching closing
                # quote; anything after it (e.g. " # comment") is discarded.
                quote = val[0]
                end = val.find(quote, 1)
                val = val[1:end] if end != -1 else val[1:]
            else:
                # Unquoted: a '#' starts an inline comment.
                hashpos = val.find("#")
                if hashpos != -1:
                    val = val[:hashpos]
                val = val.rstrip()
            os.environ.setdefault(key, val)


_load_dotenv()


def env(name, default=None):
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


SUFFIX = env("SUFFIX", "pump")
TARGET_COUNT = int(env("TARGET_COUNT", "1"))  # 0 means "run forever"
VANITY_BIN = env("VANITY_BIN", DEFAULT_BIN)
DRY_RUN = env("DRY_RUN", "0") == "1"


def log(msg):
    print(msg, flush=True)


def verify_keypair(address: str, seed_b58: str):
    """Re-derive the keypair from the GPU-reported seed and validate it.

    Returns (address, private_key_b58, secret_key_json) or raises ValueError.
    """
    seed = base58.b58decode(seed_b58)
    if len(seed) != 32:
        raise ValueError(f"seed is {len(seed)} bytes, expected 32")

    kp = Keypair.from_seed(seed)             # ed25519 derivation, as Solana does
    derived = str(kp.pubkey())               # base58 address

    if derived != address:
        raise ValueError(
            f"GPU address {address!r} != locally derived {derived!r}"
        )
    if not derived.endswith(SUFFIX):
        raise ValueError(f"address {derived!r} does not end with {SUFFIX!r}")

    raw64 = bytes(kp)                         # 32-byte seed || 32-byte pubkey
    private_key_b58 = base58.b58encode(raw64).decode()  # Phantom/Solflare import
    return derived, private_key_b58, list(raw64)


def _truthy(s):
    return str(s).strip().lower() in ("1", "true", "yes", "on")


def _ssh_diagnose(ssh_host, ssh_port, ssh_user, ssh_key, passphrase):
    """Run a paramiko probe to print a clear, actionable reason for an SSH failure.

    sshtunnel collapses every failure into one generic message; this routine
    reproduces just the SSH handshake with paramiko so we can tell the user
    whether it was a key-parse problem, server reject, or network issue.
    """
    import paramiko, socket
    # 1) Can we even reach the port?
    try:
        with socket.create_connection((ssh_host, ssh_port), timeout=10):
            pass
    except OSError as e:
        log(f"   [diag] cannot TCP-connect to {ssh_host}:{ssh_port}: {e}")
        return

    # 2) Can paramiko parse the private key? Try every common type.
    pkey, parsed_as = None, None
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            pkey = cls.from_private_key_file(ssh_key, password=passphrase or None)
            parsed_as = cls.__name__
            break
        except paramiko.PasswordRequiredException:
            log(f"   [diag] key {ssh_key} is encrypted; set SSH_KEY_PASSPHRASE.")
            return
        except (paramiko.SSHException, ValueError):
            continue
    if pkey is None:
        log(f"   [diag] paramiko could not parse {ssh_key}. "
            f"Confirm it is the PRIVATE key (not .pub) in OpenSSH/PEM format. "
            f"Sanity check on Windows:  ssh-keygen -y -f \"{ssh_key}\"")
        return
    log(f"   [diag] key parsed as {parsed_as}")

    # 3) Try the actual SSH auth.
    try:
        t = paramiko.Transport((ssh_host, ssh_port))
        t.start_client(timeout=15)
    except paramiko.SSHException as e:
        log(f"   [diag] SSH protocol handshake failed: {e}")
        return
    try:
        try:
            t.auth_publickey(ssh_user, pkey)
            log(f"   [diag] paramiko auth SUCCEEDED for {ssh_user}@{ssh_host} — "
                f"the failure is somewhere else in sshtunnel.")
        except paramiko.AuthenticationException:
            log(f"   [diag] server REJECTED the key for user {ssh_user!r}. "
                f"Confirm the matching public key is in "
                f"{ssh_user}@{ssh_host}:~/.ssh/authorized_keys, that the file "
                f"perms are 600, and that SSH_USER is right.")
        except paramiko.SSHException as e:
            log(f"   [diag] SSH error during auth: {e}")
    finally:
        try: t.close()
        except Exception: pass


def _make_sshtunnel_logger():
    """Detailed stderr logger so sshtunnel's underlying error is visible."""
    import logging
    logger = logging.getLogger("vanity.sshtunnel")
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("   [ssh] %(levelname)s %(message)s"))
        logger.addHandler(h)
        logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger


def _open_ssh_tunnel():
    """Open an SSH tunnel to a private MongoDB host. Returns (tunnel, local_port).

    Activated when SSH_HOST is set. Uses public-key auth (SSH_KEY_PATH), with an
    optional passphrase. The tunnel forwards a local loopback port to the
    MongoDB host as seen from the SSH server.
    """
    from sshtunnel import SSHTunnelForwarder, BaseSSHTunnelForwarderError

    ssh_host = env("SSH_HOST")
    ssh_port = int(env("SSH_PORT", "22"))
    ssh_user = env("SSH_USER")
    ssh_key  = env("SSH_KEY_PATH")
    passphrase = env("SSH_KEY_PASSPHRASE")
    if not (ssh_user and ssh_key):
        log("ERROR: SSH mode needs SSH_USER and SSH_KEY_PATH (private key file).")
        sys.exit(1)
    ssh_key = os.path.expanduser(ssh_key)
    if not os.path.isfile(ssh_key):
        log(f"ERROR: SSH key file not found: {ssh_key}")
        sys.exit(1)

    remote_host = env("SSH_REMOTE_MONGO_HOST", "127.0.0.1")
    remote_port = int(env("SSH_REMOTE_MONGO_PORT", "27017"))
    local_port  = int(env("SSH_LOCAL_BIND_PORT", "0"))   # 0 -> auto-pick free port

    log(f">> Opening SSH tunnel: {ssh_user}@{ssh_host}:{ssh_port}"
        f"  -> {remote_host}:{remote_port}")
    tunnel = SSHTunnelForwarder(
        (ssh_host, ssh_port),
        ssh_username=ssh_user,
        ssh_pkey=ssh_key,
        ssh_private_key_password=passphrase,
        remote_bind_address=(remote_host, remote_port),
        local_bind_address=("127.0.0.1", local_port),
        # Limit sshtunnel to ONLY the key we pass; don't let it scan ~/.ssh or
        # the SSH agent (which can introduce noisy unrelated failures).
        allow_agent=False,
        host_pkey_directories=[],
        logger=_make_sshtunnel_logger(),
    )
    try:
        tunnel.start()
    except BaseSSHTunnelForwarderError as e:
        log(f"ERROR: SSH tunnel failed: {e}")
        log("   running a direct paramiko probe to surface the actual reason...")
        _ssh_diagnose(ssh_host, ssh_port, ssh_user, ssh_key, passphrase)
        sys.exit(1)
    log(f"   tunnel listening on 127.0.0.1:{tunnel.local_bind_port}")
    return tunnel, tunnel.local_bind_port


def open_collection():
    """Connect to MongoDB (optionally via SSH tunnel + TLS).

    Returns (client, collection, tunnel_or_None). Caller must close the client
    AND stop the tunnel when done.

    Two connection modes:
      * SSH tunnel mode: SSH_HOST is set. We open a forward to the private
        MongoDB host and connect over TLS using component env vars
        (MONGODB_USERNAME / MONGODB_PASSWORD / MONGODB_AUTH_SOURCE), with
        MONGODB_TLS_CA_FILE and optional MONGODB_TLS_CERT_KEY_FILE.
      * URI mode: just use MONGODB_URI as-is (Atlas, local, etc.).
    """
    from pymongo import ASCENDING, MongoClient

    db_name   = env("MONGODB_DB", "solana")
    coll_name = env("MONGODB_COLLECTION", "pump_keys")

    tunnel = None
    if env("SSH_HOST"):
        tunnel, local_port = _open_ssh_tunnel()

        tls_ca   = env("MONGODB_TLS_CA_FILE")
        tls_cert = env("MONGODB_TLS_CERT_KEY_FILE")
        if not tls_ca:
            log("WARN: MONGODB_TLS_CA_FILE not set; the server's TLS cert "
                "won't be verified against a CA (set it for proper TLS).")

        kwargs = dict(
            host="127.0.0.1",
            port=local_port,
            authSource=env("MONGODB_AUTH_SOURCE", "admin"),
            tls=True,
            tlsCAFile=os.path.expanduser(tls_ca) if tls_ca else None,
            tlsAllowInvalidHostnames=_truthy(
                env("MONGODB_TLS_ALLOW_INVALID_HOSTNAMES", "0")),
            tlsAllowInvalidCertificates=_truthy(
                env("MONGODB_TLS_ALLOW_INVALID_CERTIFICATES", "0")),
            # Force single-host connect; otherwise pymongo may try to reach
            # other replica-set members directly, bypassing our tunnel.
            directConnection=True,
            serverSelectionTimeoutMS=15000,
        )
        if tls_cert:
            kwargs["tlsCertificateKeyFile"] = os.path.expanduser(tls_cert)
        if env("MONGODB_USERNAME"):
            kwargs["username"] = env("MONGODB_USERNAME")
            kwargs["password"] = env("MONGODB_PASSWORD")
        if env("MONGODB_AUTH_MECHANISM"):
            kwargs["authMechanism"] = env("MONGODB_AUTH_MECHANISM")
        # Drop None values so pymongo uses its own defaults.
        kwargs = {k: v for k, v in kwargs.items() if v is not None}

        client = MongoClient(**kwargs)
    else:
        uri = env("MONGODB_URI")
        if not uri:
            log("ERROR: neither MONGODB_URI nor SSH_HOST is set. Configure one "
                "(or run with DRY_RUN=1 to test the engine without a database).")
            sys.exit(1)
        client = MongoClient(uri, serverSelectionTimeoutMS=15000)

    client.admin.command("ping")             # fail fast on bad config / network
    coll = client[db_name][coll_name]
    # Prevent duplicate addresses from ever being stored twice.
    coll.create_index([("public_key", ASCENDING)], unique=True)
    log(f">> MongoDB connected: db={db_name!r} collection={coll_name!r}")
    return client, coll, tunnel


def store(coll, address, private_key_b58, secret_key_json):
    """Upsert a verified key. Returns True if it was newly inserted."""
    doc_on_insert = {
        "public_key": address,
        "private_key": private_key_b58,
        "isused": False,
        "secret_key_json": secret_key_json,
        "suffix": SUFFIX,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    }
    result = coll.update_one(
        {"public_key": address},
        {"$setOnInsert": doc_on_insert},
        upsert=True,
    )
    return result.upserted_id is not None


def build_subprocess_env():
    """Make sure the engine can find any co-located libs at runtime.

    On Linux the Makefile build links libcuda-crypt.so by path, so we add the
    binary's directory to LD_LIBRARY_PATH. On Windows the engine is a standalone
    .exe (no DLL), but we still prepend its directory to PATH for safety.
    """
    child_env = dict(os.environ)
    lib_dir = os.path.dirname(VANITY_BIN)  # .../engine/src/release
    if os.name == "nt":
        child_env["PATH"] = lib_dir + os.pathsep + child_env.get("PATH", "")
    else:
        existing = child_env.get("LD_LIBRARY_PATH", "")
        child_env["LD_LIBRARY_PATH"] = (
            lib_dir + (os.pathsep + existing if existing else "")
        )
    return child_env


def build_command():
    """Wrap the binary in stdbuf -oL so matches reach us promptly."""
    # On Windows, os.access(X_OK) is unreliable; just require the file to exist.
    ok = os.path.isfile(VANITY_BIN) and (
        os.name == "nt" or os.access(VANITY_BIN, os.X_OK)
    )
    if not ok:
        build_cmd = ".\\build.bat" if os.name == "nt" else "./build.sh"
        log(f"ERROR: engine binary not found: {VANITY_BIN}")
        log(f"       Build it first:  {build_cmd}")
        sys.exit(1)
    stdbuf = shutil.which("stdbuf")
    if stdbuf:
        return [stdbuf, "-oL", "-eL", VANITY_BIN]
    return [VANITY_BIN]


def main():
    log("=" * 70)
    log("  Solana 'pump' vanity finder  (GPU search -> verify -> MongoDB)")
    log("=" * 70)
    log(f"  suffix       : {SUFFIX!r}")
    log(f"  target count : {TARGET_COUNT if TARGET_COUNT else 'unlimited'}")
    log(f"  engine       : {VANITY_BIN}")
    log(f"  mode         : {'DRY RUN (no DB writes)' if DRY_RUN else 'store to MongoDB'}")
    log("=" * 70)

    client = coll = ssh_tunnel = None
    if not DRY_RUN:
        client, coll, ssh_tunnel = open_collection()

    cmd = build_command()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=build_subprocess_env(),
        text=True,
        bufsize=1,
    )

    stored = 0
    seen_addresses = set()

    def shutdown(*_):
        if proc.poll() is not None:
            return
        log("\n>> Stopping engine...")
        try:
            # Windows can't deliver SIGINT to another process; terminate() maps
            # to TerminateProcess there and SIGTERM on POSIX.
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    signal.signal(signal.SIGINT, lambda *_: (shutdown(), sys.exit(130)))

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            if not line.startswith("FOUND "):
                # Engine status / performance lines: surface them quietly.
                if line.split(":", 1)[0] in ("Attempts", "GPU", "CONFIG", "END"):
                    log("   [engine] " + line)
                continue

            parts = line.split()
            if len(parts) != 3:
                log(f"   [warn] malformed FOUND line ignored: {line!r}")
                continue
            _, address, seed_b58 = parts

            try:
                address, private_key_b58, secret_key_json = verify_keypair(
                    address, seed_b58
                )
            except ValueError as e:
                # This should never happen with a correct build; if it does,
                # the GPU produced a bad key and we refuse to store it.
                log(f"   [REJECTED] {e}")
                continue

            if address in seen_addresses:
                continue
            seen_addresses.add(address)

            if DRY_RUN:
                log(f"   [verified] {address}")
                log(f"              private_key(base58)= {private_key_b58}")
                newly_stored = True
            else:
                newly_stored = store(
                    coll, address, private_key_b58, secret_key_json
                )
                tag = "stored" if newly_stored else "already in DB"
                log(f"   [{tag}] {address}")

            if newly_stored:
                stored += 1
                if TARGET_COUNT and stored >= TARGET_COUNT:
                    log(f"\n>> Reached target of {TARGET_COUNT} key(s).")
                    break
    finally:
        shutdown()
        if client is not None:
            client.close()
        if ssh_tunnel is not None:
            try:
                ssh_tunnel.stop()
            except Exception:
                pass

    log(f">> Done. {stored} key(s) {'verified' if DRY_RUN else 'stored'}.")


if __name__ == "__main__":
    main()
