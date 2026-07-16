from soopts.collector.xml_parse import parse_chat_xml


def test_parses_chat_and_ogq(fixtures_dir):
    raw = (fixtures_dir / "chat_part0_0.xml").read_bytes()
    msgs = parse_chat_xml(raw, part=0, part_offset_s=0)
    kinds = [m.kind for m in msgs]
    assert "chat" in kinds
    assert "ogq" in kinds
    # ogq는 메시지가 비어도 보존된다
    ogq = [m for m in msgs if m.kind == "ogq"]
    assert len(ogq) == 1
    assert ogq[0].t == 15


def test_global_offset_applied(fixtures_dir):
    raw = (fixtures_dir / "chat_part0_300.xml").read_bytes()
    msgs = parse_chat_xml(raw, part=1, part_offset_s=1000)
    first = next(m for m in msgs if m.t_local == 301)
    assert first.t == 1301
    assert first.part == 1


def test_recovers_malformed_xml(fixtures_dir):
    # 이스케이프 안 된 &, 잘린 태그가 있어도 앞쪽 정상 메시지는 살아남는다
    raw = (fixtures_dir / "chat_part0_300.xml").read_bytes()
    msgs = parse_chat_xml(raw, part=0, part_offset_s=0)
    assert any("뭐야" in m.msg for m in msgs)


def test_empty_input():
    assert parse_chat_xml(b"", part=0, part_offset_s=0) == []
