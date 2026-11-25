import diagnostics


def test_confinement_reads_apparmor_label_and_seccomp_mode(monkeypatch):
    files = {'/proc/self/attr/current': 'devbox-workspace (enforce)\n',
             '/proc/self/status': 'Name:\tpython3\nNoNewPrivs:\t1\nSeccomp:\t2\nSeccomp_filters:\t1\n'}
    monkeypatch.setattr(diagnostics, '_read', files.get)
    assert diagnostics.confinement() == {'apparmor': 'devbox-workspace (enforce)', 'seccomp': 2}


def test_confinement_tolerates_missing_proc_files(monkeypatch):
    monkeypatch.setattr(diagnostics, '_read', lambda path: None)
    assert diagnostics.confinement() == {'apparmor': None, 'seccomp': None}


def test_unconfined_label_has_trailing_nul_stripped(monkeypatch):
    files = {'/proc/self/attr/current': 'unconfined\x00', '/proc/self/status': 'Seccomp:\t0\n'}
    monkeypatch.setattr(diagnostics, '_read', files.get)
    assert diagnostics.confinement() == {'apparmor': 'unconfined', 'seccomp': 0}


def test_report_includes_platform_and_confinement(monkeypatch):
    monkeypatch.setattr(diagnostics.shutil, 'which', lambda name: None)
    report = diagnostics.report()
    assert report['platform']['machine']
    assert set(report['confinement']) == {'apparmor', 'seccomp'}


def test_report_lists_gh_with_hint_when_missing(monkeypatch):
    monkeypatch.setattr(diagnostics.shutil, 'which', lambda name: None)
    gh = diagnostics.report()['tools']['gh']
    assert gh['available'] is False
    assert 'GH_VERSION' in gh['hint']


def no_managed(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostics, 'MANAGED_SETTINGS', {a: tmp_path / 'none' / a for a in ('claude', 'codex')})


def test_agent_settings_states(tmp_path, monkeypatch):
    no_managed(monkeypatch, tmp_path)
    (tmp_path / '.claude').mkdir()
    (tmp_path / '.claude/settings.json').write_text('{"remoteControlAtStartup": true, "x": "devbox-status"}')
    settings = diagnostics.agent_settings(tmp_path)
    assert settings['claude']['state'] == 'ok' and settings['claude']['hooks'] is True
    assert settings['claude']['remote_control'] is True
    assert settings['codex']['state'] == 'missing' and 'hint' in settings['codex']


def test_agent_settings_reports_symlink_without_hooks(tmp_path, monkeypatch):
    no_managed(monkeypatch, tmp_path)
    target = tmp_path / 'dots.json'
    target.write_text('{}')
    (tmp_path / '.claude').mkdir()
    (tmp_path / '.claude/settings.json').symlink_to(target)
    claude = diagnostics.agent_settings(tmp_path)['claude']
    assert claude['state'] == 'managed-link' and claude['hooks'] is False
    assert 'managed by' in claude['hint'] and claude['link_target'] == str(target.resolve())
