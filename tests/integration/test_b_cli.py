"""Legacy B remains available to developers without redundant root BAT files."""
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]


def test_b_demo_cli_help_from_another_directory(tmp_path):
    result=subprocess.run([sys.executable,str(ROOT/'scripts/esg_audit_run.py'),'--help'],
                          cwd=tmp_path,capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr.decode(errors='replace')
    assert b'--fixture-mode' in result.stdout and b'--demo' in result.stdout
