import os
from pathlib import Path

with Path(os.environ["NEEDLETAIL_TEST_LOG"]).open("a", encoding="utf-8") as log:
    log.write("export\n")
