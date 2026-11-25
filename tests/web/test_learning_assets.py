"""合成登录验证 D111 工作台静态入口、离线依赖和路径边界。"""

import re
from pathlib import Path

from tests.web.config_rig import ConfigRig


def test_learning_workbench_requires_existing_login_and_serves_local_assets(tmp_path: Path) -> None:
    rig = ConfigRig(tmp_path)
    try:
        path = "/learning-workbench/"
        assert rig.client.get(path).status_code == 401
        rig.log_in()
        response = rig.client.get(path)
        assert response.status_code == 200
        assert "Aureus 学习工作台" in response.text
        assert "connect-src 'self'" in response.headers["content-security-policy"]
        assets = re.findall(r'(?:src|href)="([^"]+)"', response.text)
        assert assets
        for asset in assets:
            assert asset.startswith(path)
            loaded = rig.client.get(asset)
            assert loaded.status_code == 200
            assert loaded.headers["cache-control"] == "no-store"
        missing = rig.client.get(path + "missing.js")
        traversal = rig.client.get(path + "%2e%2e%2findex.html")
        assert missing.status_code == traversal.status_code == 404
    finally:
        rig.close()
