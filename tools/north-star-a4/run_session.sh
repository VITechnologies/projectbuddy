#!/bin/sh
# Run one authoring session of north-star A4-r1 in a built workspace (a4.py workspace).
# Usage: run_session.sh <workspace> <item, for example VITechnologies/projectbuddy#379>
set -eu
ws=$(cd "$1" && pwd)
item=$2
state="$HOME/.copilot/session-state"
model=claude-opus-5.5
expected=$(cat "$ws/task.sha256")
mkdir -p "$ws/tmp"
ls "$state" | sort > "$ws/sessions-before.txt"
start=$(date -u +%FT%TZ)
cd "$ws"
TMPDIR="$ws/tmp" timeout 14400 copilot -p "$(cat TASK.md)" --model "$model" --no-ask-user --no-auto-update \
  --allow-all-tools --disable-builtin-mcps --no-custom-instructions \
  --deny-tool 'shell(git push)' --deny-tool 'shell(gh:*)' --deny-tool 'shell(glab:*)' --deny-tool 'shell(curl:*)' --deny-tool 'shell(wget:*)' \
  --share "$ws/transcript.md" > "$ws/copilot-output.log" 2>&1 &
pid=$!
sid=""
i=0
while [ -z "$sid" ] && [ $i -lt 60 ]; do
  sleep 2
  ls "$state" | sort > "$ws/sessions-after.txt"
  sid=$(comm -13 "$ws/sessions-before.txt" "$ws/sessions-after.txt" | while read -r d; do
    if grep -q "$ws" "$state/$d/events.jsonl" 2>/dev/null; then echo "$d"; fi
  done | head -1)
  i=$((i + 1))
done
echo "session=$sid"
if [ -n "$sid" ]; then
  projectbuddy-kpi mark --session "$sid" --item "$item" --port 55432 --password-file "$HOME/.config/rsr-registry-local/postgres-password" || true
fi
rc=0
wait $pid || rc=$?
end=$(date -u +%FT%TZ)
cat > "$ws/run-record.json" <<EOF
{"workspace": "$ws", "model": "$model", "session": "$sid", "start": "$start", "end": "$end", "exit": $rc, "task_sha256": "$expected"}
EOF
echo "exit=$rc session=$sid"
