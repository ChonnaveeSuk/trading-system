# trading-system/scripts/tests/test_error_report.py
#
# Lightweight tests for scripts/error_report.py — the wrapper used by
# run_daily.sh to forward step failures to Cloud Error Reporting.

from __future__ import annotations

import importlib.util
import io
import os
import sys
import textwrap
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_MODULE_PATH = _SCRIPTS_DIR / "error_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("error_report", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["error_report"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
# _read_traceback_file behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestReadTraceback:
    def test_missing_path_returns_empty(self):
        mod = _load_module()
        assert mod._read_traceback_file(None) == ""
        assert mod._read_traceback_file("/nonexistent/path/abc") == ""

    def test_short_file_read_in_full(self, tmp_path):
        mod = _load_module()
        f = tmp_path / "tb.log"
        f.write_text("Traceback (most recent call last):\nValueError: nope\n")
        out = mod._read_traceback_file(str(f))
        assert "ValueError: nope" in out

    def test_long_file_truncated_to_8kb_tail(self, tmp_path):
        mod = _load_module()
        f = tmp_path / "tb.log"
        # 20 KB of recognisable lines
        body = "\n".join(f"line-{i:05d}" for i in range(2_000))
        f.write_text(body + "\n--- last line ---\n")
        out = mod._read_traceback_file(str(f))
        assert len(out) <= 8192
        # tail must contain the last marker
        assert "--- last line ---" in out


# ─────────────────────────────────────────────────────────────────────────────
# _report graceful degradation
# ─────────────────────────────────────────────────────────────────────────────

class TestReportGracefulDegradation:
    def test_missing_dependency_returns_false(self, monkeypatch):
        """If google-cloud-error-reporting isn't installed, _report must return
        False rather than raising — the caller has already failed."""
        mod = _load_module()
        # Pretend the import fails by temporarily clearing the cached module.
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "google.cloud" or name.startswith("google.cloud.error_reporting"):
                raise ImportError("simulated missing dep")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        ok = mod._report("Step X", "exit_code=1", "", "test-project")
        assert ok is False

    def test_client_raise_returns_false(self, monkeypatch):
        """If error_reporting.Client init raises, _report must swallow and
        return False — never propagate a tooling error to the daily run."""
        mod = _load_module()

        class FakeERModule:
            class Client:  # noqa: D401
                def __init__(self, *a, **kw):
                    raise RuntimeError("simulated GCP auth failure")

        # Inject a fake `google.cloud.error_reporting` so the import inside
        # _report succeeds but Client() raises.
        google_pkg = type(sys)("google")
        cloud_pkg  = type(sys)("google.cloud")
        cloud_pkg.error_reporting = FakeERModule
        google_pkg.cloud = cloud_pkg

        monkeypatch.setitem(sys.modules, "google", google_pkg)
        monkeypatch.setitem(sys.modules, "google.cloud", cloud_pkg)
        monkeypatch.setitem(sys.modules, "google.cloud.error_reporting", FakeERModule)

        ok = mod._report("Step X", "exit_code=1", "", "test-project")
        assert ok is False
