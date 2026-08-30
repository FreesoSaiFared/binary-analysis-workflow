# RK3588 Compute Fabric — Source Quarry Provenance Ledger
Date: 2026-08-30
Base proof: `rk3588-v10-exec-wake-fix1` @ `50c77211804f321ca5c92791897a397b4ebbf5ba`
Quarry guide: `Code Quarrying Living Guide — Master` v1.0.0, Drive `18g55KHIN2VUNtZznRRb_XtYVa8-SQuNm2ghc5XnipZk`

## Evidence boundary
This ledger does not upgrade any simulation result into RK3588 silicon evidence. QEMU ARM `virt` remains a software-contract environment, not RK3588 emulation.

## Authoritative V10 source snapshot
The canonical compact Drive capsule `RK3588_HARDWARE_MASTER_STATE_2026-08-11_V10.zip` was re-fetched during this quarry and independently SHA-checked:
- capsule SHA-256: `d0baa9eed1949e83428422a9afb503a5e3db15d938a1a2824a58eb8fbd1462e7`
- `SOURCE/rkmesh_v10.c`: `d2569685c5e680e4a94b6befe63dd7c49ae088d3757aee807c962231680fa4d6`
- `SOURCE/init_v10.c`: `09ddd3da8c18342a53923687b84cb38713bcb242ddf024d03346e26bff9a5eef`
- `SOURCE/ivshmem_server.py`: `c7aa2afe1bcbaed5e33dd11aa498553cb47f2417e5fda1f053c68f79bc6a0844`
- all capsule manifest entries verified.

Observed V10 application-level abstraction leak:
- `init_v10.c::prepare_park()` explicitly invokes `RK_PREP_MIGRATION`, forks, then invokes `RK_EXEC_PARK`.
- the application loop explicitly waits for the park marker before the destination executes `run_task()`.
- this is the exact choreography V11 must hide without weakening stale-wake rejection or old-host exclusion.

## Quarry results

### QEMU ivshmem — DIRECT REPLACEMENT CANDIDATE
Repository: `qemu/qemu`
Pinned commit: `d2e570cc0f97b936902a5b1b86b73c0f5998b475`
Source:
- `contrib/ivshmem-server/ivshmem-server.c`
- `contrib/ivshmem-server/main.c`
- `hw/misc/ivshmem-pci.c`
- `tests/qtest/ivshmem-test.c`
Observed behavior:
- upstream server sends protocol version, peer ID, shared-memory FD and peer vector eventfds over the Unix socket.
- `ivshmem_io_write()` decodes destination from bits 31:16 and vector from low bits, then signals the destination eventfd.
- upstream peer teardown advertises the disconnect to the remaining peers.
- upstream qtests verify distinct VM IDs, vector count and inter-VM MSI-X doorbells.
Mapping to V10:
- current 1,598-byte `ivshmem_server.py` reproduces the same core handshake for one vector and four peers.
- current custom server removes a disconnected peer locally but does not implement the upstream disconnect advertisement to remaining peers.
Decision: **ADOPT-CANDIDATE**, but only in a new control lane. Do not modify frozen V10 until the complete 20-boot + matrix proof is reproduced using the upstream server.

### StarPU DMDA/DMDAS — PLACEMENT / HYSTERESIS ORACLE
Repository: `starpu-runtime/starpu` (GitHub mirror of Inria development repo)
Pinned commit: `c0c6ee6d44bcd4feebfef25b714a190ec18b7907`
License metadata: LGPL-2.1
Source:
- `src/sched_policies/deque_modeling_policy_data_aware.c`
- `src/datawizard/coherency.c`
Observed behavior:
- per worker, StarPU predicts task execution time and data-transfer penalty.
- DMDA chooses using a fitness containing execution completion cost plus weighted transfer cost; energy can be a third term.
- when a model is unavailable it deliberately falls back to a greedy calibration path instead of pretending prediction exists.
Mapping to V7/V8:
- V10's `best_guest()` currently minimizes only modeled remote-page bytes.
- mature comparison should separate predicted compute cost, transfer cost, confidence/calibration state and migration penalty.
Decision: **CONTROL-ORACLE**. Do not transliterate StarPU into new home-grown scheduler code. Use its behavior as the mature baseline against which the next placement experiment is judged.

### ArgoDSM — COHERENCE / SELECTIVE INVALIDATION ORACLE
Repository: `etascale/argodsm`
Pinned commit: `131e36217137b3f180e3640e686ffb144494b00e`
Repository license metadata: NOASSERTION; `src/backend/mpi/coherence.cpp` declares the Eta Scale Open Source License. Direct source reuse therefore requires license review.
Source:
- `src/backend/mpi/coherence.cpp`
- `src/backend/mpi/swdsm.cpp`
Observed behavior:
- selective acquire checks sharer/writer metadata before invalidating.
- pages that remain valid are retained; pages requiring invalidation are marked invalid and protected with `mprotect(PROT_NONE)`.
- selective release downgrades dirty pages and emits diffs/write-buffer state before returning them clean.
Mapping to V1–V4:
- validates the direction of ownership + invalidation + fault-driven reacquisition, while showing that blanket invalidation is not the mature endpoint.
Decision: **CONTROL-ORACLE / FAILURE-MODE SOURCE**, not direct reuse until licensing and backend mismatch are resolved.

