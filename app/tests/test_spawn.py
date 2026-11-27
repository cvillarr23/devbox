"""devbox spawn: argument checks, worktrees, limits, parent link."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import devbox_store as store
import runtime
import server
import spawn


def test_plan_defaults_and_validation():
    assert spawn.plan({})['agent'] == 'claude'
    bad = [{'agent': 'vim'}, {'model': 'gpt'}, {'agent': 'codex', 'permission_mode': 'plan'},
           {'permission_mode': 'yolo'}, {'name': 'a b'}, {'cwd': '/x', 'worktree': 'b'},
           {'worktree': '../x'}, {'worktree': '-rf'}, {'prompt': 'a\0b'}]
    for args in bad:
        with pytest.raises(ValueError):
            spawn.plan(args)
    assert spawn.plan({'agent': 'codex', 'model': 'gpt-5.1'})['model'] == 'gpt-5.1'


def test_child_prompt_names_parent_and_protocol():
    text = spawn.child_prompt('do it', 'boss:3')
    assert text.startswith('do it\n\n')
    assert '`boss`' in text and '--to parent --kind question' in text and '--kind done' in text
    assert spawn.child_prompt('do it', '') == 'do it'


def test_child_name_skips_taken():
    assert spawn.child_name('boss', {'boss-1', 'boss-2'}) == 'boss-3'
    assert spawn.child_name(None, set()) == 'spawn-1'


def test_agent_script_inlines_only_validated_tokens():
    script = spawn.agent_script('claude', 'sonnet', 'plan', "/s/it's/prompt.md")
    assert script.startswith('claude --model sonnet --permission-mode plan -- "$(cat ')
    assert "'/s/it'\\''s/prompt.md'" in script


def test_layout_escapes_kdl_strings():
    text = spawn.layout('/w"x', 'echo "$(a)" \\')
    assert 'cwd="/w\\"x"' in text and 'args "-c" "echo \\"$(a)\\" \\\\"' in text


def git(cwd, *argv):
    subprocess.run(['git', '-C', str(cwd), *argv], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'proj'
    path.mkdir()
    git(path, 'init', '-q', '-b', 'main')
    git(path, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'init')
    return path


def test_create_worktree_new_and_existing_branch(repo):
    path = spawn.create_worktree(str(repo / '.'), 'feat/x')
    assert path == str(repo.parent / 'proj-feat-x') and Path(path, '.git').exists()
    git(repo, 'branch', 'older')
    assert Path(spawn.create_worktree(str(repo), 'older')).is_dir()
    with pytest.raises(ValueError):
        spawn.create_worktree(str(repo), 'feat/x')
    with pytest.raises(ValueError):
        spawn.create_worktree(str(repo.parent), 'nope')


def _claude_config(tmp_path, monkeypatch, projects):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    config = tmp_path / 'home' / '.claude.json'
    config.parent.mkdir()
    config.write_text(json.dumps({'other': 1, 'projects': projects}))
    return config


def test_worktree_inherits_trust_from_trusted_repo(repo, tmp_path, monkeypatch):
    top = str(repo.resolve())
    config = _claude_config(tmp_path, monkeypatch, {top: {'hasTrustDialogAccepted': True, 'x': 1}, '/o': {}})
    path = spawn.create_worktree(top, 'feat/t')
    data = json.loads(config.read_text())
    assert data['projects'][path] == {'hasTrustDialogAccepted': True}
    assert data['projects'][top]['x'] == 1 and data['projects']['/o'] == {} and data['other'] == 1


def test_worktree_inherits_trust_when_spawned_from_another_worktree(repo, tmp_path, monkeypatch):
    config = _claude_config(tmp_path, monkeypatch, {str(repo.resolve()): {'hasTrustDialogAccepted': True}})
    first = spawn.create_worktree(str(repo), 'feat/a')
    second = spawn.create_worktree(first, 'feat/b')
    assert json.loads(config.read_text())['projects'][second] == {'hasTrustDialogAccepted': True}


def test_worktree_does_not_inherit_without_trust(repo, tmp_path, monkeypatch):
    config = _claude_config(tmp_path, monkeypatch, {str(repo.resolve()): {'hasTrustDialogAccepted': False}})
    before = config.read_text()
    spawn.create_worktree(str(repo), 'feat/u')
    assert config.read_text() == before


@pytest.mark.parametrize('kind', ['missing', 'invalid', 'symlink'])
def test_worktree_trust_skips_unusable_config(repo, tmp_path, monkeypatch, kind):
    config = _claude_config(tmp_path, monkeypatch, {str(repo.resolve()): {'hasTrustDialogAccepted': True}})
    if kind == 'missing':
        config.unlink()
    elif kind == 'invalid':
        config.write_text('{nope')
    else:
        real = tmp_path / 'real.json'
        real.write_text(config.read_text())
        config.unlink()
        config.symlink_to(real)
        config = real
    before = config.read_text() if config.exists() else None
    assert Path(spawn.create_worktree(str(repo), 'feat/v')).is_dir()
    assert (config.read_text() if config.exists() else None) == before


@pytest_asyncio.fixture
async def gateway(tmp_path, monkeypatch, repo):
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('DEVBOX_ENVIRONMENTS', str(tmp_path / 'environments.yaml'))
    monkeypatch.setenv('DEVBOX_WORKSPACE', str(repo))
    started = []
    monkeypatch.setattr(spawn, 'start_agent', lambda name, cwd, spec, prompt: started.append((name, cwd, spec, prompt)))
    rt = runtime.Runtime()

    async def fake_ensure(name, target='local', cwd=''):
        rt.sessions[name] = {'name': name, 'port': 7700 + len(rt.sessions), 'connection': 'connected',
                             'environment': target, 'target': {'type': 'local'}, 'cwd': cwd or str(repo),
                             'started_at': 1}
        return rt.sessions[name]
    rt.ensure = fake_ensure
    await fake_ensure('boss')
    app = server.create_app(rt)
    async with TestClient(TestServer(app), headers={'Authorization': 'Bearer ' + app['token']}) as client:
        yield client, rt, started, repo


@pytest.mark.asyncio
async def test_spawn_records_parent_and_starts_agent(gateway):
    client, rt, started, repo = gateway
    response = await client.post('/api/sessions/boss/spawn', json={
        'prompt': 'go', 'model': 'sonnet', 'worktree': 'feat/a', 'origin_cwd': str(repo), 'pane': '4'})
    body = await response.json()
    assert response.status == 200, body
    assert body['name'] == 'boss-1' and body['parent'] == 'boss:4'
    assert body['url'].endswith('/view/boss-1') and body['cwd'] == str(repo.parent / 'proj-feat-a')
    assert store.read_meta('boss-1')['parent'] == 'boss:4'
    (name, cwd, spec, prompt), = started
    assert cwd == body['cwd'] and spec['model'] == 'sonnet' and prompt.startswith('go\n\n')
    listing = await (await client.get('/api/sessions')).json()
    assert {s['name']: s['parent'] for s in listing}['boss-1'] == 'boss:4'


@pytest.mark.asyncio
async def test_spawn_refuses_beyond_limit(gateway, monkeypatch):
    client, rt, started, _ = gateway
    monkeypatch.setenv('DEVBOX_SPAWN_LIMIT', '2')
    for _ in range(2):
        assert (await client.post('/api/sessions/boss/spawn', json={})).status == 200
    response = await client.post('/api/sessions/boss/spawn', json={})
    assert response.status == 400 and 'DEVBOX_SPAWN_LIMIT' in (await response.json())['error']
    assert len(started) == 2


@pytest.mark.asyncio
async def test_top_level_spawn_has_no_parent_or_limit(gateway, monkeypatch):
    client, rt, started, _ = gateway
    monkeypatch.setenv('DEVBOX_SPAWN_LIMIT', '1')
    for _ in range(2):
        body = await (await client.post('/api/spawn', json={})).json()
        assert body['parent'] == ''
    assert store.read_meta(body['name'])['parent'] == ''


@pytest.mark.asyncio
async def test_failed_start_discards_session_and_worktree(gateway, monkeypatch):
    client, rt, started, repo = gateway

    def boom(*args):
        raise RuntimeError('zellij failed')
    monkeypatch.setattr(spawn, 'start_agent', boom)
    async def fake_kill(name):
        rt.sessions.pop(name)
    rt.kill = fake_kill
    response = await client.post('/api/sessions/boss/spawn', json={'worktree': 'feat/z', 'origin_cwd': str(repo)})
    assert response.status == 503
    assert 'boss-1' not in rt.sessions and not (repo.parent / 'proj-feat-z').exists()


@pytest.mark.asyncio
async def test_worktree_rejected_across_environments(gateway):
    client, *_ = gateway
    response = await client.post('/api/sessions/boss/spawn', json={'worktree': 'b', 'environment': 'other'})
    assert response.status == 400


def test_remote_rpc_spawn_and_worktree(monkeypatch, repo):
    import remote
    calls = []
    monkeypatch.setattr(spawn, 'start_agent', lambda *a: calls.append(a))
    assert remote.rpc({'operation': 'spawn', 'session': 's', 'cwd': '/', 'spec': {}, 'prompt': 'p'}) == {'ok': True}
    assert calls == [('s', '/', {}, 'p')]
    assert Path(remote.rpc({'operation': 'worktree', 'cwd': str(repo), 'branch': 'r'})['path']).is_dir()


def test_bootstrap_ships_spawn_module():
    import base64, io, tarfile, bootstrap
    names = tarfile.open(fileobj=io.BytesIO(base64.b64decode(bootstrap.archive())), mode='r:gz').getnames()
    assert 'spawn.py' in names and 'plugins/devbox/skills/devbox-spawn/SKILL.md' in names


def test_color_env_defaults_and_respects_operator():
    assert spawn.color_env({}) == {'TERM': 'xterm-256color', 'COLORTERM': 'truecolor'}
    assert spawn.color_env({'TERM': '', 'COLORTERM': ''})['TERM'] == 'xterm-256color'
    assert spawn.color_env({'TERM': 'screen', 'COLORTERM': '24bit'}) == {'TERM': 'screen', 'COLORTERM': '24bit'}


def test_start_agent_launches_zellij_with_color_env(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.delenv('TERM', raising=False)
    monkeypatch.delenv('COLORTERM', raising=False)
    monkeypatch.setattr(spawn.store, 'session_dir', lambda name: str(tmp_path / name))
    monkeypatch.setattr(spawn, '_create_detached', lambda name, cwd, layout_file, env: seen.update(env))
    spawn.start_agent('s', str(tmp_path), {'agent': 'claude', 'model': 'opus', 'permission_mode': 'auto'}, 'p')
    assert seen['TERM'] == 'xterm-256color' and seen['COLORTERM'] == 'truecolor'


UUID = '0b7f3c52-8a1e-4d2b-9c61-5e4f7a8b9c10'


@pytest.mark.parametrize('agent,levels,flag', [
    ('claude', ('low', 'medium', 'high', 'xhigh', 'max'), '--effort %s'),
    ('codex', ('minimal', 'low', 'medium', 'high', 'xhigh'), '-c model_reasoning_effort=%s')])
def test_effort_builds_flag_per_agent(agent, levels, flag):
    for level in levels:
        assert spawn.plan({'agent': agent, 'effort': level})['effort'] == level
        script = spawn.agent_script(agent, '', '', '/p.md', level)
        assert script.startswith('%s %s -- ' % (agent, flag % level))


def test_effort_unset_changes_nothing():
    assert spawn.plan({})['effort'] == ''
    assert spawn.agent_script('claude', 'sonnet', 'plan', '/p.md') == spawn.agent_script(
        'claude', 'sonnet', 'plan', '/p.md', '')
    assert '--effort' not in spawn.agent_script('claude', '', '', '/p.md', '')
    assert 'reasoning' not in spawn.agent_script('codex', '', '', '/p.md', '')


@pytest.mark.parametrize('agent,level', [('claude', 'minimal'), ('codex', 'max'), ('claude', 'high; rm -rf /'),
                                         ('codex', 'low $(x)')])
def test_invalid_effort_rejected(agent, level):
    with pytest.raises(ValueError, match='effort must be one of'):
        spawn.plan({'agent': agent, 'effort': level})
    assert spawn.agent_script(agent, '', '', '/p.md', level).startswith(agent + ' -- ')


def test_effort_carried_into_resume(monkeypatch, tmp_path):
    monkeypatch.setenv('DEVBOX_STATE_DIR', str(tmp_path))
    store.write_spawn('s', {'agent': 'claude', 'effort': 'high'})
    assert json.loads((tmp_path / 'sessions/s/spawn.json').read_text())['effort'] == 'high'
    event = {'hook_event_name': 'Stop', 'session_id': UUID, 'cwd': '/w', 'permission_mode': 'default'}
    store.record_agent_event('s', '1', 'claude', event, 1)
    resume = store.read_resume('s')
    assert resume['effort'] == 'high'
    assert '--effort high' in spawn.resume_script(resume)
    codex = spawn.resume_script({'agent': 'codex', 'session_id': UUID, 'effort': 'low'})
    assert codex == 'codex resume -c model_reasoning_effort=low %s || echo %s; exec "${SHELL:-sh}"' % (
        UUID, spawn._sh_quote(spawn.RESUME_HINT % 'codex resume'))
    assert '--effort' not in spawn.resume_script({**resume, 'effort': 'bogus'})
    assert '--effort' not in spawn.resume_script({**resume, 'effort': ''})
