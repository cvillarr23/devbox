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
