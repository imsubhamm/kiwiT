import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('runtime_dependencies', Path('scripts/runtime_dependencies.py'))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
RELEASE = 'a' * 40


@pytest.fixture
def graph(tmp_path, monkeypatch):
    (tmp_path / '.python-version').write_text('3.14\n')
    (tmp_path / 'requirements.lock').write_text('example-pkg==1.2.3\n')
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="kiwit"\nversion="0.1.0"\n')
    monkeypatch.setattr(runtime.platform, 'python_version_tuple', lambda: ('3', '14', '0'))
    monkeypatch.setattr(runtime.importlib.metadata, 'distributions', list)
    versions = {'kiwit': '0.1.0', 'example-pkg': '1.2.3'}
    monkeypatch.setattr(runtime.importlib.metadata, 'version', versions.__getitem__)
    calls = []

    def pip(args, **kwargs):
        calls.append((args, kwargs))
        if 'install' in args:
            Path(args[args.index('--report') + 1]).write_text(json.dumps({'install': [
                {'metadata': {'name': key, 'version': value}} for key, value in versions.items()]}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime.subprocess, 'run', pip)
    return tmp_path, versions, calls


def test_install_records_exact_graph_and_scanner_input(graph):
    root, versions, calls = graph
    output = root / 'evidence'
    runtime.install(root, RELEASE, output)
    manifest = json.loads((output / 'runtime-manifest.json').read_text())
    assert manifest['packages'] == versions
    assert manifest['release'] == RELEASE
    assert manifest['lock_sha256'] == runtime.digest(root / 'requirements.lock')
    assert (output / 'runtime-requirements.txt').read_text() == 'example-pkg==1.2.3\n'
    command, options = calls[0]
    assert command[command.index('--constraint') + 1] == str(root / 'requirements.lock')
    assert command[command.index('--timeout') + 1] == '20'
    assert command[command.index('--retries') + 1] == '2'
    assert options == {'check': True, 'timeout': 600}
    assert calls[-1][0][-1] == 'check'


@pytest.mark.parametrize('failure', [subprocess.TimeoutExpired('pip', 600), subprocess.CalledProcessError(1, 'pip')])
def test_network_or_dependency_failure_never_produces_acceptance(graph, monkeypatch, failure):
    root, _, _ = graph

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(runtime.subprocess, 'run', fail)
    with pytest.raises(type(failure)):
        runtime.install(root, RELEASE, root / 'evidence')
    assert not (root / 'evidence/runtime-manifest.json').exists()


def test_unlocked_transitive_dependency_is_rejected(graph):
    root, versions, _ = graph
    versions['unexpected'] = '9.0'
    with pytest.raises(ValueError, match='not locked'):
        runtime.install(root, RELEASE, root / 'evidence')
    assert not (root / 'evidence/runtime-manifest.json').exists()


def test_deployment_requires_tested_identity_before_acceptance(graph):
    root, _, _ = graph
    runtime.install(root, RELEASE, root / 'ci')
    expected = root / 'ci/runtime-manifest.json'
    runtime.install(root, RELEASE, root / 'deploy', expected)
    manifest = json.loads(expected.read_text())
    manifest['packages']['example-pkg'] = '1.2.4'
    expected.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='identity mismatch'):
        runtime.install(root, RELEASE, root / 'bad-deploy', expected)
    assert not (root / 'bad-deploy/runtime-manifest.json').exists()


def test_tooling_cannot_change_tested_runtime(graph):
    root, versions, _ = graph
    runtime.install(root, RELEASE, root / 'ci')
    manifest = json.loads((root / 'ci/runtime-manifest.json').read_text())
    versions['example-pkg'] = '2.0'
    with pytest.raises(ValueError, match='package mismatch'):
        runtime.verify(manifest, root, RELEASE)


@pytest.mark.parametrize('field,value', [('release', 'b'*40), ('python', '3.12'), ('machine', 'different'),
                                        ('lock_sha256', 'changed'), ('project_sha256', 'changed')])
def test_identity_rejects_other_release_interpreter_platform_or_inputs(graph, field, value):
    root, _, _ = graph
    runtime.install(root, RELEASE, root / 'ci')
    manifest = json.loads((root / 'ci/runtime-manifest.json').read_text())
    changed = {**manifest, field: value}
    with pytest.raises(ValueError, match=field):
        runtime.compare(manifest, changed)


def test_wrong_python_fails_before_network(graph, monkeypatch):
    root, _, calls = graph
    monkeypatch.setattr(runtime.platform, 'python_version_tuple', lambda: ('3', '12', '7'))
    with pytest.raises(ValueError, match='requires Python'):
        runtime.install(root, RELEASE, root / 'ci')
    assert not calls


def test_dirty_environment_fails_before_network(graph, monkeypatch):
    root, _, calls = graph
    monkeypatch.setattr(runtime.importlib.metadata, 'distributions',
                        lambda: [SimpleNamespace(metadata={'Name': 'untracked'})])
    with pytest.raises(ValueError, match='fresh virtual environment'):
        runtime.install(root, RELEASE, root / 'ci')
    assert not calls


def test_dependency_preparation_failure_cannot_drain_or_activate(tmp_path):
    script = Path('deploy/remote_deploy.sh').read_text()
    start = script.index('runuser -u kiwit -- "$release_dir/.venv/bin/python" "$release_dir/scripts/runtime_dependencies.py"')
    end = script.index("printf '%s\\n' \"$release_sha\"", start)
    install = script[start:end]
    harness = 'set -e\nrelease_dir=/test\nrelease_sha=' + RELEASE + '\nrunuser() { return 1; }\n'
    result = subprocess.run(['bash', '-c', harness + install + '\necho ACTIVATED'], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert 'ACTIVATED' not in result.stdout
    assert start < script.index('gap_started=$(date +%s)')
