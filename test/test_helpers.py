import pytest
from unittest.mock import patch, mock_open
from helpers import timeit, BlockLoader, Blocks


def test_timeit_decorator_logs_time(caplog):
    @timeit
    def dummy(x):
        return x + 1
    with caplog.at_level("INFO"):
        result = dummy(1)
    assert result == 2
    assert "Function dummy took" in caplog.text


def test_blockloader_get_block(monkeypatch):
    fake_json = '{"blocks": [{"type": "section", "block_id": "b1"}]}'
    with patch("builtins.open", mock_open(read_data=fake_json)):
        blocks = BlockLoader.get_block("fakefile")
        assert isinstance(blocks, list)
        assert blocks[0]["block_id"] == "b1"


def test_blockloader_get_blocks(monkeypatch):
    fake_json = '{"blocks": [{"type": "section", "block_id": "b1"}]}'
    with patch("builtins.open", mock_open(read_data=fake_json)):
        blocks = BlockLoader.get_blocks("fakefile")
        assert isinstance(blocks, list)
        assert blocks[0]["block_id"] == "b1"


def test_blockloader_get_block_exp_logs_not_mapped(caplog):
    fake_json = '{"blocks": [{"type": "section", "block_id": "b1"}]}'
    with patch("builtins.open", mock_open(read_data=fake_json)):
        with caplog.at_level("INFO"):
            blocks = BlockLoader.get_block_exp("fakepath", "bid", "aid")
        assert blocks[0]["block_id"] == "bid"
        assert "NOT MAPPED" in caplog.text
