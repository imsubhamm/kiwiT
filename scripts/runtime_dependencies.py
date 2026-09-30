"""Install, record and verify the release runtime, independently of CI tooling."""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess  # nosec B404
import sys
from pathlib import Path

EXTRAS = 'api,production,workflow,research,ml,llm'
NETWORK_TIMEOUT = 20
NETWORK_RETRIES = 2
INSTALL_TIMEOUT = 600


def name(value):
    return re.sub(r'[-_.]+', '-', value).lower()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(root, release):
    expected_python = (root / '.python-version').read_text().strip()
    actual_python = '.'.join(platform.python_version_tuple()[:2])
    if actual_python != expected_python:
        raise ValueError(f'Release requires Python {expected_python}; found {actual_python}')
    return {'format': 'kiwit-runtime-v1', 'release': release,
            'python': actual_python, 'system': platform.system(), 'machine': platform.machine(),
            'lock_sha256': digest(root / 'requirements.lock'),
            'project_sha256': digest(root / 'pyproject.toml')}


def compare(actual, expected):
    if actual != expected:
        differences = sorted(key for key in set(actual) | set(expected) if actual.get(key) != expected.get(key))
        raise ValueError('Runtime identity mismatch: ' + ', '.join(differences))


def verify(manifest, root, release):
    compare({k: v for k, v in manifest.items() if k != 'packages'}, identity(root, release))
    for package, version in manifest['packages'].items():
        installed = importlib.metadata.version(package)
        if installed != version:
            raise ValueError(f'Runtime package mismatch: {package}: {installed} != {version}')
    subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True, timeout=60)  # nosec B603


def install(root, release, output, expected=None):
    # Fresh venvs are required: pip's report must describe the entire runtime graph.
    existing = {name(d.metadata['Name']) for d in importlib.metadata.distributions()}
    if existing - {'pip', 'setuptools', 'wheel'}:
        raise ValueError('Runtime install requires a fresh virtual environment')
    manifest = identity(root, release)
    output.mkdir(parents=True, exist_ok=True)
    report = output / 'pip-report.json'
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',  # nosec B603
                    '--timeout', str(NETWORK_TIMEOUT), '--retries', str(NETWORK_RETRIES),
                    '--constraint', str(root / 'requirements.lock'), '--report', str(report),
                    f'{root}[{EXTRAS}]'], check=True, timeout=INSTALL_TIMEOUT)
    resolved = json.loads(report.read_text())['install']
    manifest['packages'] = {name(item['metadata']['name']): item['metadata']['version'] for item in resolved}
    if 'kiwit' not in manifest['packages'] or len(manifest['packages']) < 2:
        raise ValueError('Incomplete runtime install report')
    pins = set(re.findall(r'^([A-Za-z0-9_.-]+)==([^ ;\n]+)', (root / 'requirements.lock').read_text(), re.MULTILINE))
    pins = {(name(package), version) for package, version in pins}
    for package, version in manifest['packages'].items():
        if package != 'kiwit' and (package, version) not in pins:
            raise ValueError(f'Runtime dependency is not locked: {package}=={version}')
    verify(manifest, root, release)
    if expected is not None:
        compare(manifest, json.loads(expected.read_text()))
    # Publish acceptance evidence only after successful verification.
    (output / 'runtime-manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    (output / 'runtime-requirements.txt').write_text(''.join(
        f'{package}=={version}\n' for package, version in sorted(manifest['packages'].items()) if package != 'kiwit'))
    print('Verified runtime identity: ' + digest(output / 'runtime-manifest.json'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['install', 'verify', 'compare'])
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--release', required=True)
    parser.add_argument('--output', type=Path, default=Path('runtime-evidence'))
    parser.add_argument('--expected', type=Path)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.release):
        parser.error('--release must be a full Git commit SHA')
    manifest_path = args.output / 'runtime-manifest.json'
    if args.mode == 'install':
        install(args.root.resolve(), args.release, args.output.resolve(), args.expected)
    elif args.mode == 'verify':
        verify(json.loads(manifest_path.read_text()), args.root, args.release)
    else:
        if args.expected is None:
            parser.error('compare requires --expected')
        actual = json.loads(manifest_path.read_text())
        if actual['release'] != args.release:
            raise ValueError('Evidence belongs to another release')
        compare(actual, json.loads(args.expected.read_text()))


if __name__ == '__main__':
    main()
