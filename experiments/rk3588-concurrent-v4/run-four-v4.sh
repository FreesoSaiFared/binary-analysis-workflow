#!/usr/bin/env bash
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd); R=${RUN:-"$H/run"}; rm -rf "$R"; mkdir -p "$R"
L="$H/runtime/lib64/ld-linux-x86-64.so.2"; LP="$H/runtime/lib/x86_64-linux-gnu:$H/runtime/usr/lib/x86_64-linux-gnu"; Q=("$L" --library-path "$LP" "$H/runtime/bin/qemu-system-aarch64")
S="$R/iv.sock"; M="$R/iv.bin"; python3 "$H/ivshmem_server.py" -S "$S" -m "$M" --peers 4 >"$R/server.log" 2>&1 & srv=$!; trap 'kill $srv 2>/dev/null || true; jobs -pr | xargs -r kill 2>/dev/null || true' EXIT
for _ in $(seq 1 100); do [ -S "$S" ] && break; sleep .02; done
p=(); for n in 0 1 2 3; do timeout 45s "${Q[@]}" -M virt -cpu cortex-a76 -smp 2 -m 256M -display none -nodefaults -kernel "$H/guest/Image" -initrd "$H/guest/initramfs.cpio.gz" -append 'console=ttyAMA0 rdinit=/init panic=-1' -chardev socket,path="$S",id=iv$n -device ivshmem-doorbell,vectors=1,ioeventfd=off,chardev=iv$n -serial "file:$R/node$n.log" -monitor none & p+=("$!"); done
rc=0; for x in "${p[@]}"; do wait "$x" || rc=1; done
echo ===SERVER===; cat "$R/server.log"; for f in "$R"/node*.log; do echo "===${f##*/}==="; grep 'RKMESH_V4_' "$f" || true; done
grep -q 'IVSHMEM_ALL_PEERS count=4' "$R/server.log"
test "$(grep -h 'RKMESH_V4_USER_READY' "$R"/node*.log|wc -l)" -eq 4
test "$(grep -h 'RKMESH_V4_OVERLAP_PROVEN' "$R"/node*.log|wc -l)" -eq 3
test "$(grep -h 'RKMESH_V4_DELAY_ISOLATION_PASS' "$R"/node*.log|wc -l)" -eq 1
test "$(grep -h 'RKMESH_V4_INVALIDATE_SEND' "$R"/node*.log|wc -l)" -eq 2
test "$(grep -h 'RKMESH_V4_INVALIDATED id=' "$R"/node*.log|wc -l)" -eq 2
test "$(grep -h 'RKMESH_V4_INVALIDATE_ACK' "$R"/node*.log|wc -l)" -eq 2
test "$(grep -h 'RKMESH_V4_STALE_FAULT' "$R"/node*.log|wc -l)" -eq 2
test "$(grep -h 'RKMESH_V4_PAGE_TRANSFER' "$R"/node*.log|wc -l)" -eq 6
test "$(grep -h 'RKMESH_V4_LEASE_TRANSFER' "$R"/node*.log|wc -l)" -eq 1
test "$(grep -h 'RKMESH_V4_RETRY requester=2.*page=4' "$R"/node*.log|wc -l)" -ge 1
! grep -hEq 'RKMESH_V4_(UNEXPECTED_FAULT|STALE_COLLISION_DATA|SERVICE_BAD|PAGE_BAD|GATE_TIMEOUT|PRIORITY_TIMEOUT|INVALIDATE_TIMEOUT)' "$R"/node*.log
test "$(grep -h 'RKMESH_V4_PASS' "$R"/node*.log|wc -l)" -eq 4
finals=$(grep -h 'RKMESH_V4_PASS' "$R"/node*.log|sed -n 's/.*final=//p'|tr -d '\r'|sort -u); test "$(printf '%s\n' "$finals"|wc -l)" -eq 1
irq_lines=$(grep -h 'RKMESH_V4_IRQ id=' "$R"/node*.log|wc -l); irq_sum=$(grep -h 'RKMESH_V4_PASS' "$R"/node*.log|sed -n 's/.*irq_count=\([0-9]*\).*/\1/p'|awk '{s+=$1}END{print s+0}'); test "$irq_lines" -eq "$irq_sum"
{
 grep 'IVSHMEM_SERVER_READY\|IVSHMEM_ALL_PEERS' "$R/server.log"
 grep -h 'RKMESH_V4_OVERLAP_PROVEN\|RKMESH_V4_DELAY_ISOLATION_PASS\|RKMESH_V4_FAULT id=\|RKMESH_V4_FAULT_RESUMED\|RKMESH_V4_INVALIDATE_SEND\|RKMESH_V4_INVALIDATED id=\|RKMESH_V4_INVALIDATE_ACK\|RKMESH_V4_STALE_FAULT\|RKMESH_V4_RETRY requester=\|RKMESH_V4_PAGE_TRANSFER\|RKMESH_V4_LEASE_TRANSFER\|RKMESH_V4_FINAL\|RKMESH_V4_PASS' "$R"/node*.log | tr -d '\r'
 echo "RKMESH_V4_IRQ_ACCOUNTING lines=$irq_lines pass_sum=$irq_sum"
 echo RKMESH_QEMU_4ARM64_CONCURRENT_SLOTS_V4_PASS
} | tee "$R/RESULT.txt"
exit "$rc"
