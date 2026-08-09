#!/usr/bin/env bash
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd)
mode=${1:?mode 0..3}
names=(neg_hyst neg_greedy pos_hyst pos_stay)
name=${names[$mode]}
R=${RUN_ROOT:-"$H/results"}/"$name"
rm -rf "$R";mkdir -p "$R"
L="$H/runtime/lib64/ld-linux-x86-64.so.2";LP="$H/runtime/lib/x86_64-linux-gnu:$H/runtime/usr/lib/x86_64-linux-gnu";Q=("$L" --library-path "$LP" "$H/runtime/bin/qemu-system-aarch64")
S="$R/iv.sock";M="$R/iv.bin"
python3 "$H/ivshmem_server.py" -S "$S" -m "$M" --peers 4 >"$R/server.log" 2>&1 & srv=$!
trap 'kill $srv 2>/dev/null || true; jobs -pr | xargs -r kill 2>/dev/null || true' EXIT
for _ in $(seq 1 100);do [ -S "$S" ]&&break;sleep .02;done
p=()
for n in 0 1 2 3;do
 timeout 45s "${Q[@]}" -M virt -cpu cortex-a76 -smp 2 -m 256M -display none -nodefaults \
  -kernel "$H/guest/Image" -initrd "$H/guest/initramfs.cpio.gz" \
  -append "console=ttyAMA0 rdinit=/init panic=-1 v8_mode=$mode" \
  -chardev socket,path="$S",id=iv$n -device ivshmem-doorbell,vectors=1,ioeventfd=off,chardev=iv$n \
  -serial "file:$R/node$n.log" -monitor none & p+=("$!")
done
rc=0;for x in "${p[@]}";do wait "$x"||rc=1;done
test "$rc" -eq 0
grep -q 'IVSHMEM_ALL_PEERS count=4' "$R/server.log"
test "$(grep -h 'RKMESH_V8_PASS' "$R"/node*.log|wc -l)" -eq 4
test "$(grep -h 'RKMESH_V8_SUMMARY' "$R"/node*.log|wc -l)" -eq 1
grep -h 'RKMESH_V8_SUMMARY' "$R"/node*.log|tr -d '\r' >"$R/summary.txt"
grep -h 'RKMESH_V8_DECISION' "$R"/node*.log|tr -d '\r' >"$R/decisions.txt"
grep -h 'RKMESH_V8_TASK_RESULT' "$R"/node*.log|tr -d '\r' >"$R/tasks.txt"
grep -h 'RKMESH_V8_MUTATION' "$R"/node*.log|tr -d '\r' >"$R/mutations.txt"
cat "$R/summary.txt"
echo "RKMESH_QEMU_4ARM64_HYSTERESIS_V8_CASE_PASS mode=$name"
