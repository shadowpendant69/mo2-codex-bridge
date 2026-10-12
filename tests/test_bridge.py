import importlib.util
import json
import shutil
import sys
import tempfile
import types
import unittest
from enum import IntFlag, Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType('mo2_codex_bridge')
package.__path__ = [str(ROOT / 'mo2_codex_bridge')]
sys.modules['mo2_codex_bridge'] = package
from mo2_codex_bridge.common import contained_path, safe_name, digest
from mo2_codex_bridge.operations import Operations


class ModState(IntFlag):
    ACTIVE = 1


class PluginState(Enum):
    ACTIVE = 1
    INACTIVE = 0


class Version:
    def displayString(self):
        return '1.0'


class Mod:
    def __init__(self, root, name):
        self.root, self._name = root, name
        (root / name).mkdir(exist_ok=True)
    def name(self): return self._name
    def absolutePath(self): return str(self.root / self._name)
    def isForeign(self): return False
    def isOverwrite(self): return False
    def isBackup(self): return False
    def isSeparator(self): return self._name.endswith('_separator')
    def version(self): return Version()
    def newestVersion(self): return Version()
    def nexusId(self): return 0


class ModList:
    def __init__(self, root):
        self.root = root
        self.mods = {n: Mod(root, n) for n in ['A', 'B']}
        self.order = list(self.mods)
        self.active = {'A', 'B'}
    def allModsByProfilePriority(self): return self.order
    def allMods(self): return self.order + ['Overwrite']
    def getMod(self, name): return self.mods.get(name)
    def displayName(self, name): return name
    def state(self, name): return ModState.ACTIVE if name in self.active else ModState(0)
    def priority(self, name): return self.order.index(name)
    def setPriority(self, name, priority):
        self.order.remove(name)
        self.order.insert(priority, name)
        return True
    def setActive(self, name, value):
        self.active.add(name) if value else self.active.discard(name)
        return True
    def removeMod(self, mod):
        path = Path(mod.absolutePath())
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('Outside test fixture')
        shutil.rmtree(path)
        self.order.remove(mod.name())
        self.mods.pop(mod.name())
        self.active.discard(mod.name())
        return True


class Plugins:
    def __init__(self):
        self.order = ['Master.esm', 'Child.esp']
        self.active = set(self.order)
    def pluginNames(self): return self.order
    def priority(self, name): return self.order.index(name)
    def loadOrder(self, name): return self.order.index(name) if name in self.active else -1
    def state(self, name): return PluginState.ACTIVE if name in self.active else PluginState.INACTIVE
    def origin(self, name): return 'A'
    def masters(self, name): return ['Master.esm'] if name == 'Child.esp' else []
    def isLightFlagged(self, name): return False
    def hasLightExtension(self, name): return False
    def isMasterFlagged(self, name): return name.endswith('.esm')
    def hasMasterExtension(self, name): return name.endswith('.esm')
    def setLoadOrder(self, order): self.order = list(order)
    def setState(self, name, state):
        self.active.add(name) if state == PluginState.ACTIVE else self.active.discard(name)


