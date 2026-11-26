"""Error reporting never stops the server (ADR-076)."""

import sys
import types

import pytest

from yt_mcp import logging as ytlog


def _fake_sdk(init):
    sdk = types.ModuleType("sentry_sdk")
    sdk.init = init
    sdk.tags = {}
    sdk.set_tag = lambda k, v: sdk.tags.__setitem__(k, v)
    return sdk


def test_no_dsn_does_nothing(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    ytlog.setup_sentry()  # must not import or raise


def test_sdk_rejecting_an_option_retries_without_it(monkeypatch):
    calls = []

    def init(**kw):
        calls.append(kw)
        if "send_default_pii" in kw:
            raise TypeError("Unknown option 'send_default_pii'")

    sdk = _fake_sdk(init)
    monkeypatch.setitem(sys.modules, "sentry_sdk", sdk)
    monkeypatch.setenv("SENTRY_DSN", "https://key@example.invalid/1")
    ytlog.setup_sentry()
    assert len(calls) == 2
    assert "send_default_pii" not in calls[1]
    assert calls[1]["before_send"] is ytlog._scrub_event
    assert "instance_id" in sdk.tags


def test_any_init_failure_leaves_the_server_running(monkeypatch, capsys):
    def init(**kw):
        raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "sentry_sdk", _fake_sdk(init))
    monkeypatch.setenv("SENTRY_DSN", "https://key@example.invalid/1")
    ytlog.setup_sentry()
    assert "Sentry disabled" in capsys.readouterr().err


@pytest.mark.parametrize("spec", ["sentry-sdk>=2.0,<3"])
def test_sentry_sdk_is_capped_below_3(spec):
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert spec in text
