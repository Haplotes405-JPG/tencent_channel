"""tests 包：全部为**零框架依赖**的离线测试（不 import 核心，不需要 Neo-MoFox 运行环境）。

运行：``python -m unittest discover -s tests -t .``（在插件目录下执行）

注意：``test_manifest_static.StructureTest.test_no_build_artifacts`` 会检查源码树里没有
``__pycache__`` / ``.pyc`` / 工具链缓存。本包在导入时把 ``sys.dont_write_bytecode`` 打开，
以免跑测试本身就把这些产物写出来；打包前也建议先跑一次该测试。
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
