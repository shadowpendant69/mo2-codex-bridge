"""Configure a local MO2 Codex Bridge installation on this computer."""
import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def validate_mo2(path):
    mo2 = Path(path).expanduser().resolve()
    if not (mo2 / 'ModOrganizer.exe').is_file() or not (mo2 / 'plugins/plugin_python').is_dir():
        raise ValueError('Select an MO2 installation with ModOrganizer.exe and Python plugin support')
    return mo2


def install(mo2, source):
    destination = mo2 / 'plugins/mo2_codex_bridge'
    if not destination.resolve().is_relative_to(mo2):
        raise ValueError('Plugin destination resolves outside the selected MO2 folder')
    if destination.is_symlink() or (hasattr(destination, 'is_junction') and destination.is_junction()):
        raise ValueError('Plugin destination is a link; installation refused')
    if destination.exists():
        backup_root = mo2 / 'plugins/data/mo2_codex_bridge/setup-backups'
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / (time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns()))
        shutil.copytree(destination, backup)
    destination.mkdir(parents=True, exist_ok=True)
    for path in (source / 'mo2_codex_bridge').glob('*.py'):
        shutil.copy2(path, destination / path.name)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mo2', required=True, help='MO2 installation folder')
    parser.add_argument('--register-codex', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Validate and print paths without modifying anything')
    args = parser.parse_args()
    mo2 = validate_mo2(args.mo2)
    source = Path(__file__).resolve().parent
    config_path = Path.home() / '.mo2-codex-bridge/config.json'
    server = source / 'mcp_server.py'
    if args.dry_run:
        print(json.dumps({'mo2': str(mo2), 'configuration': str(config_path), 'server': str(server)}))
        return 0
    if os.name != 'nt':
        raise ValueError('The MO2 component requires Windows')
    process_list = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ModOrganizer.exe', '/FO', 'CSV', '/NH'],
                                   capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if any(row and row[0].casefold() == 'modorganizer.exe' for row in csv.reader(io.StringIO(process_list.stdout))):
        raise ValueError('Close MO2 before installing or upgrading its bridge')
    destination = install(mo2, source)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({'mo2_path': str(mo2)}, indent=2), encoding='utf-8')
    mcp = {'mcpServers': {'mo2_bridge': {'command': sys.executable,
                                      'args': [str(server), '--mo2', str(mo2)]}}}
    (source / '.mcp.json').write_text(json.dumps(mcp, indent=2), encoding='utf-8')
    plugin_dir = source / '.codex-plugin'
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / 'plugin.json').write_text(json.dumps({'name': 'mo2-codex-bridge', 'version': '0.1.0',
        'description': 'Manage a running local Mod Organizer 2 through its plugin interface.',
        'author': {'name': 'Project contributors'}, 'mcpServers': './.mcp.json',
        'interface': {'displayName': 'MO2 Codex Bridge', 'shortDescription': 'Manage the running MO2',
                      'category': 'Productivity', 'capabilities': ['Read', 'Write', 'Interactive']}}, indent=2), encoding='utf-8')
    if args.register_codex:
        codex = shutil.which('codex')
        if codex is None:
            raise ValueError('MO2 bridge installed, but Codex CLI is not on PATH. Use the generated .mcp.json for registration.')
        subprocess.run([codex, 'mcp', 'add', 'mo2_bridge', '--', sys.executable, str(server), '--mo2', str(mo2)], check=True)
    print(json.dumps({'installed_plugin': str(destination), 'mcp_configuration': str(source / '.mcp.json'),
                      'next_step': 'Open MO2 and begin a new Codex chat if needed'}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('Setup failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
