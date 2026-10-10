"""Logs enthalten keine URLs: httpx/httpcore/mcp erst ab WARNING."""

import logging

import pytest


@pytest.mark.parametrize("name", ["httpx", "httpcore", "mcp"])
def test_http_loggers_not_info(name):
    assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
