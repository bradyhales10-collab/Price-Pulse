from __future__ import annotations

import os
import subprocess


def protect_password(password: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Part Pulse desktop credentials can only be protected on Windows.")
    script = (
        "$plain=[Console]::In.ReadToEnd();"
        "$secure=ConvertTo-SecureString $plain -AsPlainText -Force;"
        "ConvertFrom-SecureString $secure"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        input=password,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def unprotect_password(protected_password: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Part Pulse desktop credentials can only be opened on Windows.")
    script = (
        "$encrypted=[Console]::In.ReadToEnd();"
        "$secure=ConvertTo-SecureString $encrypted;"
        "$pointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure);"
        "try {[Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)}"
        "finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        input=protected_password,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.rstrip("\r\n")
