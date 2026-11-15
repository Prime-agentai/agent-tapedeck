from __future__ import annotations

import sys

import pytest

from tapedeck.cassette import Cassette
from tapedeck.synth import synth_interaction


def test_fixture_replays_marked_cassette_and_reports_calls(pytester: pytest.Pytester) -> None:
    cassettes = pytester.path / "cassettes"
    cassettes.mkdir()
    Cassette([synth_interaction("claude", "PLANNED", writes={"plan.md": "# plan"})]).save(cassettes / "planner.json")
    pytester.makepyfile(
        test_agent=f"""
        import subprocess, json, pytest

        @pytest.mark.tapedeck("planner.json")
        def test_planner(tapedeck, tmp_path, monkeypatch):
            monkeypatch.chdir(tmp_path)
            out = subprocess.run(["claude", "-p", "plan"], capture_output=True, text=True).stdout
            assert json.loads(out.splitlines()[-1])["result"] == "PLANNED"
            assert (tmp_path / "plan.md").read_text() == "# plan"
            assert tapedeck.mode == "replay"
            assert [c["action"] for c in tapedeck.calls()] == ["replay"]

        @pytest.mark.tapedeck("planner.json", fault="exit:7")
        def test_fault_marker(tapedeck):
            assert subprocess.run(["claude", "-p", "plan"], capture_output=True).returncode == 7

        def test_missing_cassette_in_replay_mode_fails_loudly(tapedeck):
            r = subprocess.run(["claude", "-p", "plan"], capture_output=True, text=True)
            assert r.returncode == 97 and "no recorded calls" in r.stderr
            assert tapedeck.cassette.name == "test_missing_cassette_in_replay_mode_fails_loudly.json"
        """
    )
    result = pytester.runpytest_subprocess("-p", "tapedeck.pytest_plugin", "--tapedeck-mode=replay",
                                           "-p", "no:cacheprovider")
    result.assert_outcomes(passed=3)


def test_unknown_marker_option_is_a_usage_error(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.tapedeck("x.json", colour="red")
        def test_x(tapedeck):
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "tapedeck.pytest_plugin", "--tapedeck-mode=replay")
    result.stdout.fnmatch_lines(["*unknown tapedeck marker option(s): colour*"])
    assert result.ret != 0


def test_record_session_does_not_wipe_a_shared_cassette(pytester: pytest.Pytester, fake_bin) -> None:
    pytester.makepyfile(
        """
        import subprocess, pytest

        @pytest.mark.tapedeck("shared.json", bins=["claude"])
        def test_one(tapedeck, tmp_path):
            subprocess.run(["claude", "-p", "one"], cwd=tmp_path, check=True, capture_output=True)

        @pytest.mark.tapedeck("shared.json", bins=["claude"])
        def test_two(tapedeck, tmp_path):
            subprocess.run(["claude", "-p", "two"], cwd=tmp_path, check=True, capture_output=True)
        """
    )
    pytester.runpytest_subprocess("-p", "tapedeck.pytest_plugin", "--tapedeck-mode=record").assert_outcomes(passed=2)
    recorded = Cassette.load(pytester.path / "cassettes" / "shared.json").interactions
    assert [r.argv[1] for r in recorded] == ["one", "two"]
    (fake_bin / "claude").unlink()
    pytester.runpytest_subprocess("-p", "tapedeck.pytest_plugin", "--tapedeck-mode=replay").assert_outcomes(passed=2)


def test_python_version_supported() -> None:
    assert sys.version_info >= (3, 10)
