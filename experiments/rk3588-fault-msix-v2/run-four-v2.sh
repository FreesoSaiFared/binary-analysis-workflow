#!/usr/bin/env bash
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd)
R=${RUN:-"$H/run"}
rm -rf "$R"; mkdir -p "$R"
L="$H/runtime/lib64/ld-linux-x86-64.so.2"
LP="$H/runtime/lib/x86_64-linux-gnu:$H/runtime/usr/lib/x86_64-linux-gnu"
Q=("$L" --library-path "$LP" "$H/runtime/bin/qemu-system-aarch64")
S="$R/iv.sock"; M="$R/iv.bin"
python3 "$H/ivshmem_server.py" -S "$S" -m "$M" --peers 4 >"$R/server.log" 2>&1 &
srv=$!
trap 'kill $srv 2>/dev/null || true; jobs -pr | xargs -r kill 2>/dev/null || true' EXIT
for _ in $(seq 1 100); do [ -S "$S" ] && break; sleep .02; done
p=()
for n in 0 1 2 3; do
  "${Q[@]}" -M virt -cpu cortex-a76 -smp 1 -m 192M -display none -nodefaults \
    -kernel "$H/guest/Image" -initrd "$H/guest/initramfs.cpio.gz" \
    -append 'console=ttyAMA0 rdinit=/init panic=-1' \
    -chardev socket,path="$S",id=iv$n \
    -device ivshmem-doorbell,vectors=1,ioeventfd=off,chardev=iv$n \
    -serial "file:$R/node$n.log" -monitor none &
  p+=("$!")
done
rc=0
for x in "${p[@]}"; do wait "$x" || rc=1; done

echo ===SERVER===; cat "$R/server.log"
for f in "$R"/node*.log; do echo "===${f##*/}==="; grep 'RKMESH_V2_' "$f" || true; done

grep -q 'IVSHMEM_ALL_PEERS count=4' "$R/server.log"
test "$(grep -h 'RKMESH_V2_MODULE_LOAD rc=0' "$R"/node*.log | wc -l)" -eq 4
test "$(grep -h 'RKMESH_V2_USER_READY' "$R"/node*.log | wc -l)" -eq 4
test "$(grep -h 'RKMESH_V2_FAULT_SIGNAL' "$R"/node*.log | wc -l)" -eq 3
test "$(grep -h 'RKMESH_V2_REQUEST_DOORBELL' "$R"/node*.log | wc -l)" -eq 3
test "$(grep -h 'RKMESH_V2_REQUEST_IRQ' "$R"/node*.log | wc -l)" -eq 3
test "$(grep -h 'RKMESH_V2_PAGE_TRANSFER' "$R"/node*.log | wc -l)" -eq 3
test "$(grep -h 'RKMESH_V2_RESPONSE_DOORBELL' "$R"/node*.log | wc -l)" -eq 3
test "$(grep -h 'RKMESH_V2_RESPONSE_IRQ' "$R"/node*.log | wc -l)" -eq 3
test "$(grep -h 'RKMESH_V2_IRQ id=' "$R"/node*.log | wc -l)" -eq 6
test "$(grep -h 'RKMESH_V2_ACQUIRE_COMPLETE' "$R"/node*.log | wc -l)" -eq 3
test "$(grep -h 'RKMESH_V2_FAULT_RESUMED' "$R"/node*.log | wc -l)" -eq 3
test "$(grep -h 'RKMESH_V2_PASS' "$R"/node*.log | wc -l)" -eq 4
! grep -hEq 'RKMESH_V2_(FAIL|IRQ_UNEXPECTED|SERVICE_FAIL)' "$R"/node*.log
for n in 1 2 3; do test "$(grep -h "RKMESH_V2_FAULT_SIGNAL id=$n count=1" "$R"/node*.log | wc -l)" -eq 1; done
test "$(grep -h 'RKMESH_V2_PASS' "$R"/node*.log | sed -n 's/.*final=//p' | tr -d '\r' | sort -u)" = '0xc52f003769448c32'
{
  grep 'IVSHMEM_SERVER_READY\|IVSHMEM_ALL_PEERS' "$R/server.log"
  grep -h 'RKMESH_V2_MODULE_LOAD\|RKMESH_V2_READY\|RKMESH_V2_INITIAL_OWNER\|RKMESH_V2_FAULT_SIGNAL\|RKMESH_V2_REQUEST_DOORBELL\|RKMESH_V2_REQUEST_IRQ\|RKMESH_V2_PAGE_TRANSFER\|RKMESH_V2_RESPONSE_DOORBELL\|RKMESH_V2_RESPONSE_IRQ\|RKMESH_V2_ACQUIRE_COMPLETE\|RKMESH_V2_FAULT_RESUMED\|RKMESH_V2_COMMIT\|RKMESH_V2_CHAIN_DONE\|RKMESH_V2_PASS' "$R"/node*.log | tr -d '\r'
  echo RKMESH_QEMU_4ARM64_FAULT_MSIX_V2_PASS
} | tee "$R/RESULT.txt"
exit "$rc"
