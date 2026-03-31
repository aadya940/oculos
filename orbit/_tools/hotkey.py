from typing import Dict, Any

try:
    import pyautogui

    _PYAUTOGUI_IMPORT_ERROR = None
except Exception as e:  # pragma: no cover - environment-dependent (e.g. headless CI)
    pyautogui = None
    _PYAUTOGUI_IMPORT_ERROR = e


def press_hotkey(keys: str) -> Dict[str, Any]:
    """
    Presses a keyboard shortcut or key combination.
    Use this for system-level shortcuts that don't have a UI element.

    Args:
        keys (str): Key combination e.g. 'ctrl+c', 'alt+tab',
                    'ctrl+shift+t', 'win+d', 'alt+f4'
    """
    try:
        if pyautogui is None:
            raise RuntimeError(
                "pyautogui is unavailable in this environment "
                f"(import error: {_PYAUTOGUI_IMPORT_ERROR!r})"
            )
        parts = keys.lower().split("+")
        pyautogui.hotkey(*parts)
        return {"status": "success", "message": f"Pressed {keys}."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to press hotkey: {str(e)}"}
