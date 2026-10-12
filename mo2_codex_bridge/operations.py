"""All organizer access in this module runs on MO2's Qt GUI thread."""
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .common import VERSION, atomic_write, contained_path, digest, safe_name, write_json

READ_ACTIONS = {'status', 'snapshot', 'downloads', 'file_conflict', 'mod_conflicts',
                'read_config', 'diagnostics', 'backups', 'logs', 'api_capabilities', 'profiles'}
WRITE_ACTIONS = {'create_separator', 'set_mod_priority', 'set_mod_active', 'set_plugin_active',
                 'set_plugin_order', 'install_archive', 'update_archive', 'write_config',
                 'restore_config', 'hide_file', 'unhide_file', 'restore_order', 'remove_empty_separator',
                 'remove_mod', 'select_profile'}


class Operations:
    def __init__(self, organizer, mobase, data_path, refresh, select_profile=None):
        self.o, self.m = organizer, mobase
        self.data = Path(data_path)
        self.refresh = refresh
        self.profile_selector = select_profile
        self.running = set()

    def is_active(self, name):
        return bool(self.o.modList().state(name) & self.m.ModState.ACTIVE)

    def mod(self, name, writable=False):
        mod = self.o.modList().getMod(name)
        if mod is None:
            raise ValueError('Unknown mod: ' + str(name))
        if writable and (mod.isForeign() or mod.isOverwrite() or mod.isBackup()):
            raise ValueError('This operation requires a regular MO2-managed mod')
        if writable:
            root = Path(mod.absolutePath()).resolve()
            if not root.is_relative_to(Path(self.o.modsPath()).resolve()):
                raise ValueError('Mod folder is outside configured Mods directory')
        return mod

    def snapshot(self):
        mods = []
        ml = self.o.modList()
        for name in ml.allModsByProfilePriority():
            mod = ml.getMod(name)
            if mod is None:
                continue
            mods.append({'name': name, 'display_name': ml.displayName(name),
                         'priority': ml.priority(name), 'active': self.is_active(name),
                         'separator': mod.isSeparator(), 'foreign': mod.isForeign(),
                         'overwrite': mod.isOverwrite(), 'path': mod.absolutePath(),
                         'version': mod.version().displayString(), 'nexus_id': mod.nexusId(),
                         'latest_known_version': mod.newestVersion().displayString()})
        pl = self.o.pluginList()
        plugins = []
        for name in pl.pluginNames():
            plugins.append({'name': name, 'priority': pl.priority(name), 'load_order': pl.loadOrder(name),
                            'active': pl.state(name) == self.m.PluginState.ACTIVE,
                            'origin': pl.origin(name), 'masters': list(pl.masters(name)),
                            'light': pl.isLightFlagged(name) or pl.hasLightExtension(name),
                            'master': pl.isMasterFlagged(name) or pl.hasMasterExtension(name)})
        plugins.sort(key=lambda item: item['priority'])
        result = {'bridge_version': VERSION, 'mo2_version': self.o.appVersion().displayString(),
                  'profile': self.o.profileName(), 'profile_path': self.o.profilePath(),
                  'paths': {'mods': self.o.modsPath(), 'downloads': self.o.downloadsPath(),
                            'overwrite': self.o.overwritePath()}, 'mods': mods, 'plugins': plugins,
                  'running_applications': sorted(self.running)}
        result['revision'] = digest(json.dumps(result, sort_keys=True).encode())
        return result

    def dispatch(self, action, p):
        if action not in READ_ACTIONS | WRITE_ACTIONS:
            raise ValueError('Unknown action: ' + action)
        if not isinstance(p, dict):
            raise ValueError('Parameters must be a JSON object')
        backup = None
        if action in WRITE_ACTIONS:
            before = self.snapshot()
            if p.get('expected_profile') != before['profile'] or p.get('expected_revision') != before['revision']:
                raise ValueError('Profile or state changed. Read snapshot again before making changes.')
            if self.running:
                raise ValueError('Close the game or running MO2-launched tools before changing mods')
            backup = self.data / 'backups' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
            backup.mkdir(parents=True)
            write_json(backup / 'before.json', before)
        try:
            result = self.snapshot() if action == 'snapshot' else getattr(self, action)(p, backup)
            if backup is not None:
                write_json(backup / 'result.json', {'action': action, 'result': result})
                result['backup_id'] = backup.name
            return result
        except Exception as error:
            if backup is not None:
                write_json(backup / 'error.json', {'action': action, 'error': str(error)})
            raise

    def status(self, p, b):
        return {'bridge_version': VERSION, 'profile': self.o.profileName(), 'running_applications': sorted(self.running)}

    def api_capabilities(self, p, b):
        return {'read_actions': sorted(READ_ACTIONS), 'write_actions': sorted(WRITE_ACTIONS),
                'organizer_methods': [x for x in dir(self.o) if not x.startswith('_')]}

    def profiles(self, p, b):
        root = Path(self.o.profilePath()).parent
        return {'profiles': sorted(x.name for x in root.iterdir() if x.is_dir() and (x / 'modlist.txt').is_file()),
                'current': self.o.profileName()}

    def select_profile(self, p, b):
        if self.profile_selector is None or p['profile'] not in self.profiles({}, None)['profiles']:
            raise ValueError('Unknown profile or profile selection unavailable')
        self.profile_selector(p['profile'])
        self.refresh()
        if self.o.profileName() != p['profile']:
            raise RuntimeError('MO2 did not switch profiles')
        return {'profile': self.o.profileName()}

    def snapshot_action(self, p, b):
        return self.snapshot()

    def downloads(self, p, b):
        files = []
        for path in sorted(Path(self.o.downloadsPath()).iterdir()):
            if path.is_file() and path.suffix.lower() in {'.zip', '.7z', '.rar', '.omod', '.fomod'}:
                files.append({'name': path.name, 'path': str(path), 'bytes': path.stat().st_size})
        return {'archives': files}

    def file_conflict(self, p, b):
        relative = p['path'].replace('\\', '/')
        contained_path(self.o.overwritePath(), relative)
        return {'path': relative, 'origins_in_priority_order': list(self.o.getFileOrigins(relative)),
                'winning_physical_path': self.o.resolvePath(relative),
                'scope': 'MO2 virtual file origins, including archives when indexed by MO2; not ESP record conflicts'}

    def mod_conflicts(self, p, b):
        mod = self.mod(p['mod'])
        root = Path(mod.absolutePath())
        maximum = max(1, min(2000, int(p.get('max_files', 300))))
        cursor = max(0, int(p.get('offset', 0)))
        files = []
        visited = 0
        more = False
        for folder, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink()
                             and not (hasattr(Path, 'is_junction') and (Path(folder) / d).is_junction()))
            for name in sorted(names):
                path = Path(folder) / name
                if name.lower() == 'meta.ini' or path.is_symlink() or name.endswith('.mohidden'):
                    continue
                if visited < cursor:
                    visited += 1
                    continue
                if visited - cursor >= maximum:
                    more = True
                    break
                relative = path.relative_to(root).as_posix()
                report = self.file_conflict({'path': relative}, None)
                if len(report['origins_in_priority_order']) > 1:
                    files.append(report)
                visited += 1
            if more:
                break
        return {'mod': p['mod'], 'conflicts': files, 'scanned': visited - cursor,
                'next_offset': visited if more else None,
                'scope': 'Files physically present in this mod, inspected through MO2 VFS; archive contents not enumerated'}

    def config_path(self, p):
        scope = p.get('scope', 'mod')
        if scope == 'mod':
            mod = self.mod(p['mod'], writable=True)
            if mod.isSeparator():
                raise ValueError('Separators do not have editable mod configuration')
            return contained_path(mod.absolutePath(), p['path'], text_only=True)
        if scope == 'profile':
            filename = p['path']
            if filename.lower() not in {'skyrim.ini', 'skyrimprefs.ini', 'skyrimcustom.ini'}:
                raise ValueError('Only profile-local Skyrim game INIs can be edited')
            if not self.o.profile().localSettingsEnabled():
                raise ValueError('Enable profile-specific game settings first; global game INIs are not edited')
            return contained_path(self.o.profilePath(), filename, text_only=True)
        if scope == 'overwrite':
            return contained_path(self.o.overwritePath(), p['path'], text_only=True)
        raise ValueError('Unknown configuration scope')

    def read_config(self, p, b):
        path = self.config_path(p)
        if not path.exists():
            return {'path': str(path), 'exists': False, 'sha256': None, 'text': ''}
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('Configuration file exceeds the 1 MiB limit')
        content = path.read_bytes()
        encoding = 'utf-16' if content.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
        try:
            text = content.decode(encoding)
        except UnicodeError:
            encoding = 'cp1252'
            text = content.decode(encoding)
        return {'path': str(path), 'exists': True, 'sha256': digest(content), 'text': text, 'encoding': encoding}

    def write_config(self, p, b):
        path = self.config_path(p)
        old = path.read_bytes() if path.exists() else None
        if p.get('expected_sha256') != (digest(old) if old is not None else None):
            raise ValueError('Configuration file changed. Read it again before writing.')
        encoding = p.get('encoding', 'utf-8')
        if encoding not in {'utf-8', 'utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be', 'cp1252'}:
            raise ValueError('Unsupported text encoding')
        text = p['text']
        if not isinstance(text, str) or '\x00' in text:
            raise ValueError('Text must be a string without null characters')
        if path.suffix.lower() == '.json':
            json.loads(text)
        new = text.encode(encoding)
        if len(new) > 1024 * 1024:
            raise ValueError('Configuration file exceeds the 1 MiB limit')
        if old is not None:
            atomic_write(b / 'config-before.bin', old)
        write_json(b / 'config.json', {'scope': p.get('scope', 'mod'), 'mod': p.get('mod'),
                                     'path': p['path'], 'existed': old is not None,
                                     'before_sha256': digest(old) if old is not None else None,
                                     'after_sha256': digest(new)})
        atomic_write(path, new)
        if old is None:
            self.refresh()
        return {'path': str(path), 'sha256': digest(new), 'bytes': len(new)}

    def get_backup(self, name):
        name = safe_name(name)
        path = contained_path(self.data / 'backups', name)
        if not path.is_dir():
            raise ValueError('Unknown backup ID')
        return path

    def restore_config(self, p, b):
        source = self.get_backup(p['backup_id'])
        manifest = json.loads((source / 'config.json').read_text(encoding='utf-8'))
        path = self.config_path(manifest)
        current = path.read_bytes() if path.exists() else None
        if current is None or digest(current) != manifest['after_sha256']:
            raise ValueError('File changed since this backup; restore would overwrite later work')
        atomic_write(b / 'config-before.bin', current)
        if manifest['existed']:
            atomic_write(path, (source / 'config-before.bin').read_bytes())
        else:
            path.unlink()
        return {'restored': str(path), 'source_backup': source.name}

    def create_separator(self, p, b):
        name = safe_name(p['name'])
        if not name.endswith('_separator'):
            name += '_separator'
        if self.o.modList().getMod(name) is not None or (Path(self.o.modsPath()) / name).exists():
            raise ValueError('A mod or separator with this name already exists')
        created = self.o.createMod(self.m.GuessedString(name))
        if created is None:
            raise RuntimeError('MO2 did not create the separator')
        self.refresh()
        created = self.mod(name)
        if not created.isSeparator():
            raise RuntimeError('MO2 did not recognize the new folder as a separator')
        if 'priority' in p:
            self.move_mod(name, p['priority'])
        return {'name': name, 'priority': self.o.modList().priority(name), 'separator': True}

    def move_mod(self, name, priority):
        self.mod(name, writable=True)
        maximum = len(self.o.modList().allMods()) - 2  # overwrite is not movable
        if type(priority) is not int or not 0 <= priority <= maximum:
            raise ValueError('Priority must be an integer within the movable list')
        if not self.o.modList().setPriority(name, priority):
            if self.o.modList().priority(name) != priority:
                raise RuntimeError('MO2 rejected this mod priority')
        return self.o.modList().priority(name)

    def set_mod_priority(self, p, b):
        return {'name': p['mod'], 'priority': self.move_mod(p['mod'], p['priority'])}

    def set_mod_active(self, p, b):
        mod = self.mod(p['mod'], writable=True)
        if mod.isSeparator() or type(p['active']) is not bool:
            raise ValueError('Specify a regular mod and a boolean active value')
        self.o.modList().setActive(p['mod'], p['active'])
        self.refresh()
        if self.is_active(p['mod']) != p['active']:
            raise RuntimeError('MO2 did not apply the requested enabled state')
        return {'mod': p['mod'], 'active': self.is_active(p['mod'])}

    def set_plugin_active(self, p, b):
        pl = self.o.pluginList()
        name = p['plugin']
        if name not in pl.pluginNames() or type(p['active']) is not bool:
            raise ValueError('Specify an existing plugin and a boolean active value')
        if p['active']:
            inactive = [master for master in pl.masters(name) if pl.state(master) != self.m.PluginState.ACTIVE]
            if inactive:
                raise ValueError('Enable required masters first: ' + ', '.join(inactive))
        else:
            dependents = [n for n in pl.pluginNames() if pl.state(n) == self.m.PluginState.ACTIVE and name in pl.masters(n)]
            if dependents:
                raise ValueError('Disable dependent plugins first: ' + ', '.join(dependents))
        pl.setState(name, self.m.PluginState.ACTIVE if p['active'] else self.m.PluginState.INACTIVE)
        actual = pl.state(name) == self.m.PluginState.ACTIVE
        if actual != p['active']:
            raise RuntimeError('MO2 rejected the plugin enabled state (possibly an essential master)')
        return {'plugin': name, 'active': actual}

    def set_plugin_order(self, p, b):
        pl = self.o.pluginList()
        order = p['plugins']
        if not isinstance(order, list) or len(order) != len(set(order)) or set(order) != set(pl.pluginNames()):
            raise ValueError('Supply every currently visible plugin exactly once')
        positions = {name: i for i, name in enumerate(order)}
        for name in order:
            if any(master in positions and positions[master] > positions[name] for master in pl.masters(name)):
                raise ValueError('A master must load before its dependent: ' + name)
        pl.setLoadOrder(order)
        actual = sorted(pl.pluginNames(), key=pl.priority)
        return {'requested': order, 'actual': actual, 'exact_match': order == actual}

    def archive(self, p):
        path = Path(p['archive']).resolve()
        if not path.is_file() or path.suffix.lower() not in {'.zip', '.7z', '.rar', '.omod', '.fomod'}:
            raise ValueError('Specify an existing local mod archive')
        # An archive path can be outside Downloads, as with user-provided E: files.
        return path

    def install_archive(self, p, b):
        name = safe_name(p['name'])
        if self.o.modList().getMod(name) is not None or (Path(self.o.modsPath()) / name).exists():
            raise ValueError('Use a new name; this tool will not silently merge or replace existing mods')
        archive = self.archive(p)
        created = self.o.installMod(str(archive), name)
        if created is None:
            return {'installed': False, 'reason': 'Installer canceled or failed'}
        installed_name = created.name()
        self.refresh()
        if p.get('active') is True:
            self.o.modList().setActive(installed_name, True)
            self.refresh()
        if 'priority' in p:
            self.move_mod(installed_name, p['priority'])
        return {'installed': True, 'name': installed_name, 'active': self.is_active(installed_name),
                'priority': self.o.modList().priority(installed_name)}

    def update_archive(self, p, b):
        old = self.mod(p['mod'], writable=True)
        if old.isSeparator():
            raise ValueError('Separators cannot be updated from an archive')
        before_priority = self.o.modList().priority(p['mod'])
        before_active = self.is_active(p['mod'])
        options = dict(p, active=False)
        result = self.install_archive(options, b)
        if not result['installed']:
            return result
        if p.get('activate_update', False):
            self.o.modList().setActive(p['mod'], False)
            self.o.modList().setActive(result['name'], before_active)
            self.refresh()
            self.move_mod(result['name'], before_priority)
        result.update({'previous_mod': p['mod'], 'previous_mod_preserved': True,
                       'update_activated': bool(p.get('activate_update', False)),
                       'scope': 'New version installed separately; enabled-state changes affect only the current profile'})
        return result

    def hide_file(self, p, b):
        mod = self.mod(p['mod'], writable=True)
        path = contained_path(mod.absolutePath(), p['path'])
        if path.name.lower() == 'meta.ini' or path.suffix.lower() in {'.esp', '.esm', '.esl'}:
            raise ValueError('Use plugin tools for ESP/ESM/ESL files; mod metadata is protected')
        hidden = Path(str(path) + '.mohidden')
        if not path.is_file() or hidden.exists():
            raise ValueError('File missing or an existing hidden copy would be overwritten')
        path.rename(hidden)
        self.refresh()
        return {'hidden': p['path'], 'mod': p['mod'], 'scope': 'Shared mod file: affects every profile using this mod'}

    def unhide_file(self, p, b):
        mod = self.mod(p['mod'], writable=True)
        path = contained_path(mod.absolutePath(), p['path'])
        hidden = contained_path(mod.absolutePath(), p['path'] + '.mohidden')
        if path.exists() or not hidden.is_file():
            raise ValueError('Hidden file missing or visible file already exists')
        hidden.rename(path)
        self.refresh()
        return {'unhidden': p['path'], 'mod': p['mod']}

    def backups(self, p, b):
        root = self.data / 'backups'
        return {'backup_ids': sorted((x.name for x in root.iterdir() if x.is_dir()), reverse=True) if root.exists() else []}

    def restore_order(self, p, b):
        source = self.get_backup(p['backup_id'])
        before = json.loads((source / 'before.json').read_text(encoding='utf-8'))
        current = self.snapshot()
        if before['profile'] != current['profile']:
            raise ValueError('The backup belongs to another profile')
        managed = [x for x in before['mods'] if not x['foreign'] and not x['overwrite']]
        current_names = {x['name'] for x in current['mods']}
        if any(x['name'] not in current_names for x in managed):
            raise ValueError('Backup refers to removed mods; restore them first')
        for mod in sorted(managed, key=lambda x: x['priority']):
            self.move_mod(mod['name'], mod['priority'])
            if not mod['separator']:
                self.o.modList().setActive(mod['name'], mod['active'])
        extra = [x for x in current['mods'] if x['name'] not in {y['name'] for y in before['mods']} and not x['foreign'] and not x['overwrite'] and not x['separator']]
        for mod in extra:
            self.o.modList().setActive(mod['name'], False)
        self.refresh()
        pl = self.o.pluginList()
        order = [x['name'] for x in before['plugins'] if x['name'] in pl.pluginNames()]
        order += [n for n in pl.pluginNames() if n not in order]
        pl.setLoadOrder(order)
        for plugin in before['plugins']:
            if plugin['name'] in pl.pluginNames():
                pl.setState(plugin['name'], self.m.PluginState.ACTIVE if plugin['active'] else self.m.PluginState.INACTIVE)
        return {'restored_profile_order': before['profile'], 'extra_mods_disabled': [x['name'] for x in extra],
                'scope': 'Priorities and enabled states only; mod files and removed mods are not restored'}

    def remove_empty_separator(self, p, b):
        mod = self.mod(p['mod'], writable=True)
        if not mod.isSeparator():
            raise ValueError('Only empty separators can be removed through this tool')
        contents = list(Path(mod.absolutePath()).iterdir())
        if any(x.name.lower() != 'meta.ini' or not x.is_file() for x in contents):
            raise ValueError('Separator contains unexpected files; removal refused')
        removed = self.o.modList().removeMod(mod)
        self.refresh()
        return {'removed': bool(removed), 'name': p['mod']}

    def remove_mod(self, p, b):
        mod = self.mod(p['mod'], writable=True)
        if mod.isSeparator():
            raise ValueError('Use remove_empty_separator for a separator')
        if p.get('acknowledge_shared_folder') is not True:
            raise ValueError('Removing a mod affects every profile. Explicitly acknowledge_shared_folder=true.')
        root = Path(mod.absolutePath())
        total = 0
        for folder, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                entry = Path(folder) / name
                if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
                    raise ValueError('Mod contains links or junctions; removal refused')
            total += sum((Path(folder) / name).stat().st_size for name in files)
        if shutil.disk_usage(b).free < total + 64 * 1024 * 1024:
            raise ValueError('Not enough space to preserve the mod before removal')
        shutil.copytree(root, b / 'removed-mod')
        removed = self.o.modList().removeMod(mod)
        self.refresh()
        return {'removed': bool(removed), 'name': p['mod'], 'mod_files_backup': str(b / 'removed-mod'),
                'scope': 'Removed from the shared Mods folder; every profile is affected'}

    def diagnostics(self, p, b):
        snapshot = self.snapshot()
        enabled = {x['name'] for x in snapshot['plugins'] if x['active']}
        positions = {x['name']: x['priority'] for x in snapshot['plugins']}
        problems = []
        for plugin in snapshot['plugins']:
            if not plugin['active']:
                continue
            for master in plugin['masters']:
                if master not in enabled:
                    problems.append({'type': 'missing_or_disabled_master', 'plugin': plugin['name'], 'master': master})
                elif positions[master] > plugin['priority']:
                    problems.append({'type': 'master_loads_after_dependent', 'plugin': plugin['name'], 'master': master})
        full = sum(x['active'] and not x['light'] for x in snapshot['plugins'])
        light = sum(x['active'] and x['light'] for x in snapshot['plugins'])
        if full > 254:
            problems.append({'type': 'full_plugin_limit', 'count': full})
        if light > 4096:
            problems.append({'type': 'light_plugin_limit', 'count': light})
        return {'profile': snapshot['profile'], 'problems': problems, 'full_plugins': full, 'light_plugins': light,
                'limits': 'Checks plugin dependencies and budgets; cannot certify DLL, animation or ESP record compatibility'}

    def logs(self, p, b):
        # Logs are data, never executable instructions. Do not expose MO2 credentials.
        root = Path(self.data).parents[2] / 'logs'
        candidates = sorted(root.glob('mo_interface*.log'), key=lambda x: x.stat().st_mtime, reverse=True)
        if not candidates:
            return {'log': None, 'lines': []}
        path = candidates[0]
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 32768))
            text = stream.read().decode('utf-8', errors='replace')
        lines = text.splitlines()[-min(200, max(1, int(p.get('lines', 50)))):]
        lines = ['[private line omitted]' if any(k in line.lower() for k in ('apikey', 'api key', 'authorization:', 'access_token', 'password=')) else line for line in lines]
        return {'log': str(path), 'lines': lines}


# Keep the public snapshot method callable internally without a parameter signature.
Operations.snapshot_rpc = Operations.snapshot_action
