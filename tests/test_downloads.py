import httpx

from videomaker.downloads import download_file


def test_download_file_streams_to_dest(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"model-bytes")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "sub" / "model.onnx"
    result = download_file("https://example.com/model.onnx", dest, client=client)
    assert result == dest
    assert dest.read_bytes() == b"model-bytes"
    assert not dest.with_suffix(".onnx.part").exists()


def test_download_file_raises_on_http_error(tmp_path):
    def handler(request):
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "model.onnx"
    try:
        download_file("https://example.com/missing", dest, client=client)
        raise AssertionError("expected HTTPStatusError")
    except httpx.HTTPStatusError:
        assert not dest.exists()
