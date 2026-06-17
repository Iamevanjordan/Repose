"""Unit tests for repose.utils.cli_base — Track 0 POL criteria 9-10."""

import json
import sys
from io import StringIO
from unittest import mock

import pytest

from repose.utils.cli_base import CLIBase


class TestCLI(CLIBase):
    """Concrete CLI for testing CLIBase functionality."""
    agent_name = "intel_feed"
    nouns = ["sources", "agents", "observations"]

    def handle(self, noun: str, verb: str, args: list[str]):
        if verb == "list":
            return [
                {"id": "source_3", "name": "arxiv_cs_ai", "timestamp": "2026-01-03T00:00:00Z"},
                {"id": "source_1", "name": "hackernews", "timestamp": "2026-01-01T00:00:00Z"},
                {"id": "source_2", "name": "reddit_ml", "timestamp": "2026-01-02T00:00:00Z"},
            ]
        elif verb == "history":
            return [
                {"id": "h3", "action": "disabled", "timestamp": "2026-05-03T00:00:00Z"},
                {"id": "h1", "action": "created", "timestamp": "2026-05-01T00:00:00Z"},
                {"id": "h2", "action": "enabled", "timestamp": "2026-05-02T00:00:00Z"},
            ]
        elif verb == "test":
            return {"status": "ok", "agent": self.agent_name}
        elif verb == "enable":
            return {"noun": noun, "verb": "enabled", "id": args[0] if args else "all"}
        elif verb == "disable":
            return {"noun": noun, "verb": "disabled", "id": args[0] if args else "all"}
        else:
            return {"noun": noun, "verb": verb}


class ErrorCLI(CLIBase):
    agent_name = "event_monitor"
    nouns = ["observations"]
    def handle(self, noun: str, verb: str, args: list[str]):
        raise RuntimeError("Simulated failure")


# Test 1: --format json produces valid JSON output
def test_format_json_produces_valid_json():
    cli = TestCLI(args=["sources", "list", "--format", "json"])
    cli.use_json = True
    output = cli.format_output(cli.handle("sources", "list", []))
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    assert len(parsed) == 3


def test_format_json_sorted_deterministically():
    cli = TestCLI(args=["sources", "list", "--format", "json"])
    cli.use_json = True
    output = cli.format_output(cli.handle("sources", "list", []))
    parsed = json.loads(output)
    timestamps = [item["timestamp"] for item in parsed]
    assert timestamps == sorted(timestamps)
    ids = [item["id"] for item in parsed]
    assert ids == ["source_1", "source_2", "source_3"]


def test_format_json_no_ansi():
    cli = TestCLI(args=["sources", "list", "--format", "json"])
    cli.use_json = True
    output = cli.format_output(cli.handle("sources", "list", []))
    assert "\x1b[" not in output


# Test 2: Exit codes are correct (0/1/2)
def test_exit_code_zero_on_success():
    cli = TestCLI(args=["sources", "list"])
    with mock.patch("sys.stdout", new_callable=StringIO):
        code = cli.run()
        assert code == 0


def test_exit_code_one_on_error():
    cli = ErrorCLI(args=["observations", "list"])
    with mock.patch("sys.stdout", new_callable=StringIO):
        with mock.patch("sys.stderr", new_callable=StringIO):
            code = cli.run()
            assert code == 1


def test_exit_code_two_on_cancelled():
    cli = TestCLI(args=["sources", "disable", "arxiv_cs_ai"])
    with mock.patch("builtins.input", return_value="n"):
        with mock.patch("sys.stdout", new_callable=StringIO):
            code = cli.run()
            assert code == 2


# Test 3: Output is deterministically ordered
def test_list_output_deterministic():
    cli1 = TestCLI(args=["sources", "list"])
    cli2 = TestCLI(args=["sources", "list"])
    out1 = cli1.format_output(cli1.handle("sources", "list", []))
    out2 = cli2.format_output(cli2.handle("sources", "list", []))
    assert out1 == out2
    cli1.use_json = True
    cli2.use_json = True
    json1 = cli1.format_output(cli1.handle("sources", "list", []))
    json2 = cli2.format_output(cli2.handle("sources", "list", []))
    assert json1 == json2


# Test 4: Restart prompt on enable/disable; not on list/history/test
def test_restart_prompt_on_enable():
    with mock.patch("builtins.input", side_effect=["y", ""]):
        with mock.patch("sys.stdout", new_callable=StringIO):
            with mock.patch("subprocess.run"):
                cli = TestCLI(args=["sources", "enable", "arxiv_cs_ai", "--yes"])
                code = cli.run()
                assert code == 0


def test_no_restart_prompt_on_list():
    with mock.patch("builtins.input") as mock_input:
        with mock.patch("sys.stdout", new_callable=StringIO):
            cli = TestCLI(args=["sources", "list"])
            code = cli.run()
            assert code == 0
            mock_input.assert_not_called()


def test_no_restart_prompt_on_test():
    with mock.patch("builtins.input") as mock_input:
        with mock.patch("sys.stdout", new_callable=StringIO):
            cli = TestCLI(args=["observations", "test"])
            code = cli.run()
            assert code == 0
            mock_input.assert_not_called()


def test_no_restart_prompt_on_history():
    with mock.patch("builtins.input") as mock_input:
        with mock.patch("sys.stdout", new_callable=StringIO):
            cli = TestCLI(args=["sources", "history"])
            code = cli.run()
            assert code == 0
            mock_input.assert_not_called()


def test_format_json_dict_output():
    cli = TestCLI(args=["observations", "test", "--format", "json"])
    cli.use_json = True
    output = cli.format_output(cli.handle("observations", "test", []))
    parsed = json.loads(output)
    assert parsed["status"] == "ok"
    assert parsed["agent"] == "intel_feed"
