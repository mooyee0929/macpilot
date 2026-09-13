import pytest

from macpilot.keys import FLAG_COMMAND, FLAG_CONTROL, FLAG_SHIFT, KeyError_, parse_chord


def test_plain_key() -> None:
    chord = parse_chord("enter")
    assert chord.keycode == 36
    assert chord.flags == 0


def test_modifiers_are_combined() -> None:
    chord = parse_chord("cmd+shift+t")
    assert chord.keycode == 17
    assert chord.flags == FLAG_COMMAND | FLAG_SHIFT


def test_modifier_aliases() -> None:
    assert parse_chord("control+c").flags == FLAG_CONTROL
    assert parse_chord("ctrl+c").flags == FLAG_CONTROL
    assert parse_chord("Cmd+C").flags == FLAG_COMMAND


def test_letters_are_case_insensitive() -> None:
    assert parse_chord("A").flags == 0
    assert parse_chord("A").keycode == parse_chord("a").keycode


def test_shifted_symbol_maps_to_base_key() -> None:
    chord = parse_chord("?")
    assert chord.keycode == parse_chord("/").keycode
    assert chord.flags == FLAG_SHIFT


def test_plus_key_forms() -> None:
    assert parse_chord("+").keycode == parse_chord("=").keycode
    assert parse_chord("cmd++").keycode == parse_chord("=").keycode
    assert parse_chord("cmd++").flags == FLAG_COMMAND | FLAG_SHIFT


@pytest.mark.parametrize("bad", ["", "cmd+", "hyper+a", "cmd+nosuchkey"])
def test_invalid_specs_raise(bad: str) -> None:
    with pytest.raises(KeyError_):
        parse_chord(bad)
