"""Single CLI entry for the current EsgAgent -> B -> workbench workflow."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT))


def main(argv=None):
    from esg_pipeline.esgagent_cli import main as run
    return run(argv)


if __name__=='__main__':
    raise SystemExit(main())
