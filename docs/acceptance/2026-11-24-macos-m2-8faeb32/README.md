# M2 Mac verification for the acquisition-method round trip

The tested code is `8faeb32b8d43fe5ed74b901e81ace303756aec2c`. Verification ran in an
isolated, clean checkout on macOS 26.5.2 (arm64), with synthetic media and zero paid
services. Acceptance and smoke used the 60 GiB production floor and dedicated localhost
ports 18865 and 18012. The smoke instance was stopped after verification.

| Check | Result |
| --- | --- |
| Full test suite | Backend 391 passed, 3 skipped (requires a second filesystem). Benchmark 47 passed, 1 skipped. Frontend 36 passed and production build succeeded. |
| Audit regression | 17 passed. The three extended cases failed before the fix and passed with it. |
| Acceptance | 28/28 at the 60 GiB floor. |
| Smoke | 30 passed, 0 failed at the 60 GiB floor. |

The extended cases confirm the acquisition method through the API, downgrade to revision
0004, upgrade to head, and verify that the stored method and analysis decision survive.
They cover missing, ambiguous, and unknown legacy attachment methods.
