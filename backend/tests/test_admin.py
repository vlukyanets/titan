import pytest

from titan import admin


def test_add_user_prints_a_code(
    migrated: str, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin.main(["add-user", name]) == 0
    first_line = capsys.readouterr().out.splitlines()[0]
    assert first_line.startswith(f"One-time sign-in code for {name}: ")
    assert len(first_line.rsplit(" ", 1)[1]) >= 22


def test_existing_name_is_refused(
    migrated: str, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    admin.main(["add-user", name])
    capsys.readouterr()
    assert admin.main(["add-user", name]) == 1
    output = capsys.readouterr()
    assert "already exists" in output.err
    assert output.out == ""


@pytest.mark.parametrize("empty", ["", "  "])
def test_empty_name_is_refused(
    migrated: str, empty: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin.main(["add-user", empty]) == 1
    output = capsys.readouterr()
    assert "empty" in output.err
    assert output.out == ""
