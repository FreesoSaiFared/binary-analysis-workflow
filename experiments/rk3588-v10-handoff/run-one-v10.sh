#!/usr/bin/env bash
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd); MODE=${1:?mode 0=migrate_sleep 1=stay_local}; NAME=$([ "$MODE" = 0 ] && echo migrate_sleep || echo stay_local); R=${RUN_ROOT:-"$H/results"}/$NAME; rm -rf "$R"; mkdir -p "$R"
L="$H/runtime/lib64/ld-linux-x86-64.so.2"; LP="$H/runtime/lib/x86_64-linux-gnu:$H/runtime/usr/lib/x86_64-linux-gnu"; Q=("$L" --library-path "$LP" "$H/runtime/bin/qemu-system-aarch64")
S="$R/iv.sock"; M="$R/iv.bin"; python3 "$H/ivshmem_server.py" -S "$S" -m "$M" --peers 4 >"$R/server.log" 2>&1 & srv=$!; trap 'kill $srv 2>/dev/null || true; jobs -pr | xargs -r kill 2>/dev/null || true' EXIT
for _ in $(seq 1 100); do [ -S "$S" ] && break; sleep .02; done
p=(); for n in 0 1 2 3; do timeout 30s "${Q[@]}" -M virt -cpu cortex-a76 -smp 2 -m 256M -display none -nodefaults -kernel "$H/guest/Image" -initrd "$H/guest/initramfs.cpio.gz" -append "console=ttyAMA0 rdinit=/init panic=-1 v10_mode=$MODE" -chardev socket,path="$S",id=iv$n -device ivshmem-doorbell,vectors=1,ioeventfd=off,chardev=iv$n -serial "file:$R/node$n.log" -monitor none & p+=("$!"); done
rc=0; for x in "${p[@]}"; do wait "$x" || rc=1; done
cat "$R/server.log"; grep -h 'RKMESH_V10_' "$R"/node*.log | tr -d '\r' | tee "$R/events.txt"
grep -q 'IVSHMEM_ALL_PEERS count=4' "$R/server.log"; test "$(grep -h 'RKMESH_V10_PASS' "$R"/node*.log|wc -l)" -eq 4
if [ "$MODE" = 0 ]; then
 test "$(grep -h 'RKMESH_V10_STALE_WAKE_INJECT' "$R"/node*.log|wc -l)" -eq 1
 test "$(grep -h 'RKMESH_V10_STALE_WAKE_IGNORED' "$R"/node*.log|wc -l)" -eq 1
 test "$(grep -h 'RKMESH_V10_VALID_WAKE' "$R"/node*.log|wc -l)" -eq 1
 test "$(grep -h 'RKMESH_V10_STATE_TRANSFER' "$R"/node*.log|wc -l)" -eq 1
 test "$(grep -h 'RKMESH_V10_OLD_HOST_NEGATIVE_PASS' "$R"/node*.log|wc -l)" -eq 1
 grep -q 'mode=migrate_sleep owner=1 gen=1 state_xfers=1 state_bytes=8192 waits=1 wakes=1 stale=1 old_rejects=1 checksum=0x0602e7fe82ac09b0' "$R/events.txt"
else
 ! grep -q 'RKMESH_V10_STATE_TRANSFER' "$R/events.txt"
 grep -q 'mode=stay_local owner=0 gen=1 state_xfers=0 state_bytes=0 waits=0 wakes=0 stale=0 old_rejects=0 checksum=0x0602e7fe82ac09b0' "$R/events.txt"
fi
echo "RKMESH_QEMU_4ARM64_V10_CASE_PASS mode=$NAME" | tee "$R/summary.txt"
exit "$rc"
