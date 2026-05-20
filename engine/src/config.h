#ifndef VANITY_CONFIG
#define VANITY_CONFIG

// ---------------------------------------------------------------------------
// The base58 SUFFIX every generated Solana address must end with.
//
// pump.fun mint addresses end with "pump". This match is CASE-SENSITIVE, so
// only addresses ending in the exact lowercase string below are reported.
//
// Longer suffixes are exponentially rarer: each extra character multiplies the
// expected work by ~58x (1 / 58^len attempts on average).
// ---------------------------------------------------------------------------
__device__ static char const *suffix = "pump";

#endif
