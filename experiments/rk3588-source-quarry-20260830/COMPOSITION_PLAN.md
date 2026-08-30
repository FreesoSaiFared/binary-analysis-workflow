# RK3588 Source-Composed Next Experiments
Branch: `rk3588-source-quarry-20260830`
Base: proven V10 `50c77211804f321ca5c92791897a397b4ebbf5ba`

## Rule
Do not mutate or weaken V10 to make a quarry candidate fit. Every candidate first enters as a parallel control. Promotion requires preservation of the existing V10 proof invariants.

## Q0 — Replace only the ivshmem server in a control
Source authority:
- QEMU `d2e570cc0f97b936902a5b1b86b73c0f5998b475`
- `contrib/ivshmem-server/*`
- `tests/qtest/ivshmem-test.c`

Composition:
1. build/invoke QEMU's existing `ivshmem-server`; no reimplementation;
2. keep the exact V10 kernel module, init, QEMU machine, guest count, BAR size, `ivshmem-doorbell,vectors=1`, and verifier;
3. run the same migration reliability gate: 20 fresh migrate boots followed by migrate/stay matrix;
4. compare against frozen V10.

Acceptance:
- 20/20 migration boots pass;
- all four guests expose unique logical IDs;
- exact stale wake = 1, valid wake = 1, old-host exclusion = 1, exec runs = 1;
- exact migration totals remain 73,728 task-page bytes + 8,192 state bytes = 81,920;
- stay remains 86,016 task-page bytes + 0 state bytes;
- final generation/checksum remain exact;
- no new application-level choreography;
- upstream server handles disconnect notification at least as well as current custom server.

If all pass: mark `SOURCE_REPLACEMENT_PROVEN: ivshmem_server.py -> qemu ivshmem-server`.
If any fail: preserve current server and record the exact protocol mismatch.

## Q1 — Placement policy mature-code differential
Authority:
- StarPU `c0c6ee6d44bcd4feebfef25b714a190ec18b7907`
- `src/sched_policies/deque_modeling_policy_data_aware.c`

Do not copy the formula into a new local scheduler.
Instead, treat StarPU DMDA/DMDAS as an independent oracle and ask whether V8's byte-only choice is stable when the decision space includes:
- predicted execution completion;
- predicted transfer time;
- data locality;
- model confidence/calibration state;
- migration/state-transfer penalty;
- optionally energy.

Required output for the next simulator policy experiment:
`guest, predicted_compute, predicted_transfer, migration_penalty, confidence, selected`

Negative controls:
- equal compute / unequal locality;
- unequal compute / equal locality;
- missing prediction on one worker;
- transfer contention multiplier;
- alternating locality that previously provoked oscillation.

Promotion criterion:
V8 hysteresis remains only if it is not dominated by the mature data-aware decision under the same observable cost inputs.

## Q2 — DSM selective coherence differential
Authority:
- ArgoDSM `131e36217137b3f180e3640e686ffb144494b00e`
- `src/backend/mpi/coherence.cpp`

Do not copy Argo implementation until its nonstandard license is cleared.
Use its behavior to add differential questions:
- Can a locally cached page survive acquire when this node remains sole writer / valid sharer?
- Can selective release downgrade only dirty pages?
- Can invalidation be restricted to the affected ownership set?
- What bytes/invalidations are saved against blanket invalidation?

Required counters:
`selective_kept, selective_invalidated, dirty_downgrades, diff_bytes, reacquire_faults`.

## Q3 — V11 semantics control
Authority:
- Charm++ `935d1441d17d81506de8d5964772d97321ab88ad`
- `src/ck-core/ck.C`, `src/ck-core/cklocation.C`
Complexity oracle:
- Popcorn Linux `fa78739980898b048338c0db5d762b2e67035f8b`

V11 application contract remains:
`ordinary logical task/continuation call -> same logical execution object`

The application must not contain:
- `RK_PREP_MIGRATION`;
- `RK_EXEC_PARK`;
- explicit remote wake;
- explicit state-transfer request;
- peer ID / destination messaging.

Runtime-visible invariants remain:
- exact 8192-byte state object;
- monotonic generation;
- destination verifies state before valid wake;
- wrong generation wake rejected below application;
- old source cannot execute or commit migrated generation;
- same application path for stay and migrate;
- stay sends zero state-transfer bytes;
- deliberately disabled handoff fails/blocks rather than executing twice.

Strict quarry status:
`BLOCKED_BY_SOURCE_CONSTRAINT` for implementation. Charm++ is a semantic match but not a drop-in to the V10 kernel transport; Popcorn is a broad distributed-thread substrate, not the bounded object we need. Continue quarrying before writing a custom adapter.

Next search fingerprints:
- serialized/migratable object runtime with stable logical object ID;
- C/C++ runtime task API with transparent worker relocation;
- migration constructor + serializer but transport-pluggable;
- bounded continuation/state-machine runtime that can use an existing shared-memory/doorbell backend;
- Linux kernel/user runtime that can resume a serialized work item on another node without process migration.

## Q4 — ARM64 ordering proof lane
Authority:
- Herdtools7 `cadf88b3764279c7c38b115df725da4177d87c32`
- Linux `tools/memory-model`

Before silicon promotion, represent and test at least these publication relationships:
1. state payload visible before state generation/owner publication;
2. verified-state marker visible before valid execution wake;
3. stale wake cannot satisfy the valid generation predicate;
4. destination task execution cannot precede verified-state acquisition.

Run positive and barrier-removed negative-control litmus variants.
Important: LKMM/AArch64 litmus results are ordering evidence, not a substitute for RK3588 cache-maintenance and PCIe device-memory tests.

## Q5 — Real RK3588 RC↔EP gate
Authority:
- Linux `08dbfad3f5040f5bdb6c529da20d6d4e81fefd72`
- `drivers/pci/endpoint/functions/pci-epf-test.c`
- `drivers/misc/pci_endpoint_test.c`
- `drivers/ntb/test/ntb_perf.c`

First board experiments should reuse existing kernel test behavior before custom fabric benchmarking:
- BAR accessibility;
- host -> endpoint WRITE;
- endpoint -> host READ;
- COPY;
- DMA on/off where supported;
- MSI;
- MSI-X;
- doorbell;
- checksum/error detection;
- size sweep;
- sustained transfer stress.

Record separately:
- payload bytes;
- transaction size;
- latency distribution;
- throughput;
- IRQ count/rate;
- retries/errors;
- CPU load;
- temperature;
- power.

Never import QEMU modeled timing into these measurements.

## Q6 — Independent topology/timing oracle
Authority:
- SimGrid mirror `12a63c78cdaa56b78836c95c6a18484f367dca38`

Keep native `rkmesh` as the deterministic functional/policy simulator.
Use SimGrid as an independent cross-check after real link parameters exist:
- same switch/ring/mesh topology;
- measured link latency/bandwidth;
- contention enabled;
- identical workload trace.

The useful test is disagreement:
if native modeled ranking and SimGrid ranking diverge, inspect the missing contention/topology assumption rather than averaging the numbers.

## Promotion order
1. Q0 upstream ivshmem server equivalence.
2. Q4 memory-order model + negative controls.
3. Q1 placement differential.
4. Q2 selective coherence differential.
5. Q5 real RC↔EP hardware baseline.
6. Q6 calibrated topology cross-check.
7. Continue V11 source quarry until the implementation is no longer blocked by the strict source-composition rule.

This ordering reduces custom code first, strengthens the proof model second, and only then expands the architecture.
