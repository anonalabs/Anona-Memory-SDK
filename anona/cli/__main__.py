"""`python -m anona.cli`: the same command as `anona`."""

import sys

from .main import main

sys.exit(main())
