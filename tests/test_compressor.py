"""Compression backend selection: QSV, VAAPI and software fallback."""

from __future__ import annotations

from app.services import compressor


def _fake_run(cmd, timeout):
    output = cmd[-1]
    with open(output, "wb") as handle:
        handle.write(b"x" * 4096)
    return True, ""


def _source(tmp_path):
    path = tmp_path / "in.mp4"
    path.write_bytes(b"a" * 2048)
    return str(path)


def _settings(backend: str = "auto") -> dict[str, str]:
    return {
        "compression.enabled": "true",
        "compression.backend": backend,
        "compression.qsv_preset": "faster",
        "compression.qsv_quality": "25",
        "compression.cpu_preset": "ultrafast",
        "compression.cpu_crf": "30",
    }


def test_auto_prefers_qsv(tmp_path, monkeypatch):
    monkeypatch.setattr(compressor, "probe_hardware", lambda force=False: ("qsv", "ready"))
    monkeypatch.setattr(compressor, "_run", _fake_run)

    result = compressor.compress_file(_source(tmp_path), _settings("auto"))

    assert result.backend == "qsv"
    assert result.compressed is True


def test_auto_uses_vaapi_when_qsv_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(compressor, "probe_hardware", lambda force=False: ("vaapi", "ready"))
    monkeypatch.setattr(compressor, "_run", _fake_run)

    result = compressor.compress_file(_source(tmp_path), _settings("auto"))

    assert result.backend == "vaapi"
    assert result.compressed is True


def test_software_when_no_hardware(tmp_path, monkeypatch):
    monkeypatch.setattr(compressor, "probe_hardware", lambda force=False: (None, "no gpu"))
    monkeypatch.setattr(compressor, "_run", _fake_run)

    result = compressor.compress_file(_source(tmp_path), _settings("auto"))

    assert result.backend == "software"
    assert result.compressed is True


def test_qsv_only_does_not_silently_use_vaapi(tmp_path, monkeypatch):
    monkeypatch.setattr(compressor, "probe_hardware", lambda force=False: ("vaapi", "ready"))
    monkeypatch.setattr(compressor, "_run", _fake_run)

    result = compressor.compress_file(_source(tmp_path), _settings("qsv"))

    assert result.backend == "software"
