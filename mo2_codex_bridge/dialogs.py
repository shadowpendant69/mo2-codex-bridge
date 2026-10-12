"""Controls scoped exclusively to the native installer opened by a bridge job."""
import json
from PyQt6 import sip

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QDialog,
                             QLabel, QLineEdit)

from .common import digest


def installer_widget(owner):
    active = getattr(owner, 'active_job', None)
    if not owner.busy or not active or active['action'] not in {'install_archive', 'update_archive'}:
        return None
    dialog = QApplication.activeModalWidget()
    # A tool may not interact with unrelated MO2 settings, authentication or file pickers.
    if not isinstance(dialog, QDialog):
        return None
    identity = (dialog.objectName() + ' ' + dialog.windowTitle()).lower()
    if not any(term in identity for term in ('simpleinstalldialog', 'fomodinstallerdialog', 'fomoddialog', 'manualinstalldialog', 'mod installation')):
        return None
    return dialog


def state(owner):
    dialog = installer_widget(owner)
    if dialog is None:
        return {'installer_open': False, 'message': 'No supported installer dialog is active'}
    controls = []
    previous_controls = getattr(owner, 'installer_controls', {})
    owner.installer_dialog = dialog
    owner.installer_controls = {}
    widgets = dialog.findChildren(QAbstractButton) + dialog.findChildren(QComboBox) + dialog.findChildren(QLineEdit)
    for widget in widgets:
        if not widget.isVisible():
            continue
        item = {'id': str(sip.unwrapinstance(widget)), 'name': widget.objectName(), 'enabled': widget.isEnabled()}
        if isinstance(widget, QAbstractButton):
            item.update(kind='button', text=widget.text(), checkable=widget.isCheckable(), checked=widget.isChecked())
        elif isinstance(widget, QComboBox):
            item.update(kind='choice', value=widget.currentText(), index=widget.currentIndex(),
                        choices=[widget.itemText(i) for i in range(widget.count())])
        elif isinstance(widget, QLineEdit):
            # Installer names are visible; password or hidden fields never appear here.
            if widget.echoMode() != QLineEdit.EchoMode.Normal:
                continue
            item.update(kind='text', value=widget.text(), editable=not widget.isReadOnly())
        controls.append(item)
        owner.installer_controls[item['id']] = widget
    result = {'installer_open': True, 'dialog_id': str(sip.unwrapinstance(dialog)), 'title': dialog.windowTitle(),
              'labels': [w.text() for w in dialog.findChildren(QLabel) if w.isVisible()],
              'controls': controls}
    result['dialog_revision'] = digest(json.dumps(result, sort_keys=True).encode())
    return result


def choose(owner, params):
    current = state(owner)
    if (not current['installer_open'] or params.get('dialog_id') != current['dialog_id']
            or params.get('dialog_revision') != current['dialog_revision']):
        raise ValueError('Installer changed. Read installer_state again before selecting an option.')
    if owner.organizer.profileName() != owner.active_job['params'].get('expected_profile'):
        raise ValueError('Profile changed while the installer was open')
    if owner.organizer.pluginSetting(owner.name(), 'allow_changes') is False:
        raise ValueError('Changes are disabled in bridge settings')
    dialog = installer_widget(owner)
    candidates = dialog.findChildren(QAbstractButton) + dialog.findChildren(QComboBox) + dialog.findChildren(QLineEdit)
    widget = next((x for x in candidates if str(sip.unwrapinstance(x)) == params['control_id']), None)
    if widget is None or not widget.isEnabled() or not widget.isVisible():
        raise ValueError('Installer control is unavailable')
    if isinstance(widget, QAbstractButton):
        if widget.isCheckable():
            if type(params.get('checked')) is not bool:
                raise ValueError('A boolean checked value is required')
            widget.setChecked(params['checked'])
        else:
            QTimer.singleShot(0, widget.click)
    elif isinstance(widget, QComboBox):
        if dialog.objectName() == 'SimpleInstallDialog':
            raise ValueError('The archive target name is fixed by the install request')
        index = params['index']
        if type(index) is not int or not 0 <= index < widget.count():
            raise ValueError('Invalid choice index')
        widget.setCurrentIndex(index)
    elif isinstance(widget, QLineEdit):
        # Naming targets or arbitrary file paths are not altered through dialog controls.
        raise ValueError('Text fields are inspect-only; change archive names in the install request')
    return {'selection_applied': True, 'control_id': params['control_id']}


def accept_quick_install(owner):
    dialog = installer_widget(owner)
    if dialog is None or dialog.objectName() != 'SimpleInstallDialog':
        return
    combo = dialog.findChild(QComboBox, 'nameCombo')
    button = dialog.findChild(QAbstractButton, 'okBtn')
    expected = owner.active_job['params'].get('name')
    if (combo is not None and button is not None and combo.currentText() == expected
            and button.isEnabled() and owner.organizer.profileName() == owner.active_job['params'].get('expected_profile')
            and owner.organizer.pluginSetting(owner.name(), 'allow_changes') is not False):
        button.click()
