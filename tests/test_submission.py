from zipfile import ZipFile

import pytest
import yaml

from scripts import submission


@pytest.mark.parametrize("name", [".env", ".env.local", "Dockerfile", "sample_data/employees.json", ".local/data/file.db", "../.env"])
def test_prohibited_submission_paths(name):
    assert submission.forbidden_path(name)


def test_source_allowlist_and_workflow_are_valid():
    files = submission.validate()
    assert "app/main.py" in files and ".env.example" in files
    assert not any(submission.forbidden_path(name) for name in files)
    workflow = yaml.load((submission.ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert {"push", "pull_request", "workflow_dispatch"} <= workflow["on"].keys()
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["test"]["strategy"]["matrix"]["include"] == [
        {"python": "3.11", "mongodb": "6.0"}, {"python": "3.13", "mongodb": "7.0"}]


def test_archive_is_deterministic_and_only_contains_source():
    first, first_digest = submission.build_archive()
    second, second_digest = submission.build_archive()
    assert first == second and first_digest == second_digest
    with ZipFile(second) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(submission.SOURCE_FILES)
        for name in archive.namelist():
            assert archive.read(name) == (submission.ROOT / name).read_bytes()


def test_credential_check_reports_only_filename(tmp_path):
    for relative in submission.SOURCE_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((submission.ROOT / relative).read_text(), encoding="utf-8")
    # Build the synthetic credential at runtime; never put a credential-shaped
    # URI in this published test file.
    credential = "mongodb://" + "test-user:test-password" + "@example.invalid"
    (tmp_path / "README.md").write_text(credential)
    with pytest.raises(ValueError, match="README.md") as error:
        submission.validate(tmp_path)
    assert credential not in str(error.value)
