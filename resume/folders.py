"""Native folder selection for a locally running browser workspace."""
from __future__ import annotations

import base64
import platform
import shutil
import subprocess
from pathlib import Path


def choose_folder(initial):
    initial = Path(initial).expanduser().resolve()
    while not initial.is_dir() and initial != initial.parent:
        initial = initial.parent
    system = platform.system()
    if system == 'Darwin':
        script = '''on run argv
            set selectedFolder to choose folder with prompt "Choose your JhalCV resume folder" default location (POSIX file (item 1 of argv))
            return POSIX path of selectedFolder
        end run'''
        command = ['osascript', '-e', script, str(initial)]
    elif system == 'Windows':
        # EncodedCommand keeps shell parsing entirely separate from any paths.
        script = '''Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Choose your JhalCV resume folder'
try {
  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::WriteLine($dialog.SelectedPath)
  }
} finally { $dialog.Dispose() }
'''
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        command = ['powershell.exe', '-NoProfile', '-STA', '-EncodedCommand', encoded]
    elif shutil.which('zenity'):
        command = ['zenity', '--file-selection', '--directory', '--title=Choose your JhalCV resume folder', '--filename=' + str(initial) + '/']
    elif shutil.which('kdialog'):
        command = ['kdialog', '--getexistingdirectory', str(initial), '--title', 'Choose your JhalCV resume folder']
    else:
        raise ValueError('No native folder picker is available. Enter the destination path manually.')
    try:
        options = {'capture_output': True, 'text': True, 'encoding': 'utf-8', 'timeout': 300}
        if system == 'Windows':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(command, **options)
    except subprocess.TimeoutExpired as e:
        raise ValueError('Folder selection timed out. Try again or enter the path manually.') from e
    except OSError as e:
        raise ValueError('Could not open the folder picker. Enter the destination path manually.') from e
    if result.returncode != 0:
        if (system == 'Darwin' and '(-128)' in result.stderr) or (system != 'Darwin' and result.returncode == 1):
            return None
        raise ValueError('Could not open the folder picker. Enter the destination path manually.')
    # Remove the dialog tool's line terminator, not valid path whitespace.
    selected = result.stdout.removesuffix('\n')
    if not selected:
        return None
    folder = Path(selected).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError('The selected folder is unavailable. Choose an existing folder.')
    return str(folder)
