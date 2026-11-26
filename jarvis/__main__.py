import sys
from pathlib import Path

from jarvis.app import main

if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
    from jarvis.selftest import run

    raise SystemExit(run(Path(sys.argv[2])))
raise SystemExit(main())
