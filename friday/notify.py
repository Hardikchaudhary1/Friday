"""Windows toast notifications through PowerShell's built-in WinRT access (no extra packages)."""
from __future__ import annotations

import subprocess
from xml.sax.saxutils import escape

# Toasts need a registered app id; PowerShell's own id is present on every Windows install.
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml([Console]::In.ReadToEnd())
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('%s').Show($toast)
""" % APP_ID


def toast(title: str, body: str) -> bool:
    xml = (
        '<toast scenario="reminder"><visual><binding template="ToastGeneric">'
        f"<text>{escape(title)}</text><text>{escape(body)}</text>"
        '</binding></visual><actions><action content="Dismiss" arguments="dismiss" activationType="system"/></actions>'
        '<audio src="ms-winsoundevent:Notification.Reminder"/></toast>'
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
            input=xml, text=True, encoding="utf-8", capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
