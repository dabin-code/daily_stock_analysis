import pytest

from src.storage import DatabaseManager

_LEAKS: list[str] = []


def _current_url():
    instance = getattr(DatabaseManager, "_instance", None)
    engine = getattr(instance, "_engine", None)
    return str(getattr(engine, "url", None)) if engine is not None else None


@pytest.fixture(autouse=True)
def _observe_database_singleton_leak(request):
    """记录哪些用例换掉了全局单例指向的库但没还原。

    单例泄漏的症状是跨用例的隐性失败，排查成本极高——本次定位
    test_e2e_five_layer_local 的两个失败花了四轮二分。
    """
    before = _current_url()
    yield
    after = _current_url()
    if before != after:
        _LEAKS.append(f"{request.node.nodeid}: {before} -> {after}")


def pytest_sessionfinish(session, exitstatus):
    if _LEAKS:
        print(f"\n[singleton-leak] {len(_LEAKS)} test(s) left a different database:")
        for line in _LEAKS:
            print(f"  {line}")
