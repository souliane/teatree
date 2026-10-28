from pathlib import Path


def install_build_space_stub(stub_bin: Path) -> None:
    script = stub_bin / "df"
    script.write_text(
        "#!/bin/sh\n"
        'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"\n'
        'echo "/dev/test 50000000 1 49999999 1% $2"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
