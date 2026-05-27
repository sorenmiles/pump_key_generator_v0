# Solana `pump` vanity key generator (NVIDIA GPU) → MongoDB

Finds Solana keypairs whose **public key (address) ends with `pump`** — the
suffix used by pump.fun mint addresses — using an NVIDIA GPU, then stores each
verified keypair in MongoDB with `private_key`, `public_key`, and `isused=false`.

```
 ┌─────────────────────┐   FOUND <address> <seed>   ┌──────────────────────────┐
 │  CUDA engine (GPU)  │ ─────────────────────────► │  find_pump_keys.py       │
 │  brute-force search │                            │  re-derive + verify key  │
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

### Prerequisites

- NVIDIA GPU + driver (check in a terminal: `nvidia-smi`)
- **CUDA Toolkit** for Windows (`nvcc --version` works)
- **Visual Studio Build Tools** with the *Desktop development with C++* workload
  — this provides `cl.exe`, which `nvcc` needs as the host compiler.
- Python 3.8+ (`py --version`)

### Build & run

`build.bat` auto-loads the Visual Studio C++ environment, so you can run it
from a normal **PowerShell** (or cmd) prompt; no special "Native Tools" prompt
needed. `cd` into this folder, then:

```powershell
# 1) Python deps for the orchestrator (the CUDA engine has no Python deps)
py -m pip install -r requirements.txt

# 2) Build the engine to a standalone .exe (auto-detects your GPU arch)
.\build.bat
# -> produces engine\src\release\cuda_ed25519_vanity.exe
#    Force the arch in PowerShell with:  $env:GPU_ARCH="sm_75"; .\build.bat

# 3) Configure: copy .env.example to .env and fill it in
#    (either MONGODB_URI for direct/Atlas access, or the SSH_*/TLS block
#     for a private MongoDB behind a bastion — see sections below)
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

