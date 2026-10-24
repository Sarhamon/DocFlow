"""누름틀 이름의 한글 표시명 (IN_02).

누름틀 이름은 전자문서 표준 서식의 영문 키다(docnumber, enforcedate …). 입력 화면은 이 표의
한글 이름을 보여 주고, 표에 없는 이름은 그대로 보여 준다. 관리자 페이지의 항목명 매핑(AD_14)이
서식별로 덮어쓸 수 있다.
"""

from __future__ import annotations

FIELD_LABELS: dict[str, str] = {
    "docnumber": "문서번호",
    "enforcedate": "시행일자",
    "content_title": "제목",
    "content_body": "본문",
    "zipcode": "우편번호",
    "address": "주소",
    "telephone": "전화",
    "fax": "팩스",
    "email": "전자우편",
    "homeurl": "홈페이지",
    "publication": "공개 구분",
    "receipt_docnumber": "접수번호",
    "receiptdate": "접수일자",
    "chief_sign": "기관장 서명",
    "seal": "관인",
    "seal_omit": "관인 생략",
    "APLY_REQ_TITLE": "신청 제목",
    "ACCT_DIV_RCD": "회계 구분",
    "BILL_AMT": "청구 금액",
    "BILL_AMT_TEXT": "청구 금액(한글)",
    "BILL_TYPE_RCD": "청구 구분",
    "CARD_CNT": "카드 증빙 수",
    "CASH_CNT": "현금영수증 수",
    "COST_CNT": "비용 증빙 수",
    "ETC_CNT": "기타 증빙 수",
    "TAX_CNT": "세금계산서 수",
    "TOTAL_CNT": "증빙 합계",
}


def field_label(name: str, overrides: dict[str, str] | None = None) -> str:
    """누름틀 이름 -> 화면에 보일 이름."""
    if overrides and name in overrides:
        return overrides[name]
    return FIELD_LABELS.get(name, name)
