"""Current-user Windows startup integration."""
import subprocess
import winreg
from monitor import APP, pythonw

RUN_KEY=r'Software\Microsoft\Windows\CurrentVersion\Run'


def auto_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,RUN_KEY) as reg:
            winreg.QueryValueEx(reg,'DiskWatch')
            return True
    except OSError:
        return False


def set_autostart(enabled):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,RUN_KEY) as reg:
        if enabled:
            command=subprocess.list2cmdline([pythonw(),str(APP/'monitor.py'),'--start'])
            winreg.SetValueEx(reg,'DiskWatch',0,winreg.REG_SZ,command)
        else:
            try:
                winreg.DeleteValue(reg,'DiskWatch')
            except FileNotFoundError:
                pass
