"""ChatLoadSplit XML → list[ChatMsg] 순수 파서.

HTTP와 분리해 픽스처 bytes로 단위 테스트한다. 스크랩 XML은 비정상 엔티티/truncation이
잦으므로 lxml의 recover 모드로 관대하게 파싱한다.

SOOP 채팅 XML 스키마는 비공식이고 시기·엔드포인트에 따라 조금씩 다르다. 따라서 시각/닉네임/
메시지/유저ID를 여러 후보 태그·속성에서 관대하게 추출한다. 새 필드명이 나오면 아래 후보
목록만 늘리면 된다.
"""

from __future__ import annotations

from lxml import etree

from soopts.models import ChatMsg, make_chat_key

# 각 필드에 대한 후보 자식 태그명 (소문자 비교).
# 주의: 짧은 별칭("s" 등)은 다른 필드와 충돌하므로 넣지 않는다.
#   - SOOP 실제 스키마: <chat>은 <t>/<n>/<u>, <ogq>(스티커)는 <t>/<sn>/<s> 사용.
_TIME_TAGS = ("t", "time", "second", "sec", "playtime", "starttime")
_NICK_TAGS = ("n", "nick", "nickname", "name", "username", "sn")  # sn=스티커 전송자 닉
_MSG_TAGS = ("m", "msg", "message", "text", "comment", "cont", "content")
_UID_TAGS = ("u", "id", "userid", "user_id", "uid", "s")          # s=스티커 전송자 ID


def _first_child_text(el: etree._Element, names: tuple[str, ...]) -> str | None:
    for child in el:
        tag = etree.QName(child).localname.lower() if isinstance(child.tag, str) else ""
        if tag in names:
            return (child.text or "").strip()
    return None


def _first_attr(el: etree._Element, names: tuple[str, ...]) -> str | None:
    for k, v in el.attrib.items():
        if k.lower() in names:
            return (v or "").strip()
    return None


def _extract(el: etree._Element, names: tuple[str, ...]) -> str | None:
    """자식 태그 → 속성 순으로 첫 매칭 값을 반환."""
    val = _first_child_text(el, names)
    if val is not None and val != "":
        return val
    return _first_attr(el, names)


def _parse_time(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        # 초 단위 정수/실수 모두 허용
        return int(float(raw))
    except (ValueError, TypeError):
        return None


def parse_chat_xml(xml_bytes: bytes, part: int, part_offset_s: int) -> list[ChatMsg]:
    """원본 XML bytes를 ChatMsg 리스트로 변환한다.

    part_offset_s: 이 파트의 전역 타임라인 시작 오프셋(초). t = t_local + part_offset_s.
    깨진 XML은 recover 모드로 최대한 살리고, 파싱 불가한 요소는 조용히 건너뛴다.
    """
    if not xml_bytes:
        return []

    parser = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)
    try:
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError:
        return []
    if root is None:
        return []

    out: list[ChatMsg] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = etree.QName(el).localname.lower()
        if tag not in ("chat", "ogq"):
            continue

        t_local = _parse_time(_extract(el, _TIME_TAGS))
        if t_local is None:
            continue
        msg = _extract(el, _MSG_TAGS) or ""
        nick = _extract(el, _NICK_TAGS) or ""
        uid = _extract(el, _UID_TAGS) or ""

        # ogq(스티커)는 메시지가 비어있을 수 있으나 반응 신호이므로 보존한다.
        if tag == "chat" and msg == "":
            continue

        out.append(
            ChatMsg(
                key=make_chat_key(part, t_local, nick, msg),
                part=part,
                t=t_local + part_offset_s,
                t_local=t_local,
                kind=tag,
                nick=nick,
                user_id=uid,
                msg=msg,
            )
        )
    return out
