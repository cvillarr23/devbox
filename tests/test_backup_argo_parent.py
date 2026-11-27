import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'devbox-backup.sh'

MOCK_KUBECTL = r'''#!/bin/sh
echo "$*" >> "$MOCK_LOG"
case "$*" in
  *"get application"*) echo '{"selfHeal":true}' ;;
  *"jsonpath={range"*) echo devbox ;;
  *"get deployment"*"spec.replicas"*) echo 1 ;;
  *"get deployment"*"containers[0].image"*) echo img ;;
  *"get deployment"*"go-template"*) echo app=devbox ;;
  *" exec "*) [ -z "$MOCK_FAIL_EXEC" ] || exit 1; echo data ;;
esac
exit 0
'''


def run_backup(tmp_path, *extra, fail_exec=False):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    kubectl = bin_dir / 'kubectl'
    kubectl.write_text(MOCK_KUBECTL)
    kubectl.chmod(0o755)
    log = tmp_path / 'calls.log'
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'MOCK_LOG': str(log), 'HOME': str(tmp_path)}
    if fail_exec:
        env['MOCK_FAIL_EXEC'] = '1'
    result = subprocess.run(
        ['sh', str(SCRIPT), 'k8s', 'backup', '-o', str(tmp_path / 'out'), '--argo-app', 'child', *extra],
        env=env, capture_output=True, text=True,
    )
    calls = log.read_text().splitlines()
    patches = [c for c in calls if ' patch application ' in c]
    return result, calls, patches


def patched(patches):
    return [(p.split('patch application ')[1].split()[0], 'null' in p) for p in patches]


def test_parent_is_paused_first_and_restored_last(tmp_path):
    result, calls, patches = run_backup(tmp_path, '--argo-parent', 'root')
    assert result.returncode == 0, result.stderr
    assert patched(patches) == [('root', True), ('child', True), ('child', False), ('root', False)]
    scale_down = next(i for i, c in enumerate(calls) if '--replicas=0' in c)
    assert calls.index(patches[1]) < scale_down < calls.index(patches[2])


def test_parent_is_restored_when_the_copy_fails(tmp_path):
    result, _, patches = run_backup(tmp_path, '--argo-parent', 'root', fail_exec=True)
    assert result.returncode != 0
    assert patched(patches) == [('root', True), ('child', True), ('child', False), ('root', False)]


def test_without_parent_only_the_app_is_touched(tmp_path):
    result, _, patches = run_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert patched(patches) == [('child', True), ('child', False)]
