"""网页口令、登录限流和登录流程的测试（D63 第 6、7 条）。"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from aureus.common import ManualClock, WebConfig
from aureus.web import (
    LoginLimiter,
    LoginLockedError,
    LoginRejectedError,
    PasswordPolicyError,
    PasswordStore,
    SessionStore,
    WebAuth,
    is_loopback,
)
from aureus.web.auth import check_new_password

START = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
LOCAL = "127.0.0.1"
LAN = "192.168.1.20"
GOOD = "correct horse"

# 测试里用最低的哈希开销，免得几十个测试各算一次完整的 argon2。
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def config() -> WebConfig:
    return WebConfig(login_max_failures=3, login_window_seconds=60, login_lockout_seconds=600)


@pytest.fixture
def password_file(tmp_path: Path) -> Path:
    return tmp_path / "config" / "web_password.argon2"


@pytest.fixture
def passwords(password_file: Path) -> PasswordStore:
    return PasswordStore(password_file, FAST_HASHER)


@pytest.fixture
def sessions(tmp_path: Path, clock: ManualClock) -> Iterator[SessionStore]:
    store = SessionStore(tmp_path / "aureus.db", clock, 7)
    store.open()
    yield store
    store.close()


@pytest.fixture
def auth(
    passwords: PasswordStore, sessions: SessionStore, config: WebConfig, clock: ManualClock
) -> WebAuth:
    return WebAuth(passwords, sessions, LoginLimiter(config, clock))


# ---------- 口令规则 ----------


@pytest.mark.parametrize("bad", ["", "short", "1234567", "admin", "ADMIN", "  admin  "])
def test_too_short_or_default_passwords_are_rejected(bad: str) -> None:
    with pytest.raises(PasswordPolicyError):
        check_new_password(bad)


def test_password_length_limits() -> None:
    check_new_password("x" * 8)
    check_new_password("x" * 256)
    with pytest.raises(PasswordPolicyError):
        check_new_password("x" * 257)


def test_policy_error_does_not_echo_the_password() -> None:
    with pytest.raises(PasswordPolicyError) as info:
        check_new_password("SECRET-pw" * 40)
    assert "SECRET" not in str(info.value)


# ---------- 来源地址 ----------


@pytest.mark.parametrize("address", ["127.0.0.1", "127.5.5.5", "::1", "::ffff:127.0.0.1"])
def test_loopback_addresses(address: str) -> None:
    assert is_loopback(address) is True


@pytest.mark.parametrize(
    "address", ["192.168.1.20", "10.0.0.1", "8.8.8.8", "::ffff:192.168.1.20", "", "localhost", "x"]
)
def test_non_loopback_and_unparseable_addresses(address: str) -> None:
    assert is_loopback(address) is False  # 认不出来的地址当作不是本机


# ---------- 口令文件 ----------


def test_without_a_file_the_default_password_works(passwords: PasswordStore) -> None:
    assert passwords.is_default() is True
    assert passwords.verify("admin") is True
    assert passwords.verify("Admin") is False
    assert passwords.verify("") is False
    assert passwords.verify("x" * 1000) is False


def test_set_password_stores_only_a_hash(passwords: PasswordStore, password_file: Path) -> None:
    passwords.set_password(GOOD)
    text = password_file.read_text(encoding="utf-8")
    assert GOOD not in text
    assert text.startswith("$argon2")
    assert passwords.is_default() is False
    assert passwords.verify(GOOD) is True
    assert passwords.verify("wrong password") is False
    assert passwords.verify("admin") is False  # 设过口令后默认口令失效


def test_set_password_leaves_no_temp_files(passwords: PasswordStore, password_file: Path) -> None:
    passwords.set_password(GOOD)
    assert [path.name for path in password_file.parent.iterdir()] == [password_file.name]


def test_rejected_new_password_keeps_the_old_one(
    passwords: PasswordStore, password_file: Path
) -> None:
    passwords.set_password(GOOD)
    before = password_file.read_text(encoding="utf-8")
    with pytest.raises(PasswordPolicyError):
        passwords.set_password("admin")
    assert password_file.read_text(encoding="utf-8") == before
    assert passwords.verify(GOOD) is True


@pytest.mark.parametrize("content", ["", "not a hash", "$argon2id$garbage", "\x00\x01"])
def test_a_broken_file_never_lets_anyone_in(
    passwords: PasswordStore, password_file: Path, content: str
) -> None:
    password_file.parent.mkdir(parents=True)
    password_file.write_text(content, encoding="utf-8")
    assert passwords.is_default() is False  # 文件坏了不能退回默认口令
    assert passwords.verify("admin") is False
    assert passwords.verify(GOOD) is False


def test_an_unreadable_path_never_lets_anyone_in(
    passwords: PasswordStore, password_file: Path
) -> None:
    password_file.mkdir(parents=True)  # 同名的是文件夹，读不了
    assert passwords.is_default() is False
    assert passwords.verify("admin") is False


def test_deleting_the_file_resets_to_the_default(
    passwords: PasswordStore, password_file: Path
) -> None:
    passwords.set_password(GOOD)
    password_file.unlink()
    assert passwords.is_default() is True
    assert passwords.verify("admin") is True


# ---------- 登录限流 ----------


def test_limiter_locks_after_enough_failures(config: WebConfig, clock: ManualClock) -> None:
    limiter = LoginLimiter(config, clock)
    for _ in range(2):
        limiter.record_failure(LAN)
    assert limiter.locked_for(LAN) is None
    limiter.record_failure(LAN)
    assert limiter.locked_for(LAN) == timedelta(seconds=600)


def test_lock_runs_out_with_time(config: WebConfig, clock: ManualClock) -> None:
    limiter = LoginLimiter(config, clock)
    for _ in range(3):
        limiter.record_failure(LAN)
    clock.advance(timedelta(seconds=599))
    assert limiter.locked_for(LAN) == timedelta(seconds=1)
    clock.advance(timedelta(seconds=1))
    assert limiter.locked_for(LAN) is None
    # 解锁后重新开始算：再失败两次还不会锁。
    limiter.record_failure(LAN)
    limiter.record_failure(LAN)
    assert limiter.locked_for(LAN) is None


def test_failures_outside_the_window_do_not_add_up(config: WebConfig, clock: ManualClock) -> None:
    limiter = LoginLimiter(config, clock)
    for _ in range(5):
        limiter.record_failure(LAN)  # 第 3 次起就锁了，这里只为推进时间前后各记几次
        clock.advance(timedelta(seconds=601))
    assert limiter.locked_for(LAN) is None


def test_slow_failures_never_lock(config: WebConfig, clock: ManualClock) -> None:
    limiter = LoginLimiter(config, clock)
    for _ in range(10):
        limiter.record_failure(LAN)
        clock.advance(timedelta(seconds=61))  # 每次都超出 60 秒窗口
        assert limiter.locked_for(LAN) is None


def test_each_address_is_counted_separately(config: WebConfig, clock: ManualClock) -> None:
    limiter = LoginLimiter(config, clock)
    for _ in range(3):
        limiter.record_failure(LAN)
    assert limiter.locked_for(LAN) is not None
    assert limiter.locked_for("192.168.1.21") is None
    assert limiter.locked_for(LOCAL) is None


def test_success_clears_the_failures(config: WebConfig, clock: ManualClock) -> None:
    limiter = LoginLimiter(config, clock)
    limiter.record_failure(LAN)
    limiter.record_failure(LAN)
    limiter.record_success(LAN)
    limiter.record_failure(LAN)
    limiter.record_failure(LAN)
    assert limiter.locked_for(LAN) is None


# ---------- 登录流程 ----------


def test_default_password_logs_in_from_this_machine_but_must_change(auth: WebAuth) -> None:
    result = auth.login("admin", LOCAL)
    assert result.must_change_password is True
    assert result.token
    assert auth.password_is_default is True


def test_default_password_is_refused_from_the_network(
    auth: WebAuth, sessions: SessionStore
) -> None:
    with pytest.raises(LoginRejectedError):
        auth.login("admin", LAN)
    assert sessions.revoke_all() == 0  # 没有留下任何登录状态


def test_default_password_attempts_from_the_network_count_as_failures(auth: WebAuth) -> None:
    for _ in range(3):
        with pytest.raises(LoginRejectedError):
            auth.login("admin", LAN)
    with pytest.raises(LoginLockedError):
        auth.login("admin", LAN)


def test_wrong_password_is_rejected_without_echoing_it(auth: WebAuth) -> None:
    with pytest.raises(LoginRejectedError) as info:
        auth.login("SECRET-guess", LOCAL)
    assert "SECRET" not in str(info.value)


def test_login_works_from_the_network_once_the_password_is_changed(auth: WebAuth) -> None:
    auth.change_password("admin", GOOD, LOCAL)
    result = auth.login(GOOD, LAN)
    assert result.must_change_password is False
    assert auth.password_is_default is False


def test_too_many_failures_lock_even_the_right_password(auth: WebAuth) -> None:
    auth.change_password("admin", GOOD, LOCAL)
    for _ in range(3):
        with pytest.raises(LoginRejectedError):
            auth.login("wrong password", LAN)
    with pytest.raises(LoginLockedError) as info:
        auth.login(GOOD, LAN)
    assert info.value.retry_after > timedelta(0)
    assert auth.login(GOOD, LOCAL).token  # 锁的是那个 IP，不影响别处


def test_lock_ends_after_the_lockout_time(auth: WebAuth, clock: ManualClock) -> None:
    auth.change_password("admin", GOOD, LOCAL)
    for _ in range(3):
        with pytest.raises(LoginRejectedError):
            auth.login("wrong password", LAN)
    clock.advance(timedelta(seconds=600))
    assert auth.login(GOOD, LAN).token


def test_confirm_password_checks_and_counts_failures(auth: WebAuth) -> None:
    auth.change_password("admin", GOOD, LOCAL)
    auth.confirm_password(GOOD, LAN)  # 改身份事实和不变人格前用（D63 第 1 条）
    for _ in range(3):
        with pytest.raises(LoginRejectedError):
            auth.confirm_password("wrong password", LAN)
    with pytest.raises(LoginLockedError):
        auth.confirm_password(GOOD, LAN)


# ---------- 改口令 ----------


def test_changing_the_password_logs_every_device_out(auth: WebAuth, sessions: SessionStore) -> None:
    phone = auth.login("admin", LOCAL).token
    laptop = auth.login("admin", LOCAL).token
    auth.change_password("admin", GOOD, LOCAL)
    assert sessions.touch(phone) is False
    assert sessions.touch(laptop) is False
    assert auth.login(GOOD, LOCAL).token  # 新口令能登录
    with pytest.raises(LoginRejectedError):
        auth.login("admin", LOCAL)  # 旧的不行


def test_changing_needs_the_old_password(auth: WebAuth, sessions: SessionStore) -> None:
    token = auth.login("admin", LOCAL).token
    with pytest.raises(LoginRejectedError):
        auth.change_password("not the old one", GOOD, LOCAL)
    assert sessions.touch(token) is True  # 没改成，登录状态不动
    assert auth.password_is_default is True


def test_a_bad_new_password_changes_nothing(auth: WebAuth, sessions: SessionStore) -> None:
    token = auth.login("admin", LOCAL).token
    with pytest.raises(PasswordPolicyError):
        auth.change_password("admin", "admin", LOCAL)
    with pytest.raises(PasswordPolicyError):
        auth.change_password("admin", "short", LOCAL)
    assert sessions.touch(token) is True
    assert auth.password_is_default is True


def test_changing_the_default_password_from_the_network_is_refused(auth: WebAuth) -> None:
    with pytest.raises(LoginRejectedError):
        auth.change_password("admin", GOOD, LAN)
    assert auth.password_is_default is True
