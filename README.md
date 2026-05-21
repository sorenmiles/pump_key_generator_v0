# Solana `pump` vanity key generator (NVIDIA GPU) → MongoDB

Finds Solana keypairs whose **public key (address) ends with `pump`** — the
suffix used by pump.fun mint addresses — using an NVIDIA GPU, then stores each
verified keypair in MongoDB with `private_key`, `public_key`, and `isused=false`.

```
 ┌─────────────────────┐   FOUND <address> <seed>   ┌──────────────────────────┐
 │  CUDA engine (GPU)   │ ─────────────────────────► │  find_pump_keys.py       │
 │  brute-force search  │                            │  re-derive + verify key  │
 └─────────────────────┘                            │  store in MongoDB        │
                                                     └──────────────────────────┘
```

The GPU engine is a patched build of [ChorusOne/solanity](https://github.com/ChorusOne/solanity)
(a proven CUDA ed25519 implementation), so the hard cryptography is battle-tested
rather than hand-written. See [What was changed vs. upstream solanity](#what-was-changed-vs-upstream-solanity).

---

## How it works

A Solana address is `base58(ed25519_public_key)`. There is no shortcut to a chosen
suffix — you must try random seeds until one lands. Matching `pump` (4 base58
chars) takes on average **58⁴ ≈ 11.3 million** attempts, which a modern GPU clears
in seconds to a minute.

For each attempt the GPU: generates a 32-byte seed → `SHA-512` → clamp → ed25519
scalar-multiply → base58-encode → check the last 4 chars equal `pump`.

**The GPU is never trusted blindly.** When the engine reports a hit, the Python
side re-derives the public key from the seed with `solders` (the same ed25519
library Solana uses) and re-checks both that the address matches and that it ends
with the suffix. Only then is it stored. (This re-derivation is unit-tested in
`test_verify.py` against an independent implementation, PyNaCl/libsodium.)

---

## ⚠️ Security — read this

- **The RNG is fast, not cryptographically strong.** The engine seeds CUDA's
  `curand` and then increments. Keys are seeded from OS entropy per run, but this
  is **not** a hardened wallet RNG. Treat generated keys as **disposable mint /
  throwaway keys**, not long-term storage for significant funds.
- **`private_key` is a real secret.** Anyone with it controls the address. Keep
  your `.env` and the database private. `.env` is git-ignored.
- **Verify before funding.** The address is stored only after local
  re-verification, but always sanity-check an address in a wallet before sending
  anything to it.

---

## Windows (native) — recommended for an RTX 2060 on Windows

**Prerequisites**
- NVIDIA GPU + driver (check in a terminal: `nvidia-smi`)
- **CUDA Toolkit** for Windows (`nvcc --version` works)
- **Visual Studio Build Tools** with the *Desktop development with C++* workload
  — this provides `cl.exe`, which `nvcc` needs as the host compiler.
- Python 3.8+ (`py --version`)

**Build & run** — `build.bat` auto-loads the Visual Studio C++ environment, so
you can run it from a normal **PowerShell** (or cmd) prompt; no special
"Native Tools" prompt needed. `cd` into this folder, then:

```powershell
# 1) Python deps for the orchestrator (the CUDA engine has no Python deps)
py -m pip install -r requirements.txt

# 2) Build the engine to a standalone .exe (auto-detects your GPU arch)
.\build.bat
# -> produces engine\src\release\cuda_ed25519_vanity.exe
#    Force the arch in PowerShell with:  $env:GPU_ARCH="sm_75"; .\build.bat

# 3) Configure: copy .env.example to .env and set MONGODB_URI
copy .env.example .env
notepad .env

# 4) Run (find_pump_keys.py auto-loads .env — no "source"/"set" needed)
py find_pump_keys.py
```

> In PowerShell you must prefix scripts with `.\` (`.\build.bat`), and `&` is not
> a command separator — set env vars with `$env:NAME="value"` instead.

Native Windows compiles `vanity.cu` directly into a single self-contained
`.exe` — no Makefile, `.so`, or `LD_LIBRARY_PATH` involved. If `build.bat`
reports it can't find Visual Studio, you must install the C++ tools (below) or
switch to WSL2.

> **Easier alternative:** if you'd rather not install Visual Studio, use **WSL2**
> (Ubuntu) on your Windows machine with the NVIDIA WSL driver + CUDA Toolkit, and
> follow the Linux steps below unchanged. CUDA-on-WSL2 is fully supported.

---

## Linux / WSL2

**Prerequisites:** NVIDIA driver (`nvidia-smi`), CUDA Toolkit (`nvcc --version`),
Python 3.8+, and a MongoDB URI.

```bash
# 1) Python deps
python3 -m pip install -r requirements.txt

# 2) Build the engine (auto-detects your GPU's compute capability)
./build.sh
# -> produces engine/src/release/cuda_ed25519_vanity

# 3) Configure
cp .env.example .env          # edit and set MONGODB_URI

# 4) Run (auto-loads .env)
python3 find_pump_keys.py
```

If `build.sh` can't detect your card, pass the arch explicitly:

```bash
GPU_ARCHS=sm_75 GPU_PTX_ARCH=compute_75 ./build.sh   # RTX 2060 = 75
```

Common compute capabilities: RTX 2060/20xx/T4 = `75`, A100 = `80`,
RTX 30xx = `86`, RTX 40xx/L4 = `89`, H100 = `90`.

---

## Running

The orchestrator launches the GPU search, verifies each hit, stores it, and
exits once `TARGET_COUNT` new keys are stored (default `1`). Set `TARGET_COUNT=0`
to keep mining a pool of unused keys until you press Ctrl-C.

**Test the GPU without a database** (verifies & prints, writes nothing):

```bash
# Linux / WSL2
DRY_RUN=1 TARGET_COUNT=1 python3 find_pump_keys.py
```
```bat
REM Windows
set DRY_RUN=1 & set TARGET_COUNT=1 & py find_pump_keys.py
```

---

## Configuration (environment variables)

| Variable             | Default                                    | Meaning |
|----------------------|--------------------------------------------|---------|
| `MONGODB_URI`        | *(required unless `DRY_RUN=1`)*            | Mongo connection string |
| `MONGODB_DB`         | `solana`                                   | Database name |
| `MONGODB_COLLECTION` | `pump_keys`                                | Collection name |
| `SUFFIX`             | `pump`                                      | Must match the engine build (see below) |
| `TARGET_COUNT`       | `1`                                         | New keys to store before exiting; `0` = forever |
| `VANITY_BIN`         | `engine/src/release/cuda_ed25519_vanity`   | Path to the compiled engine |
| `DRY_RUN`            | `0`                                         | `1` = verify & print only, no DB |

---

## What gets stored

One document per address (unique index on `public_key` prevents duplicates):

```json
{
  "public_key":  "5x9...pump",
  "private_key": "<base58 of the 64-byte keypair — import this into Phantom/Solflare>",
  "isused":      false,
  "secret_key_json": [12, 34, ...],   // 64 ints: the solana-keygen file format
  "suffix":      "pump",
  "created_at":  "2026-05-20T00:00:00Z"
}
```

`private_key` is the standard wallet-import format (base58 of `seed||pubkey`).
`secret_key_json` is the array form you can save as a `*.json` keypair file for
the Solana CLI.

---

## Changing the suffix

The suffix is compiled into the GPU kernel for speed. To search for something
else (e.g. `moon`):

1. Edit `engine/src/config.h` → `suffix = "moon";`
2. Rebuild: `./build.sh`
3. Run with a matching `SUFFIX=moon` so the verifier agrees.

Longer suffixes are exponentially slower (~58× per extra character).

---

## What was changed vs. upstream solanity

All changes are confined to the vendored `engine/` tree:

- **`src/cuda-ecc-ed25519/vanity.cu`** — replaced the case-insensitive 6-char
  *prefix-anywhere* matcher with a **case-sensitive suffix** match; emit a stable
  `FOUND <address> <seed_base58>` line; seed the RNG randomly per run so reruns
  explore new key space; run continuously until stopped.
- **`src/config.h`** — `prefixes[]` → a single `suffix` string.
- **`src/gpu-common.mk`** — modern default GPU arch (upstream `compute_35`/`sm_37`
  was removed in CUDA 12) and dropped `-Werror` so a benign warning can't fail the
  build.

The ed25519 / SHA-512 / base58 cryptography is upstream solanity, unchanged.

---

## Troubleshooting

- **`nvcc: command not found`** — install the CUDA Toolkit and add it to `PATH`.
- **`Unsupported gpu architecture 'compute_35'`** — you're on an old build; run
  `./build.sh` (it sets a modern arch), or pass `GPU_ARCHS`/`GPU_PTX_ARCH`.
- **`error while loading shared libraries: libcuda-crypt.so`** — run via
  `find_pump_keys.py` (it sets `LD_LIBRARY_PATH`), or
  `export LD_LIBRARY_PATH=engine/src/release:$LD_LIBRARY_PATH`.
- **`[REJECTED]` lines** — the GPU produced a key that failed local
  re-verification (it is *not* stored). This indicates a build/arch mismatch;
  rebuild for your exact GPU.
- **Mongo `ServerSelectionTimeoutError`** — check `MONGODB_URI`, network, and IP
  allowlist (Atlas).

---

## Verifying the Python path locally (no GPU needed)

```bash
python3 -m pip install -r requirements.txt pynacl
python3 test_verify.py
```

Cross-checks the seed→address derivation against PyNaCl, confirms private-key
round-trips, and confirms tampered/bad-suffix keys are rejected.

---

## License

The `engine/` directory is derived from ChorusOne/solanity; see
`engine/LICENSE` and `engine/src/cuda-ecc-ed25519/license.txt`.
