"""Key names, virtual key codes and modifier flags for the US ANSI layout.

Pure data and parsing so it can be unit-tested without Quartz.
"""

from __future__ import annotations

from dataclasses import dataclass

# CGEventFlags bits.
FLAG_SHIFT = 1 << 17
FLAG_CONTROL = 1 << 18
FLAG_OPTION = 1 << 19
FLAG_COMMAND = 1 << 20
FLAG_FN = 1 << 23

MODIFIERS: dict[str, int] = {
    "cmd": FLAG_COMMAND,
    "command": FLAG_COMMAND,
    "meta": FLAG_COMMAND,
    "super": FLAG_COMMAND,
    "shift": FLAG_SHIFT,
    "ctrl": FLAG_CONTROL,
    "control": FLAG_CONTROL,
    "alt": FLAG_OPTION,
    "opt": FLAG_OPTION,
    "option": FLAG_OPTION,
    "fn": FLAG_FN,
}

KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26,
    "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35,
    "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45,
    "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "backspace": 51, "delete": 51,
    "escape": 53, "esc": 53, "forwarddelete": 117, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126,
    "help": 114, "capslock": 57,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113,
    "f16": 106, "f17": 64, "f18": 79, "f19": 80, "f20": 90,
}

# Characters reachable only with shift on a US layout, mapped to the unshifted key.
SHIFTED: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";",
    '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}


@dataclass(frozen=True)
class KeyChord:
    keycode: int
    flags: int
    name: str


class KeyError_(ValueError):
    """Raised for a key name that is not on the table."""


def parse_chord(spec: str) -> KeyChord:
    """Parse "cmd+shift+t", "enter", "ctrl+c", "?" into a keycode plus modifier flags.

    Parts are separated by "+". The last part is the key; everything before it must be
    a modifier. Letters are case-insensitive ("Cmd+C" == "cmd+c"); add "shift+" explicitly.
    A single "+" as the key is written as "shift+=" or just "+".
    """
    raw = spec.strip()
    if not raw:
        raise KeyError_("empty key spec")
    if raw == "+":
        parts = ["+"]
    else:
        parts = [p for p in raw.split("+") if p != ""]
        if raw.endswith("+") and not raw.endswith("++"):
            raise KeyError_(f"dangling '+' in {spec!r}")
        if raw.endswith("++"):
            parts.append("+")
    flags = 0
    for mod in parts[:-1]:
        key = mod.lower()
        if key not in MODIFIERS:
            raise KeyError_(f"unknown modifier {mod!r} in {spec!r}")
        flags |= MODIFIERS[key]
    key = parts[-1]
    lowered = key.lower()
    if lowered in SHIFTED:
        flags |= FLAG_SHIFT
        lowered = SHIFTED[lowered]
    if lowered not in KEYCODES:
        raise KeyError_(f"unknown key {key!r} in {spec!r}")
    return KeyChord(keycode=KEYCODES[lowered], flags=flags, name=raw)
