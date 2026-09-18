"""hypothesis 跑量档位（03 §三 已决 1）。

日常 CI 每条 ≥1 万例，深度档 ≥10 万例在每周与 Step 验收前于家里 WSL2 跑：
`ZX_HYPOTHESIS_EXAMPLES=100000 uv run pytest tests/properties`。
只在这一处读环境变量，测试文件不各写各的数，否则"深度档"到底是多大没人说得清。
"""

import os

from hypothesis import settings

MAX_EXAMPLES = int(os.environ.get("ZX_HYPOTHESIS_EXAMPLES", "10000"))

property_settings = settings(max_examples=MAX_EXAMPLES, deadline=None)
