from pathlib import Path

import pytest

import scripts.hooks.check_subprocess_ban as ban


@pytest.fixture
def src_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "src" / "teatree" / "bad.py"
    target.parent.mkdir(parents=True)
    return target


class TestBanHook:
    def test_flags_subprocess_run_in_src_teatree(self, src_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        src_file.write_text("import subprocess\nsubprocess.run(['ls'], check=False)\n", encoding="utf-8")
        rc = ban.main([str(src_file)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "subprocess.run(...)" in err
        assert "src/teatree/bad.py:2" in err

    def test_flags_subprocess_popen(self, src_file: Path) -> None:
        src_file.write_text("import subprocess\nsubprocess.Popen(['sleep', '0'])\n", encoding="utf-8")
        assert ban.main([str(src_file)]) == 1

    def test_flags_subprocess_check_output(self, src_file: Path) -> None:
        src_file.write_text("import subprocess\nsubprocess.check_output(['true'])\n", encoding="utf-8")
        assert ban.main([str(src_file)]) == 1

    def test_allows_wrapper_module(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "src" / "teatree" / "utils" / "run.py"
        target.parent.mkdir(parents=True)
        target.write_text("import subprocess\nsubprocess.run(['ls'], check=False)\n", encoding="utf-8")
        assert ban.main([str(target)]) == 0

    def test_allows_scripts_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "scripts" / "hooks" / "whatever.py"
        target.parent.mkdir(parents=True)
        target.write_text("import subprocess\nsubprocess.run(['ls'], check=False)\n", encoding="utf-8")
        assert ban.main([str(target)]) == 0

    def test_allows_type_annotations(self, src_file: Path) -> None:
        src_file.write_text(
            "import subprocess\n"
            "procs: list[subprocess.Popen[str]] = []\n"
            "try:\n"
            "    pass\n"
            "except subprocess.CalledProcessError:\n"
            "    pass\n",
            encoding="utf-8",
        )
        assert ban.main([str(src_file)]) == 0

    def test_ignores_unrelated_attribute_calls(self, src_file: Path) -> None:
        src_file.write_text("import os\nos.system('ls')\n", encoding="utf-8")
        assert ban.main([str(src_file)]) == 0

    def test_returns_zero_for_clean_file(self, src_file: Path) -> None:
        src_file.write_text(
            "from teatree.utils.run import run_checked\nrun_checked(['ls'])\n",
            encoding="utf-8",
        )
        assert ban.main([str(src_file)]) == 0
