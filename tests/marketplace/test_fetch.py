import io
import zipfile

import pytest

from orchestrator.marketplace.fetch import fetch_zip_archive, scan_root, source_dir


def test_source_dir_keys_by_id(tmp_path):
    assert source_dir(tmp_path, "abc") == tmp_path / "abc"


def test_fetch_zip_archive_extracts_into_source_dir(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mcp.json", "{}")
    archive = tmp_path / "upload.zip"
    archive.write_bytes(buf.getvalue())

    dest = fetch_zip_archive(tmp_path / "sources", "src1", str(archive))
    assert (dest / "mcp.json").is_file()


def test_fetch_zip_archive_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError):
        fetch_zip_archive(tmp_path, "src1", str(tmp_path / "nope.zip"))


def test_scan_root_narrows_to_subpath(tmp_path):
    fetched = tmp_path / "repo"
    (fetched / "sub").mkdir(parents=True)
    result = scan_root(fetched, "sub")
    assert result == (fetched / "sub").resolve()


def test_scan_root_rejects_escaping_subpath(tmp_path):
    fetched = tmp_path / "repo"
    fetched.mkdir()
    with pytest.raises(ValueError):
        scan_root(fetched, "../outside")
