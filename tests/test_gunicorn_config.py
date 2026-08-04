"""验证生产服务始终使用单 worker 和高位回环端口。"""

import runpy


def test_gunicorn_config_enforces_single_worker_and_high_port(
    monkeypatch,
) -> None:
    """验证生产配置不会产生多进程抓取或监听低位端口。

    参数：
        monkeypatch: pytest 环境变量隔离工具。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    monkeypatch.setenv("PORT", "28888")
    monkeypatch.setenv("BIND_HOST", "127.0.0.1")
    config = runpy.run_path("gunicorn.conf.py")

    assert config["workers"] == 1
    assert config["bind"] == "127.0.0.1:28888"
    assert config["accesslog"] is None


def test_gunicorn_config_supports_explicit_public_high_port(monkeypatch) -> None:
    """验证没有 CDN 时可显式开放高位公网监听。

    参数：
        monkeypatch: pytest 环境变量隔离工具。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    monkeypatch.setenv("PORT", "28888")
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")

    config = runpy.run_path("gunicorn.conf.py")

    assert config["bind"] == "0.0.0.0:28888"
