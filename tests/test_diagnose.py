import sys

import pytest

import diagnose


@pytest.mark.parametrize("connected, expected", [(False, 1), (True, 0)])
def test_diagnosis_exit_code_matches_connection(monkeypatch, connected, expected):
    monkeypatch.setattr(sys, "argv", ["diagnose.py"])
    monkeypatch.setattr(diagnose, "probe", lambda: {"connected":connected})
    assert diagnose.main() == expected


def test_report_options_are_validated_before_connecting(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["diagnose.py", "--save-report"])
    monkeypatch.setattr(diagnose, "probe", lambda: pytest.fail("should not connect"))
    with pytest.raises(SystemExit) as error:
        diagnose.main()
    assert error.value.code == 2
