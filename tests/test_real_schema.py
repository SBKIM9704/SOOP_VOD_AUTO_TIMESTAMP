"""실제 SOOP 채팅 XML 스키마 회귀 테스트.

실전 VOD(197718401)에서 드러난 버그:
  - 메시지가 <![CDATA[...]]> 로 감싸짐
  - 스티커 <ogq>는 시각 <t>, 전송자 <s>/<sn>을 씀 → 시각 후보에 "s"가 있으면
    <s>(전송자 ID)를 시각으로 오독해 스티커 전량 드롭됐었다.
"""

from soopts.collector.xml_parse import parse_chat_xml


def test_cdata_message_extracted(fixtures_dir):
    raw = (fixtures_dir / "soop_real_schema.xml").read_bytes()
    msgs = parse_chat_xml(raw, part=0, part_offset_s=0)
    assert any(m.msg == "띵하" for m in msgs)
    # 이스케이프 안 된 &가 CDATA 안에 있어도 살아남는다
    assert any("Tom & Jerry" in m.msg for m in msgs)


def test_ogq_sticker_parsed_with_correct_time(fixtures_dir):
    raw = (fixtures_dir / "soop_real_schema.xml").read_bytes()
    msgs = parse_chat_xml(raw, part=0, part_offset_s=0)
    ogq = [m for m in msgs if m.kind == "ogq"]
    assert len(ogq) == 1, "스티커가 <s> 오독으로 드롭되면 안 된다"
    s = ogq[0]
    assert s.t_local == 24            # <t>24.334> → 24 (not <s>singgyul(3)>)
    assert s.nick == "띵귤_"          # <sn>에서 추출
    assert s.user_id == "singgyul(3)"  # <s>에서 추출


def test_chat_time_not_confused(fixtures_dir):
    raw = (fixtures_dir / "soop_real_schema.xml").read_bytes()
    msgs = parse_chat_xml(raw, part=0, part_offset_s=0)
    chat = [m for m in msgs if m.kind == "chat"]
    assert {m.t_local for m in chat} == {19, 30}
