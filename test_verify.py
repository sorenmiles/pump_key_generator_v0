#!/usr/bin/env python3
"""
Local correctness test for the verification path (no GPU required).

Confirms that the seed -> keypair derivation used by find_pump_keys.py:
  1. round-trips (the address derived from a keypair's own seed matches), and
  2. agrees byte-for-byte with an INDEPENDENT ed25519 implementation (PyNaCl /
     libsodium), which is the same construction Solana uses.

This is what lets us trust the GPU output: the engine emits a seed, and this
exact derivation re-checks the address before anything is stored.

Run:  PYTHONPATH=<deps> python3 test_verify.py
"""
import base58
import nacl.signing
from solders.keypair import Keypair

import find_pump_keys as app


def derive_with_nacl(seed: bytes) -> str:
    """Independent ed25519: address = base58(public key from seed)."""
    vk = nacl.signing.SigningKey(seed).verify_key
    return base58.b58encode(bytes(vk)).decode()


def main():
    failures = 0
    N = 500
    print(f">> Cross-checking {N} random seeds: solders vs PyNaCl vs verify_keypair()")
    for _ in range(N):
        kp = Keypair()                     # random keypair
        seed = bytes(kp)[:32]              # its 32-byte seed
        seed_b58 = base58.b58encode(seed).decode()
        address = str(kp.pubkey())

        # 1) independent derivation must agree
        if derive_with_nacl(seed) != address:
            print(f"   MISMATCH (nacl): {address}")
            failures += 1
            continue

        # 2) the function the app actually uses must accept and reproduce it
        app.SUFFIX = address[-3:]          # force suffix to pass for this test
        derived, priv_b58, sk_json = app.verify_keypair(address, seed_b58)
        if derived != address:
            print(f"   MISMATCH (verify_keypair): {address}")
            failures += 1
            continue

        # 3) private_key must round-trip back to the same keypair (64 bytes)
        raw = base58.b58decode(priv_b58)
        if len(raw) != 64 or Keypair.from_bytes(raw) != kp:
            print(f"   PRIVATE KEY ROUND-TRIP FAILED: {address}")
            failures += 1
            continue
        if sk_json != list(raw):
            print(f"   secret_key_json mismatch: {address}")
            failures += 1

    # 4) tamper test: a wrong (address, seed) pair must be REJECTED
    print(">> Tamper test: mismatched address/seed must be rejected")
    kp_a, kp_b = Keypair(), Keypair()
    app.SUFFIX = ""  # don't let the suffix check be what trips it
    seed_b_b58 = base58.b58encode(bytes(kp_b)[:32]).decode()
    try:
        app.verify_keypair(str(kp_a.pubkey()), seed_b_b58)  # wrong pairing
        print("   ERROR: tampered pair was NOT rejected")
        failures += 1
    except ValueError:
        print("   OK: tampered pair rejected")

    # 5) suffix enforcement
    print(">> Suffix test: address not ending in suffix must be rejected")
    kp = Keypair()
    app.SUFFIX = "zzzz_definitely_not_a_suffix"
    try:
        app.verify_keypair(str(kp.pubkey()),
                           base58.b58encode(bytes(kp)[:32]).decode())
        print("   ERROR: bad suffix was NOT rejected")
        failures += 1
    except ValueError:
        print("   OK: bad suffix rejected")

    print("-" * 50)
    if failures == 0:
        print(f"ALL CHECKS PASSED ({N} keypairs cross-verified).")
    else:
        print(f"{failures} FAILURE(S).")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
