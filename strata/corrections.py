"""近义入口：corrections 与 candidates 使用同一实现。"""

from strata.candidates import *  # noqa: F401,F403
from strata.collab.candidates import Candidates as Corrections  # noqa: F401
