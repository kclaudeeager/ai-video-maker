from videomaker.media import ffmpeg as ff

FILTERS_WITH_SUBTITLES = """Filters:
 T.. subtitles         V->V       Render text subtitles onto input video using the libass library.
 ... scale             V->V       Scale the input video size.
"""

FILTERS_WITHOUT_SUBTITLES = """Filters:
 ... scale             V->V       Scale the input video size.
"""

ENCODERS_WITH_QSV = """Encoders:
 V..... h264_qsv             H.264 (Intel Quick Sync Video acceleration)
 V..... h264_vaapi           H.264 (VAAPI)
"""


def test_probe_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(ff.shutil, "which", lambda name: None)
    caps = ff.probe_capabilities()
    assert caps.installed is False
    assert caps.has_subtitles_filter is False


def test_probe_detects_subtitles_and_hw_encoder(monkeypatch):
    monkeypatch.setattr(ff.shutil, "which", lambda name: "/usr/bin/" + name)
    outputs = {
        "-version": "ffmpeg version 6.1.1 Copyright\n",
        "-filters": FILTERS_WITH_SUBTITLES,
        "-encoders": ENCODERS_WITH_QSV,
    }
    monkeypatch.setattr(ff, "_run", lambda args: outputs[args[-1]])
    caps = ff.probe_capabilities()
    assert caps.installed is True
    assert caps.version.startswith("ffmpeg version 6.1.1")
    assert caps.has_subtitles_filter is True
    assert caps.hw_encoder == "h264_qsv"


def test_probe_detects_missing_libass_and_no_hw_encoder(monkeypatch):
    monkeypatch.setattr(ff.shutil, "which", lambda name: "/usr/bin/" + name)
    outputs = {
        "-version": "ffmpeg version 6.1.1\n",
        "-filters": FILTERS_WITHOUT_SUBTITLES,
        "-encoders": "",
    }
    monkeypatch.setattr(ff, "_run", lambda args: outputs[args[-1]])
    caps = ff.probe_capabilities()
    assert caps.has_subtitles_filter is False
    assert caps.hw_encoder == ""
