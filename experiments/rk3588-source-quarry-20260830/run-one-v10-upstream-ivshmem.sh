#!/usr/bin/env bash
set -euo pipefail

# Source-quarry Q0 wiring harness.
# Functional producer remains frozen V10.  The only changed seam is:
# custom ivshmem_server.py -> pinned upstream QEMU contrib ivshmem-server.
H=$(cd "$(dirname "$0")" && pwd)
mode=${1:?mode 0..1}
names=(migrate stay)
name=${names[$mode]}
R=${RUN_ROOT:-"$H/results"}/"$name"
rm -rf "$R"
mkdir -p "$R"

L="$H/runtime/lib64/ld-linux-x86-64.so.2"
LP="$H/runtime/lib/x86_64-linux-gnu:$H/runtime/usr/lib/x86_64-linux-gnu"
Q=("$L" --library-path "$LP" "$H/runtime/bin/qemu-system-aarch64")

UPSTREAM_SERVER=${UPSTREAM_IVSHMEM_SERVER:-"$H/ivshmem-server"}
test -x "$UPSTREAM_SERVER"

S="$R/iv.sock"
# QEMU's example server -m option takes a directory and creates/unlinks
# its own file-backed shared-memory object inside it.
"$UPSTREAM_SERVER" -F -v -S "$S" -m "$R" -l 16777216 -n 1 >"$R/server.log" 2>&1 &
srv=$!
trap 'kill $srv 2>/dev/null || true; jobs -pr | xargs -r kill 2>/dev/null || true' EXIT

for _ in $(seq 1 200); do
  [ -S "$S" ] && break
  sleep .02
done
test -S "$S"

p=()
for n in 0 1 2 3; do
  timeout 50s "${Q[@]}" \
    -M virt -cpu cortex-a76 -smp 2 -m 256M -display none -nodefaults \
    -kernel "$H/guest/Image" -initrd "$H/guest/initramfs.cpio.gz" \
    -append "console=ttyAMA0 rdinit=/init panic=-1 v10_mode=$mode" \
    -chardev socket,path="$S",id=iv$n \
    -device ivshmem-doorbell,vectors=1,ioeventfd=off,chardev=iv$n \
    -serial "file:$R/node$n.log" -monitor none &
  p+=("$!")
done

rc=0
for x in "${p[@]}"; do
  wait "$x" || rc=1
done
test "$rc" -eq 0

# Protocol-level control evidence: exactly four upstream peer admissions.
test "$(grep -c 'new peer id = ' "$R/server.log")" -eq 4
grep -q '\*\*\* Example code, do not use in production \*\*\*' "$R/server.log"

test "$(grep -h 'RKMESH_V10_PASS' "$R"/node*.log | wc -l)" -eq 4
test "$(grep -h 'RKMESH_V10_SUMMARY' "$R"/node*.log | wc -l)" -eq 1

grep -h 'RKMESH_V10_SUMMARY' "$R"/node*.log | tr -d '\r' >"$R/summary.txt"
grep -h 'RKMESH_V10_DECISION' "$R"/node*.log | tr -d '\r' >"$R/decisions.txt"
grep -h 'RKMESH_V10_TASK_RESULT' "$R"/node*.log | tr -d '\r' >"$R/tasks.txt"
grep -h 'RKMESH_V10_MUTATION' "$R"/node*.log | tr -d '\r' >"$R/mutations.txt"
grep -h 'RKMESH_V10_STATE_TRANSFER' "$R"/node*.log | tr -d '\r' >"$R/state-transfers.txt" || true
grep -hE 'RKMESH_V10_(EXEC_|OLD_HOST_|PARK_)' "$R"/node*.log | tr -d '\r' >"$R/exec-events.txt" || true

cat "$R/summary.txt"
echo "RKMESH_QEMU_UPSTREAM_IVSHMEM_V10_CASE_PASS mode=$name"
