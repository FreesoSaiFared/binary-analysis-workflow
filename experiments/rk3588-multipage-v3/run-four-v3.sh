#!/usr/bin/env bash
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd); R=${RUN:-"$H/run"}; rm -rf "$R"; mkdir -p "$R"
L="$H/runtime/lib64/ld-linux-x86-64.so.2"; LP="$H/runtime/lib/x86_64-linux-gnu:$H/runtime/usr/lib/x86_64-linux-gnu"; Q=("$L" --library-path "$LP" "$H/runtime/bin/qemu-system-aarch64")
S="$R/iv.sock"; M="$R/iv.bin"; python3 "$H/ivshmem_server.py" -S "$S" -m "$M" --peers 4 >"$R/server.log" 2>&1 & srv=$!; trap 'kill $srv 2>/dev/null || true; jobs -pr | xargs -r kill 2>/dev/null || true' EXIT
for _ in $(seq 1 100); do [ -S "$S" ] && break; sleep .02; done
p=(); for n in 0 1 2 3; do timeout 35s "${Q[@]}" -M virt -cpu cortex-a76 -smp 2 -m 256M -display none -nodefaults -kernel "$H/guest/Image" -initrd "$H/guest/initramfs.cpio.gz" -append 'console=ttyAMA0 rdinit=/init panic=-1' -chardev socket,path="$S",id=iv$n -device ivshmem-doorbell,vectors=1,ioeventfd=off,chardev=iv$n -serial "file:$R/node$n.log" -monitor none & p+=("$!"); done
rc=0; for x in "${p[@]}"; do wait "$x" || rc=1; done
echo ===SERVER===; cat "$R/server.log"; for f in "$R"/node*.log; do echo "===${f##*/}==="; grep 'RKMESH_V3_' "$f" || true; done
grep -q 'IVSHMEM_ALL_PEERS count=4' "$R/server.log"
test "$(grep -h 'RKMESH_V3_USER_READY' "$R"/node*.log|wc -l)" -eq 4
test "$(grep -h 'RKMESH_V3_FAULT id=' "$R"/node*.log|wc -l)" -eq 4
test "$(grep -h 'RKMESH_V3_INVALIDATE_SEND' "$R"/node*.log|wc -l)" -eq 2
test "$(grep -h 'RKMESH_V3_INVALIDATED id=' "$R"/node*.log|wc -l)" -eq 2
test "$(grep -h 'RKMESH_V3_INVALIDATE_ACK' "$R"/node*.log|wc -l)" -eq 2
test "$(grep -h 'RKMESH_V3_STALE_FAULT' "$R"/node*.log|wc -l)" -eq 2
test "$(grep -h 'RKMESH_V3_PAGE_TRANSFER' "$R"/node*.log|wc -l)" -eq 4
test "$(grep -h 'RKMESH_V3_LEASE_TRANSFER' "$R"/node*.log|wc -l)" -eq 3
test "$(grep -h 'RKMESH_V3_PASS' "$R"/node*.log|wc -l)" -eq 4
! grep -hEq 'RKMESH_V3_(SERVICE_FAIL|OWNER_FAIL|PAGE_FAIL|IRQ_UNEXPECTED|INVALIDATE_TIMEOUT|LEASE_OWNER_FAIL)' "$R"/node*.log
finals=$(grep -h 'RKMESH_V3_PASS' "$R"/node*.log|sed -n 's/.*final=//p'|tr -d '\r'|sort -u); test "$(printf '%s\n' "$finals"|wc -l)" -eq 1
{
 grep 'IVSHMEM_SERVER_READY\|IVSHMEM_ALL_PEERS' "$R/server.log"
 grep -h 'RKMESH_V3_USER_READY\|RKMESH_V3_FAULT id=\|RKMESH_V3_FAULT_RESUMED\|RKMESH_V3_INVALIDATE_SEND\|RKMESH_V3_INVALIDATED id=\|RKMESH_V3_INVALIDATE_ACK\|RKMESH_V3_STALE_FAULT\|RKMESH_V3_PAGE_TRANSFER\|RKMESH_V3_LEASE_TRANSFER\|RKMESH_V3_FINAL\|RKMESH_V3_PASS' "$R"/node*.log | tr -d '\r'
 echo RKMESH_QEMU_4ARM64_MULTIPAGE_INVALIDATION_V3_PASS
} | tee "$R/RESULT.txt"
exit "$rc"
