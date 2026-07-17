import base64

import pytest
from requests import RequestException

import generate_image


class FakeResponse:
    def __init__(self, chunks, content_length=0):
        self._chunks = chunks
        self.headers = {"Content-Length": str(content_length)} if content_length else {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        assert chunk_size == 64 * 1024
        yield from self._chunks


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, *, timeout, stream):
        assert url == "https://images.example/result.png"
        assert timeout == 30
        assert stream is True
        return self.response


def test_save_url_image_streams_to_an_atomic_file(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_image, "MAX_DOWNLOAD_BYTES", 10)
    output = tmp_path / "result.png"

    generate_image.save_url_image(
        FakeSession(FakeResponse([b"abc", b"def"])),
        "https://images.example/result.png",
        output,
        timeout=30,
    )

    assert output.read_bytes() == b"abcdef"
    assert not list(tmp_path.glob("*.part"))


def test_save_url_image_rejects_oversized_stream_and_removes_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_image, "MAX_DOWNLOAD_BYTES", 5)
    output = tmp_path / "result.png"

    with pytest.raises(RequestException, match="超过"):
        generate_image.save_url_image(
            FakeSession(FakeResponse([b"abc", b"def"])),
            "https://images.example/result.png",
            output,
            timeout=30,
        )

    assert not output.exists()
    assert not list(tmp_path.iterdir())


def test_save_b64_image_enforces_decoded_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_image, "MAX_DOWNLOAD_BYTES", 2)
    encoded = base64.b64encode(b"123").decode("ascii")

    with pytest.raises(ValueError, match="超过"):
        generate_image.save_b64_image(encoded, tmp_path / "result.png")

    assert not list(tmp_path.iterdir())


def test_load_image_paths_enforces_file_size_limit(tmp_path, monkeypatch):
    image = tmp_path / "reference.png"
    image.write_bytes(b"123")
    monkeypatch.setattr(generate_image, "MAX_INPUT_IMAGE_BYTES", 2)

    with pytest.raises(SystemExit, match="超过"):
        generate_image.load_image_paths([str(image)])


def test_load_config_warns_when_secret_file_is_world_readable(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.local.json"
    config.write_text('{"api_key":"secret"}', encoding="utf-8")
    config.chmod(0o644)
    monkeypatch.setattr(generate_image, "CONFIG_CANDIDATES", [config])

    assert generate_image.load_config()["api_key"] == "secret"
    assert "chmod 600" in capsys.readouterr().err
