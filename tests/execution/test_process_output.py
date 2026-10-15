from redpanda.execution.processes import read_output


def test_output_cursor_preserves_split_utf8_and_terminal_filter(tmp_path):
    path = tmp_path / "stdout"
    path.write_bytes(b"x" * 16_383 + "中".encode() + b"\x1b[31mRED\x1b[0m")
    first = read_output(path, 0, finished=False)
    assert first["content"] == "x" * 16_383
    assert first["next_offset"] == 16_383 and first["has_more"]
    second = read_output(path, first["next_offset"], finished=False)
    assert second["content"] == "中RED"
    assert not second["has_more"]
    assert read_output(path, second["next_offset"], finished=True)["content"] == ""


def test_running_output_retains_unfinished_utf8_until_next_read(tmp_path):
    path = tmp_path / "stdout"
    path.write_bytes("中".encode()[:2])
    first = read_output(path, 0, finished=False)
    assert first["content"] == "" and first["next_offset"] == 0
    with path.open("ab") as stream:
        stream.write("中".encode()[2:])
    assert read_output(path, first["next_offset"], finished=True)["content"] == "中"
