import pytest
from core.config import Settings
from core.llm.provider import get_model


def test_muse_spark_is_explicit_swap_point():
    with pytest.raises(NotImplementedError):
        get_model(Settings(llm_provider="muse_spark"))


def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        get_model(Settings(llm_provider="nope"))