Prerequisites: NVIDIA driver (`nvidia-smi`), CUDA Toolkit (`nvcc --version`),
Python 3.8+, and a MongoDB URI (or the SSH tunnel + TLS env vars described
[below](#mongodb--ssh-tunnel--tls-mode)).

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

## GPU utilization & performance

The engine launches a grid that fills **every SM** on the GPU and auto-tunes how
long each kernel runs (aiming ~0.5s) so the card stays busy without tripping
**Windows TDR** (Windows resets the driver if a kernel blocks the display GPU
for ~2 seconds). Watch the engine's own throughput line:

```
Attempts: 6144000 in 0.48 at 12800000 keys/sec (att/thread=20)
```

**“My GPU usage looks low in Task Manager.”** Windows Task Manager shows the
**3D** engine by default, which CUDA does *not* use. Click a GPU graph's
dropdown and pick **Cuda** or **Compute_0**, or — more reliably — run:

```
nvidia-smi -l 1
```

and look at the **GPU-Util %** column while the search runs. That is the real
number. (CPU stays low on purpose: the work is on the GPU; the Python side only
verifies the rare hits.)

- If you see *"display driver stopped responding and has recovered"* (a TDR
  reset), lower the launch size: set `VANITY_ATTEMPTS_PER_THREAD` to a small
  fixed value (e.g. `8`).
- If the GPU does **not** drive a monitor (headless/compute-only), you can push
  throughput by setting a larger fixed `VANITY_ATTEMPTS_PER_THREAD` (e.g.
  `5000`), since TDR doesn't apply.
- Expected on an RTX 2060: a few million keys/sec, so a `pump` (4-char) hit
  typically lands within seconds.

---

## Connecting to a private MongoDB via SSH tunnel + TLS

If your MongoDB is on a private network reachable only through a bastion (and
requires TLS), set the variables below in `.env`. `find_pump_keys.py` will open
the SSH forward itself, then connect pymongo over the tunnel with TLS. In this
mode `MONGODB_URI` is ignored — auth comes from the component vars instead.

```ini
# SSH access to the bastion (private-key auth only; no passwords)
SSH_HOST=bastion.example.com
SSH_PORT=22
SSH_USER=ubuntu
SSH_KEY_PATH=C:/Users/you/.ssh/id_ed25519     # path to your private key
SSH_KEY_PASSPHRASE=                           # only if the key is encrypted

# MongoDB host as seen FROM the bastion
SSH_REMOTE_MONGO_HOST=127.0.0.1
SSH_REMOTE_MONGO_PORT=27017

# TLS to MongoDB
MONGODB_TLS_CA_FILE=C:/path/to/ca.pem         # CA that signed Mongo's cert
MONGODB_TLS_CERT_KEY_FILE=                    # only if mTLS — client cert+key PEM

# MongoDB auth
MONGODB_USERNAME=appuser
MONGODB_PASSWORD=supersecret
MONGODB_AUTH_SOURCE=admin
MONGODB_AUTH_MECHANISM=SCRAM-SHA-256          # optional; pymongo auto-picks if blank

MONGODB_DB=solana
MONGODB_COLLECTION=pump_keys
```

A few practical notes:
- **Host-key check.** SSH the bastion once manually first (`ssh -i <key>
  <user>@<host>`) so its fingerprint lands in your `~/.ssh/known_hosts` —
  otherwise the tunnel refuses to start.
- **Replica sets.** The script sets `directConnection=true` so pymongo doesn't
  try to reach other replica-set members at their advertised addresses (which
  would bypass the tunnel). If you specifically need a replica-set connection,
  forward each member's port individually.
- **Cert paths.** Forward slashes work fine in Windows paths inside `.env`
  (no need to escape backslashes).
- **The CA file is for server verification.** Use `MONGODB_TLS_CERT_KEY_FILE`
  only if your MongoDB requires client certificates (mTLS).

---

## Configuration (environment variables)

All of these can be set in `.env` (auto-loaded) or as real environment variables.

### Search / engine

| Variable | Default | Meaning |
|---|---|---|
| `SUFFIX` | `pump` | Must match the compiled engine (`engine/src/config.h`) |
| `TARGET_COUNT` | `1` | New keys to store before exiting; `0` = run until Ctrl-C |
| `VANITY_BIN` | `engine/src/release/cuda_ed25519_vanity[.exe]` | Path to the compiled engine |
| `DRY_RUN` | `0` | `1` = verify & print only, no DB writes |
| `VANITY_ATTEMPTS_PER_THREAD` | *(auto)* | Force a fixed launch size; blank = adaptive ~0.5s/launch |

### MongoDB — URI mode

Used when `SSH_HOST` is **not** set.

| Variable | Default | Meaning |
|---|---|---|
| `MONGODB_URI` | *(required)* | Standard mongodb:// or mongodb+srv:// URI |
| `MONGODB_DB` | `solana` | Database name |
| `MONGODB_COLLECTION` | `pump_keys` | Collection name |

### MongoDB — SSH-tunnel + TLS mode

Used when `SSH_HOST` is set. `MONGODB_URI` is ignored in this mode.

| Variable | Default | Meaning |
|---|---|---|
| `SSH_HOST` | — | Bastion hostname/IP |
| `SSH_PORT` | `22` | SSH port |
| `SSH_USER` | — | SSH username |
| `SSH_KEY_PATH` | — | Path to your **private** key file |
| `SSH_KEY_PASSPHRASE` | — | Only if the key is encrypted |
| `SSH_REMOTE_MONGO_HOST` | `127.0.0.1` | MongoDB host *as seen from the bastion* |
| `SSH_REMOTE_MONGO_PORT` | `27017` | MongoDB port *as seen from the bastion* |
| `SSH_LOCAL_BIND_PORT` | `0` | `0` = auto-pick a free local port |
| `MONGODB_TLS_CA_FILE` | — | CA cert that signed the MongoDB server cert |
| `MONGODB_TLS_CERT_KEY_FILE` | — | Client cert+key PEM (mTLS only) |
| `MONGODB_TLS_ALLOW_INVALID_HOSTNAMES` | `0` | Debug only — do not leave on |
| `MONGODB_TLS_ALLOW_INVALID_CERTIFICATES` | `0` | Debug only — do not leave on |
| `MONGODB_USERNAME` / `MONGODB_PASSWORD` | — | DB credentials |
| `MONGODB_AUTH_SOURCE` | `admin` | Auth database |
| `MONGODB_AUTH_MECHANISM` | *(auto)* | e.g. `SCRAM-SHA-256` |

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

All changes are confined to the vendored `engine/` tree. The ed25519 / SHA-512 /
base58 cryptography itself is upstream solanity, unchanged.

- **`src/cuda-ecc-ed25519/vanity.cu`**
  - Replaced the case-insensitive 6-char *prefix-anywhere* matcher with a
    **case-sensitive suffix** match (`pump`).
  - Stable, greppable output: `FOUND <address> <seed_base58>`, plus
    `fflush(stdout)` per launch so the host harness sees matches promptly
    (there is no `stdbuf` on Windows).
  - RNG seeded randomly per run from OS entropy so reruns explore new key space.
  - Runs continuously until killed (the host harness stops it on target reached).
  - **Full-SM grid:** upstream launched `maxActiveBlocks` blocks total — only
    one SM's worth. Now launches `maxActiveBlocks × multiProcessorCount` to use
    every SM (was the cause of the very low GPU utilization).
  - **Adaptive launch sizing:** auto-tunes seeds-per-thread-per-launch toward
    ~0.5s/launch so the GPU stays busy while staying well under the 2s
    **Windows TDR** wall. Override with `VANITY_ATTEMPTS_PER_THREAD`.
  - **Standalone-buildable on Windows:** dropped the unused `<pthread.h>` and
    `gpu_ctx.h` includes (which dragged in `pthread_mutex_t`) and the one
    external `ed25519_set_verbose` symbol from the shared lib. `vanity.cu` now
    compiles to a single `.exe` with one `nvcc` invocation (no Makefile / no
    `libcuda-crypt.so` / no `LD_LIBRARY_PATH`).
- **`src/cuda-ecc-ed25519/fixedint.h`** — use real `<stdint.h>` on MSVC / CUDA.
  Upstream's manual `typedef unsigned long uint32_t` collided with MSVC's
  `typedef unsigned int uint32_t`, failing the Windows build.
- **`src/config.h`** — `prefixes[]` → a single `suffix` string.
- **`src/gpu-common.mk`** — modern default GPU arch (upstream `compute_35` /
  `sm_37` was removed in CUDA 12) and dropped `-Werror` so a benign warning on a
  remote toolchain can't fail the build.

---

## Troubleshooting

### Build (Windows)

- **`build.bat: not recognized` in PowerShell** — prefix with `.\` →
  `.\build.bat`. Also use `$env:NAME="value"` instead of `set NAME=value` for
  env vars.
- **`Cannot find compiler 'cl.exe' in PATH`** — install **Build Tools for
  Visual Studio** with the *Desktop development with C++* workload (`build.bat`
  then auto-loads the MSVC environment). Quickest:
  `winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"`.
- **`unsupported Microsoft Visual Studio version`** — your CUDA Toolkit is
  older than your MSVC. Either install a newer CUDA, install an older MSVC
  toolset, or open `build.bat` and add `-allow-unsupported-compiler` to the
  `nvcc` line.

### Build (Linux)

- **`nvcc: command not found`** — install the CUDA Toolkit and add it to
  `PATH`.
- **`Unsupported gpu architecture 'compute_35'`** — old upstream default; run
  `./build.sh` (sets a modern arch from `nvidia-smi`), or pass
  `GPU_ARCHS=sm_75 GPU_PTX_ARCH=compute_75`.
- **`libcuda-crypt.so: cannot open shared object file`** — run via
  `find_pump_keys.py` (it sets `LD_LIBRARY_PATH`), or
  `export LD_LIBRARY_PATH=engine/src/release:$LD_LIBRARY_PATH`.

### Runtime

- **`[REJECTED]` lines** — the GPU produced a key whose seed didn't re-derive
  to the reported address (the verifier rejected it; nothing was stored).
  Usually a build/arch mismatch — rebuild for your exact GPU.
- **`display driver stopped responding and has recovered`** — Windows TDR. The
  adaptive tuner avoids this, but if it happens, set
  `VANITY_ATTEMPTS_PER_THREAD=8` in `.env` to force tiny launches.

### MongoDB / SSH

- **`ServerSelectionTimeoutError`** (URI mode) — check `MONGODB_URI`, network,
  IP allowlist (Atlas).
- **`paramiko.SSHException: ... not found in known_hosts`** — SSH the bastion
  once manually (`ssh -i <key> <user>@<host>`) so its fingerprint is recorded.
- **`AuthenticationException`** (SSH) — wrong `SSH_USER`, wrong key file, or
  the key isn't authorized on the bastion. Test outside the script first:
  `ssh -i <SSH_KEY_PATH> <SSH_USER>@<SSH_HOST>`.
- **`SSLCertVerificationError`** (Mongo TLS) — `MONGODB_TLS_CA_FILE` is wrong
  or doesn't include the chain. To confirm it's hostname-related only,
  temporarily set `MONGODB_TLS_ALLOW_INVALID_HOSTNAMES=1` (do **not** leave on).
- **`Authentication failed`** (Mongo) — check `MONGODB_USERNAME` /
  `MONGODB_PASSWORD` / `MONGODB_AUTH_SOURCE`. If your server requires a
  specific mechanism, set `MONGODB_AUTH_MECHANISM=SCRAM-SHA-256`.

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