### Charm++ — V11 BOUNDED MIGRATABLE-OBJECT SEMANTIC ORACLE
Repository: `charmplusplus/charm`
Pinned commit: `935d1441d17d81506de8d5964772d97321ab88ad`
License: Apache-2.0
Source:
- `src/ck-core/ck.C`
- `src/ck-core/cklocation.C`
Observed behavior:
- migratable objects use migration constructors (`CkMigrateMessage*`) and PUP serialization/deserialization.
- logical object interaction is separated from physical placement; location metadata can change while the object remains the same logical target.
- runtime code reconstructs migratable state rather than forcing application code to manually send each state fragment.
Mapping to V11:
- the closest mature semantic match to the required “same ordinary task/continuation operation, runtime hides placement/movement” boundary.
Decision: **V11 SEMANTIC CONTROL-ORACLE**. Charm++ is not a drop-in transport for the existing kernel driver, so copying its mechanism into V10 would violate the quarry constraint.

### Popcorn Linux — TRANSPARENT EXECUTION COMPLEXITY / NEGATIVE ORACLE
Repository: `ssrg-vt/popcorn-kernel`
Pinned commit: `fa78739980898b048338c0db5d762b2e67035f8b`
Source:
- `kernel/popcorn/process_server.c`
- `kernel/popcorn/page_server.c`
- `kernel/popcorn/vma_server.c`
- `kernel/popcorn/wait_station.c`
Observed behavior:
- implements distributed thread execution with remote contexts, page/VMA service, remote futex handling, wait stations, and architecture-specific process-server support.
- its public issue history includes recursive-fault / hang behavior around unsupported remote thread creation, illustrating the failure surface added by general transparent process semantics.
Mapping to V11:
- proves that general distributed-thread transparency is possible, but also demonstrates why V11 should remain the bounded 8192-byte continuation primitive instead of expanding into arbitrary process migration.
Decision: **REJECT-AS-TOO-BROAD for V11 implementation; KEEP AS FAILURE ORACLE**.

### Linux PCI Endpoint Test — REAL RK3588 RC↔EP ACCEPTANCE ORACLE
Repository: `torvalds/linux`
Pinned commit: `08dbfad3f5040f5bdb6c529da20d6d4e81fefd72`
Source:
- `drivers/pci/endpoint/functions/pci-epf-test.c`
- `drivers/misc/pci_endpoint_test.c`
- `Documentation/PCI/endpoint/pci-test-howto.rst`
Observed behavior:
- existing endpoint/host test contract covers READ, WRITE, COPY, DMA use, INTx/MSI/MSI-X and doorbell operations.
- exposes concrete size/checksum/IRQ inputs rather than inferred “PCIe performance.”
Decision: **ADOPT AS FIRST REAL-HARDWARE MEASUREMENT CONTRACT** before custom fabric benchmarking.

### Linux NTB perf — PEER-WINDOW / DMA THROUGHPUT ORACLE
Repository: `torvalds/linux`
Pinned commit: `08dbfad3f5040f5bdb6c529da20d6d4e81fefd72`
Source:
- `drivers/ntb/test/ntb_perf.c`
Observed behavior:
- mature peer-memory-window throughput test with chunk sizing, transfer sizing, DMA option and multiple worker threads.
Mapping:
- not proof that RK3588 is NTB hardware.
- useful measurement vocabulary and stress-pattern source for the real RC↔EP test lane.
Decision: **CONTROL-ORACLE**.

### Herdtools7 + LKMM — ARM64 MEMORY-ORDER PROOF LANE
Repository: `herd/herdtools7`
Pinned commit: `cadf88b3764279c7c38b115df725da4177d87c32`
Source:
- AArch64 `.cat` models under `herd/libdir/aarch64/`
- Linux kernel litmus infrastructure under `tools/memory-model` in Linux.
Observed behavior:
- herd explores allowed/forbidden outcomes under an explicit memory model instead of assuming source-order visibility.
Mapping:
- V10 uses `wmb()`, MMIO `writel()`, shared BAR state and interrupt delivery.
- QEMU functional success does not prove the same ordering/cache-maintenance behavior on RK3588 silicon.
Decision: **ADOPT AS INDEPENDENT MEMORY-ORDER GATE** before claiming real-board coherence protocol correctness.

### SimGrid — TIMING / TOPOLOGY CROSS-CHECK
Repository mirror: `simgrid/simgrid`
Pinned mirror commit: `12a63c78cdaa56b78836c95c6a18484f367dca38`
Upstream development is on FramaGit; GitHub is a mirror.
Observed behavior:
- supports explicit link bandwidth/latency and contention-aware distributed-system simulation.
Mapping:
- current `rkmesh` native simulator is intentionally dependency-light and policy-focused.
- SimGrid should be an independent oracle for network/topology sensitivity, not a reason to delete the deterministic functional simulator.
Decision: **CONTROL-ORACLE**.

## Quarry conclusion
The highest-value immediate replacement is the custom ivshmem server, because an upstream implementation already exists at exactly that seam. The highest-value V11 source lesson is the opposite: neither Popcorn nor Charm++ is a drop-in component for the existing kernel/QEMU transport. Under strict quarry rules, creating a new adapter that merely imitates them is not justified.

Therefore:
1. preserve proven V10 unchanged;
2. create an upstream-QEMU-server V10 control and require exact proof equivalence;
3. make StarPU/Argo/Charm/Popcorn/Herd/SimGrid independent oracles;
4. move the real-hardware boundary onto Linux's existing PCI endpoint test contract;
5. mark a source-composed V11 runtime implementation **BLOCKED_BY_SOURCE_CONSTRAINT** until a directly reusable bounded-continuation component is located or the constraint is explicitly relaxed.
