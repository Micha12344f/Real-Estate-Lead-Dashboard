import pytest

from app.services.shared import config


@pytest.fixture(autouse=True)
def reset_runtime_mode():
    config.set_runtime_mode(None)
    config.apply_runtime_urls()
    yield
    config.set_runtime_mode(None)
    config.apply_runtime_urls()
