import subprocess
from pathlib import Path


def test_rollback_stops_workers_before_switch_and_removes_new_units(tmp_path):
    script = Path('deploy/remote_deploy.sh').read_text()
    rollback = script[script.index('rollback() {'):script.index('trap rollback ERR')]
    previous = tmp_path / 'previous'
    previous.mkdir()
    backup = tmp_path / 'backup'
    backup.mkdir()
    for name, value in {'kiwit-banknifty.enabled': 'enabled', 'kiwit-banknifty.active': 'active',
                        'kiwit-banknifty-reports.enabled': 'not-found',
                        'kiwit-banknifty-reports.active': 'inactive',
                        'kiwit-banknifty.service': '', 'kiwit-banknifty.timer': ''}.items():
        (backup / name).write_text(value)
    harness = '''
systemctl() { echo "systemctl $*"; }
ln() { echo "ln $*"; }
install() { echo "install $*"; }
rm() { echo "rm $*"; }
activated=true
drained=true
units=(kiwit-banknifty kiwit-banknifty-reports)
previous_release=$1
backup_dir=$2
'''
    result = subprocess.run(['bash', '-c', harness + rollback + '\n(exit 7)\nrollback',
                             'test', str(previous), str(backup)], capture_output=True, text=True, check=False)
    assert result.returncode == 7
    commands = result.stdout
    assert commands.index('stop kiwit-banknifty-reports.service') < commands.index('ln -sfn')
    assert 'disable --now kiwit-banknifty-reports.timer' in commands
    assert 'rm -f /etc/systemd/system/kiwit-banknifty-reports.timer' in commands
    assert 'enable kiwit-banknifty.timer' in commands
    assert 'start kiwit-banknifty.timer' in commands
    assert 'start kiwit-banknifty-reports.timer' not in commands


def test_migration_follows_drain_and_timers_start_after_readiness():
    script = Path('deploy/remote_deploy.sh').read_text()
    main = script[script.index('trap rollback ERR'):]
    assert main.index('drained=true') < main.index('manage_database.py') < main.index('ln -sfn')
    assert main.index('http://127.0.0.1:8001/ready') < main.index('systemctl enable --now')
