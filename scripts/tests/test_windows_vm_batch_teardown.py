from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BATCH_PROVISION = ROOT / "vm/windows-labview/batch-provision.sh"


def test_every_poisoned_failure_is_torn_down_including_final_attempt():
    script = BATCH_PROVISION.read_text(encoding="utf-8")
    loop_start = script.index("for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do")
    loop_end = script.index("\n  done", loop_start)
    loop = script[loop_start:loop_end]

    credential_preserve = loop.index('if [[ "$FAILURE_RETRYABLE" == "0" ]]')
    teardown = loop.index('cleanup_vm_for_retry "$vm_name"')
    retry_gate = loop.index("if (( attempt < MAX_ATTEMPTS )); then")

    assert credential_preserve < teardown < retry_gate
    assert 'FAILURE_REASON="$FAILURE_REASON; automatic poisoned-VM teardown failed"' in loop
    assert 'poisoned VM was discarded.' in script[loop_end:]


def test_poison_teardown_is_identity_bound_and_preserves_shared_media():
    script = BATCH_PROVISION.read_text(encoding="utf-8")
    start = script.index("cleanup_vm_for_retry() {")
    end = script.index("\n}", start)
    cleanup = script[start:end]

    for required in (
        '"$vm_name" != ".."',
        'storage_device="$(findmnt -n -o SOURCE --target "$storage_root")"',
        'STORAGE_ROOT device: $storage_device',
        '! -L "$requested_dir"',
        'actual_name" == "$vm_name"',
        'actual_cfg" == "$expected_cfg"',
        "livesnapshotting|onlinesnapshotting",
        '"$actual_disk" == "$expected_dir"/*',
        '"IDE 0 0" "IDE 1 0" "IDE 1 1" "SATA 1 0" "SATA 2 0"',
        'Location: ".*\\.(iso|viso)"',
        'VBoxManage unregistervm "$uuid" --delete',
        'rm -rf -- "$expected_dir"',
    ):
        assert required in cleanup, required