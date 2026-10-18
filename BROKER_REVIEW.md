# 종가 시장가 양방향 주문 후보 확인

2026-10-15. 검색·공식 문서 확인만 수행했고, 계좌 개설이나 주문은 하지 않았습니다.

| 후보 | 확인한 내용 | 남은 핵심 조건 |
|---|---|---|
| 메리츠 | 미국시장 공식 안내는 MOC 매도만 지원. OpenAPI 매수의 공통 enum에 MOC가 있어도 이 문장과 모순되므로 매수 가능 증거로 삼지 않음 | API의 실제 상품별 허용 유형, 미체결 매도 주문을 매수 자금으로 인정하는지 |
| 한국투자 | 공식 API 함수 설명은 미국 매수 `00` 지정가·`32` LOO·`34` LOC; 미국 매도에 `33` MOC 포함 | MOC 매수 대안으로 확인되지 않음 |
| IBKR | 공식 REST/TWS API 문서에 MOC 주문 포함. 공식 Desktop 설명은 MOC **매수** 예제를 직접 제시. 계좌 개설 가능 지역 목록에 Republic of Korea 포함 | 해당 계좌의 TQQQ/QQQ 거래 허가, 사전 주문 자금·마진 검사, 양방향 동시 주문 처리, 원화 입금 경로·자동 환전·운영 인증 조건 |
| LS | 공식 공개 목록에 미국시장주문 API COSAT00301 확인 | 상세 페이지의 업스트림 오류로 매수 MOC 허용을 공식 문서에서 확정하지 못함 |

현재 조사에서 **국내 계좌 + API MOC 매수·매도 + 별도 현금 없이 같은 종가 전액 전환**을 모두 확인한 대안은 없습니다. 국내 전체에서 불가능하다는 뜻은 아닙니다. IBKR는 MOC 자체의 유효한 후보지만 완성된 대체 계좌로 확정하지 않았습니다. 원화 지원에 관한 IBKR 일반 문서는 있으나, 한국 거주자의 실제 소속 법인·입금 경로와 자동 환전 조건이 맞는지는 따로 확인해야 합니다.

MOC는 다음 날 종가 기준 백테스트를 가깝게 구현하는 도구입니다. 전략의 일봉 신호 계산이나 자동화 자체에 반드시 필요한 주문 종류는 아닙니다. MOC가 양방향으로 있어도 매도 체결 전 매수 주문을 접수할 자금 조건이 별개입니다. 종가 경매 체결을 확인한 뒤 새 MOC를 내면 그날 경매가 이미 끝나므로, 순차 프로그램만으로 같은 경매의 매수·매도를 만들 수 없습니다. 별도 현금·계좌의 사전 주문 인정·마진 여유 등의 실제 조건을 검증해야 합니다. 마진 계좌도 두 주문이 무조건 허용되거나 정확히 전액 교체된다는 뜻은 아닙니다.

국내 계좌에서 종가 부근에 순차 주문하거나 다음 날 개장 직후 순차 주문하는 후보는 비교할 수 있지만, 시가/종가 일봉만으로 그 분 단위 실제 체결 성과를 재현했다고 말할 수 없습니다. 지금 만들어진 메리츠 실행 후보는 개장 직후 지정가 순차 주문이며 관찰 모드로 제공됩니다. 브로커와 체결 방식은 아직 최종 확정하지 않았습니다.

직접 확인할 문의 사항은 다음과 같습니다. 문의를 대신 발송하지 않았습니다.

1. 개인 OpenAPI에서 QQQ와 TQQQ를 MOC로 매수·매도할 수 있는가?
2. 기존 종목의 미체결 MOC 매도 주문만 있는 상태에서 그 예상 대금으로 다른 ETF의 MOC 매수를 접수할 수 있는가?
3. 현금 계좌에서 가능하지 않다면 필요한 현금·증거금 규모와 추가 비용은 무엇인가?
4. 원화 입금 후 API 환전과 해외주식 주문에도 해당 계좌의 수수료·환전 우대가 적용되는가?

출처:

- 메리츠: https://home.imeritz.com/fstock/ForeignStock02_10.do
- 한국투자 공식 소스: https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/overseas_stock/overseas_stock_functions.py (`order` 함수 설명)
- IBKR 현행 API 주문 유형: https://www.interactivebrokers.com/campus/?p=195739&post_type=ibkr-api-page
- IBKR MOC 매수 설명: https://www.interactivebrokers.com/campus/trading-lessons/ibkr-desktop-market-on-close-order/
- IBKR 개설 가능 지역: https://www.interactivebrokers.com/en/accounts/open-account-country-list.php
- IBKR 현금 계좌의 결제 자금 설명: https://www.interactivebrokers.com/campus/trading-lessons/using-the-globaltrader-swap/ (과거 댓글의 결제 일수 표현은 현재 일정으로 사용하지 않음)
- IBKR KRW 일반 설명: https://www.interactivebrokers.com/campus/glossary-terms/krw/
- LS 공식 목록: https://openapi.ls-sec.co.kr/apiservice
