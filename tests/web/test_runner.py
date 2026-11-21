"""网页服务启动检查的测试（路线图 1.10，D63 第 6 条、D53 第 5 条）。"""

import pytest
from fastapi import FastAPI

from aureus.common import ConfigError, WebConfig
from aureus.web.runner import (
    ALL_INTERFACES_HOST,
    LOOPBACK_HOST,
    WebServer,
    check_can_start,
    listen_host,
)


def test_it_listens_on_this_machine_only_by_default() -> None:
    assert listen_host(WebConfig()) == LOOPBACK_HOST
    assert WebConfig().port == 28976


def test_allow_remote_listens_on_every_interface() -> None:
    assert listen_host(WebConfig(allow_remote=True)) == ALL_INTERFACES_HOST


def test_the_default_password_is_fine_while_only_this_machine_can_connect() -> None:
    check_can_start(WebConfig(allow_remote=False), password_is_default=True)


def test_a_changed_password_is_fine_with_remote_access() -> None:
    check_can_start(WebConfig(allow_remote=True), password_is_default=False)
    check_can_start(WebConfig(allow_remote=False), password_is_default=False)


def test_remote_access_with_the_default_password_refuses_to_start() -> None:
    with pytest.raises(ConfigError) as info:
        check_can_start(WebConfig(allow_remote=True), password_is_default=True)
    message = str(info.value)
    assert "allow_remote" in message
    assert "默认" in message


def test_stopping_before_starting_is_harmless_and_repeatable() -> None:
    server = WebServer(FastAPI(), WebConfig())
    assert server.started is False
    server.request_stop()
    server.request_stop()
