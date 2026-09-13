from macpilot.screen import Display


def _display() -> Display:
    return Display(
        index=2, display_id=1, x=1920, y=-100, width=1512, height=982, scale=2.0, main=False
    )


def test_to_global_adds_origin() -> None:
    assert _display().to_global(10, 20) == (1930, -80)


def test_contains_is_half_open() -> None:
    d = _display()
    assert d.contains(0, 0)
    assert d.contains(1511, 981)
    assert not d.contains(1512, 0)
    assert not d.contains(-1, 5)