class Organizer:
    def __init__(self, root):
        self.root = root
        for n in ['mods', 'downloads', 'profile', 'overwrite']:
            (root / n).mkdir()
        self.ml, self.pl = ModList(root / 'mods'), Plugins()
    def modList(self): return self.ml
    def pluginList(self): return self.pl
    def modsPath(self): return str(self.root / 'mods')
    def downloadsPath(self): return str(self.root / 'downloads')
    def overwritePath(self): return str(self.root / 'overwrite')
    def profilePath(self): return str(self.root / 'profile')
    def profileName(self): return 'Test'
    def appVersion(self): return Version()
    def profile(self): return self
    def localSettingsEnabled(self): return True
    def getFileOrigins(self, path): return ['A', 'B']
    def resolvePath(self, path): return str(self.root / 'mods' / 'B' / path)
    def createMod(self, name):
        mod = Mod(self.root / 'mods', name)
        self.ml.mods[name] = mod
        self.ml.order.append(name)
        return mod


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.organizer = Organizer(self.root)
        self.ops = Operations(self.organizer, types.SimpleNamespace(ModState=ModState, PluginState=PluginState, GuessedString=str), self.root / 'data', lambda: None)
    def tearDown(self):
        self.temporary.cleanup()
    def change(self, action, **params):
        snapshot = self.ops.snapshot()
        params.update(expected_profile=snapshot['profile'], expected_revision=snapshot['revision'])
        return self.ops.dispatch(action, params)
    def test_snapshot_reads_current_api_state(self):
        self.assertEqual(self.ops.dispatch('snapshot', {})['profile'], 'Test')
        self.organizer.ml.active.discard('A')
        self.assertFalse(self.ops.snapshot()['mods'][0]['active'])
    def test_traversal_absolute_ads_and_protected_files(self):
        for path in ['../outside.ini', '/outside.ini', 'C:/outside.ini', 'file.ini:payload', 'a/../file.ini', 'meta.ini', 'sub/modlist.txt', 'script.py', 'a\\..\\bad.ini']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                contained_path(self.root, path, text_only=True)
    def test_invalid_windows_names(self):
        for name in ['..', 'NUL', 'CON.ini', 'COM1', 'trailing.', 'a/b', 'a\\b', ' name']:
            with self.subTest(name=name), self.assertRaises(ValueError): safe_name(name)
    def test_stale_snapshot_prevents_mutation(self):
        snapshot = self.ops.snapshot()
        self.organizer.ml.active.discard('A')
        with self.assertRaisesRegex(ValueError, 'state changed'):
            self.ops.dispatch('set_mod_active', {'mod': 'B', 'active': False, 'expected_profile': 'Test', 'expected_revision': snapshot['revision']})
        self.assertIn('B', self.organizer.ml.active)
    def test_running_game_prevents_mutation(self):
        self.ops.running.add('SkyrimSE.exe')
        with self.assertRaisesRegex(ValueError, 'Close the game'): self.change('set_mod_active', mod='A', active=False)
    def test_configuration_backup_restore_and_later_changes(self):
        path = self.root / 'mods/A/options.ini'
        path.write_bytes(b'[Settings]\r\nValue=1\r\n')
        result = self.change('write_config', mod='A', path='options.ini', expected_sha256=digest(path.read_bytes()), text='[Settings]\r\nValue=2\r\n')
        self.assertIn('Value=2', path.read_text())
        self.change('restore_config', backup_id=result['backup_id'])
        self.assertEqual(path.read_bytes(), b'[Settings]\r\nValue=1\r\n')
        with self.assertRaisesRegex(ValueError, 'File changed'): self.change('restore_config', backup_id=result['backup_id'])
    def test_configuration_hash_prevents_overwriting_later_work(self):
        path = self.root / 'mods/A/options.ini'
        path.write_text('new work')
        with self.assertRaisesRegex(ValueError, 'file changed'):
            self.change('write_config', mod='A', path='options.ini', expected_sha256='oldhash', text='replacement')
        self.assertEqual(path.read_text(), 'new work')
    def test_invalid_json_prevents_write(self):
        with self.assertRaises(json.JSONDecodeError):
            self.change('write_config', mod='A', path='options.json', expected_sha256=None, text='{')
        self.assertFalse((self.root / 'mods/A/options.json').exists())
    def test_separator_creation_uses_mo2_api(self):
        result = self.change('create_separator', name='01 Core', priority=0)
        self.assertTrue(result['separator'])
        self.assertEqual(self.organizer.ml.order[0], '01 Core_separator')
        with self.assertRaisesRegex(ValueError, 'already exists'): self.change('create_separator', name='01 Core')
    def test_plugin_dependencies_and_order(self):
        with self.assertRaisesRegex(ValueError, 'dependent'): self.change('set_plugin_active', plugin='Master.esm', active=False)
        with self.assertRaisesRegex(ValueError, 'master must load'): self.change('set_plugin_order', plugins=['Child.esp', 'Master.esm'])
        with self.assertRaisesRegex(ValueError, 'exactly once'): self.change('set_plugin_order', plugins=['Master.esm'])
    def test_hide_and_unhide_preserve_data(self):
        path = self.root / 'mods/A/example.txt'
        path.write_bytes(b'original')
        self.change('hide_file', mod='A', path='example.txt')
        self.assertFalse(path.exists())
        self.change('unhide_file', mod='A', path='example.txt')
        self.assertEqual(path.read_bytes(), b'original')
    def test_restore_order_preserves_file_contents(self):
        result = self.change('set_mod_priority', mod='A', priority=1)
        self.assertEqual(self.organizer.ml.order, ['B', 'A'])
        self.change('restore_order', backup_id=result['backup_id'])
        self.assertEqual(self.organizer.ml.order, ['A', 'B'])
    def test_diagnostics_identifies_disabled_master(self):
        self.organizer.pl.active.remove('Master.esm')
        report = self.ops.dispatch('diagnostics', {})
        self.assertEqual(report['problems'][0]['type'], 'missing_or_disabled_master')
    def test_conflict_scope_uses_vfs(self):
        report = self.ops.dispatch('file_conflict', {'path': 'textures/example.dds'})
        self.assertEqual(report['origins_in_priority_order'], ['A', 'B'])
        self.assertIn('/B/', report['winning_physical_path'].replace('\\', '/'))
    def test_removal_preserves_complete_mod_folder(self):
        path = self.root / 'mods/A/options.ini'
        path.write_bytes(b'irreplaceable user configuration')
        with self.assertRaisesRegex(ValueError, 'every profile'):
            self.change('remove_mod', mod='A')
        self.assertTrue(path.exists())
        result = self.change('remove_mod', mod='A', acknowledge_shared_folder=True)
        self.assertFalse(path.exists())
        self.assertEqual((Path(result['mod_files_backup']) / 'options.ini').read_bytes(), b'irreplaceable user configuration')
    def test_separator_removal_refreshes_live_model(self):
        created = self.change('create_separator', name='Empty')
        calls = []
        self.ops.refresh = lambda: calls.append(True)
        self.change('remove_empty_separator', mod=created['name'])
        self.assertTrue(calls)
        self.assertNotIn(created['name'], [x['name'] for x in self.ops.snapshot()['mods']])


if __name__ == '__main__':
    unittest.main(verbosity=2)
