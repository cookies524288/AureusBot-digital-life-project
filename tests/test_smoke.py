"""冒烟测试：确认 aureus 包已正确安装、能被导入。

这是 Phase 0 第 1 步的占位测试，后面的步骤会补上真正的测试。
"""

import aureus


def test_package_has_version() -> None:
    # 能读到版本号，说明 uv 已经把 aureus 安装进了项目环境。
    assert isinstance(aureus.__version__, str)
    assert aureus.__version__ != ""
