from soopts.collector.xml_parse import parse_chat_xml
from soopts.models import make_chat_key


def test_dedup_key_stable():
    k1 = make_chat_key(0, 12, "라마바", "ㅋㅋㅋㅋ /04/ 왔다")
    k2 = make_chat_key(0, 12, "라마바", "ㅋㅋㅋㅋ /04/ 왔다")
    assert k1 == k2


def test_duplicate_messages_collapse_via_set(fixtures_dir):
    raw = (fixtures_dir / "chat_part0_0.xml").read_bytes()
    msgs = parse_chat_xml(raw, part=0, part_offset_s=0)
    # 파일에 동일 메시지가 2번 있다 → 파싱은 2개지만 key는 동일
    dup = [m for m in msgs if m.t_local == 12]
    assert len(dup) == 2
    assert dup[0].key == dup[1].key
    # dedup 셋 시뮬레이션
    seen = {m.key for m in msgs}
    assert len(seen) < len(msgs)
