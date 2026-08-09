#!/usr/bin/env bash
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd)
scenario=${1:?scenario 0|1}
candidate=${2:?candidate 0..4(auto)}
name="s${scenario}-c${candidate}"
R=${RUN_ROOT:-"$H/results"}/"$name"
rm -rf "$R"; mkdir -p "$R"
L="$H/runtime/lib64/ld-linux-x86-64.so.2"
LP="$H/runtime/lib/x86_64-linux-gnu:$H/runtime/usr/lib/x86_64-linux-gnu"
Q=("$L" --library-path "$LP" "$H/runtime/bin/qemu-system-aarch64")
S="$R/iv.sock"; M="$R/iv.bin"
python3 "$H/ivshmem_server.py" -S "$S" -m "$M" --peers 4 >"$R/server.log" 2>&1 & srv=$!
trap 'kill $srv 2>/dev/null || true; jobs -pr | xargs -r kill 2>/dev/null || true' EXIT
for _ in $(seq 1 100); do [ -S "$S" ] && break; sleep .02; done
p=()
for n in 0 1 2 3; do
  timeout 35s "${Q[@]}" -M virt -cpu cortex-a76 -smp 2 -m 256M -display none -nodefaults \
    -kernel "$H/guest/Image" -initrd "$H/guest/initramfs.cpio.gz" \
    -append "console=ttyAMA0 rdinit=/init panic=-1 v7_scenario=$scenario v7_candidate=$candidate" \
    -chardev socket,path="$S",id=iv$n -device ivshmem-doorbell,vectors=1,ioeventfd=off,chardev=iv$n \
    -serial "file:$R/node$n.log" -monitor none & p+=("$!")
done
rc=0; for x in "${p[@]}"; do wait "$x" || rc=1; done
test "$rc" -eq 0
grep -q 'IVSHMEM_ALL_PEERS count=4' "$R/server.log"
test "$(grep -h 'RKMESH_V7_USER_READY' "$R"/node*.log|wc -l)" -eq 4
test "$(grep -h 'RKMESH_V7_PASS' "$R"/node*.log|wc -l)" -eq 4
test "$(grep -h 'RKMESH_V7_TASK_RESULT' "$R"/node*.log|wc -l)" -eq 1
test "$(grep -h 'RKMESH_V7_COST_TABLE' "$R"/node*.log|wc -l)" -eq 1
! grep -hEq 'RKMESH_V7_(UNEXPECTED|TIMEOUT|BAD|REJECT)' "$R"/node*.log
grep -h 'RKMESH_V7_COST_TABLE' "$R"/node*.log | tr -d '\r' > "$R/cost.txt"
grep -h 'RKMESH_V7_TASK_RESULT' "$R"/node*.log | tr -d '\r' > "$R/task.txt"
grep -h 'RKMESH_V7_PASS' "$R"/node*.log | tr -d '\r' > "$R/pass.txt"
cat "$R/cost.txt"; cat "$R/task.txt"
echo "RKMESH_QEMU_4ARM64_ADAPTIVE_PLACEMENT_V7_CASE_PASS scenario=$scenario candidate=$candidate"
