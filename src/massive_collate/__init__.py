"""Collate stock market data from the Massive API and analyse it."""

import os

# numpy's OpenBLAS commits a buffer for every CPU thread when it loads: 757 MB of private memory in
# every process of the app (and every command or build it starts) on a 24-thread machine, almost
# none of it used. The app's matrix work is small (Savitzky-Golay coefficients, cached): one thread
# (18 MB). Set before numpy loads; a value already in the environment is kept.
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

__version__ = "0.1.0"
