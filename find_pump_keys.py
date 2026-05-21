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
    environment variables already set take precedence over the file.
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
            val = val.strip().strip('"').strip("'")
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


def open_collection():
    """Connect to MongoDB and return a collection with a unique index."""
    from pymongo import ASCENDING, MongoClient

    uri = env("MONGODB_URI")
    if not uri:
        log("ERROR: MONGODB_URI is not set. Set it, or run with DRY_RUN=1 to "
            "test the engine without a database.")
        sys.exit(1)

    db_name = env("MONGODB_DB", "solana")
    coll_name = env("MONGODB_COLLECTION", "pump_keys")

    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    client.admin.command("ping")             # fail fast on bad URI/credentials
    coll = client[db_name][coll_name]
    # Prevent duplicate addresses from ever being stored twice.
    coll.create_index([("public_key", ASCENDING)], unique=True)
    log(f">> MongoDB connected: db={db_name!r} collection={coll_name!r}")
    return client, coll


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

    client = coll = None
    if not DRY_RUN:
        client, coll = open_collection()

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
                if line.startswith("Attempts:") or line.startswith("GPU:"):
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

    log(f">> Done. {stored} key(s) {'verified' if DRY_RUN else 'stored'}.")


if __name__ == "__main__":
    main()
