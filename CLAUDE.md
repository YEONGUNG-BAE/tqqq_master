# TQQQ/QQQ 자동투자: 작업 규칙

진행 상황과 남은 검증은 `WORK_STATUS.md`에 기록한다. 이 파일에는 오래 유지할 규칙만 둔다.

## 실행 명령

```sh
.venv/bin/python -m unittest discover -s tests            # 전체 테스트 (네트워크 없음)
.venv/bin/python -m autotrader doctor --config config.json   # 읽기 전용 진단 요약 + JSON 저장
.venv/bin/python -m autotrader doctor --config config.json --json
.venv/bin/python -m autotrader run --config config.json      # observe: 계획만 기록
.venv/bin/python -m autotrader replay --scenario examples/self_irregular.json --state paper_self [--sessions N]
.venv/bin/python -m autotrader status --config config.json    # 잠금·보류·진행 중 요청 (요청 전송 없음)
.venv/bin/python -m autotrader unlock --config config.json --note '...'   # 원인 해소 후 거래 잠금 해제
```

환경: `.venv`, Python 3.14.5, exchange-calendars 4.12, pandas 2.3.3, NumPy 2.5.4.
exchange-calendars의 NumPy DeprecationWarning은 알려진 경고다. 근거 없이 재설치하거나 일괄 업그레이드하지 않는다.

## 고정 규칙

- 전략: QQQ 분할 조정·배당 미조정 종가, 당일 포함 252거래일 최고 종가 대비 하락률. TQQQ에서 −15% 이하 → QQQ, QQQ에서 −12% 이상 → TQQQ, 사이는 유지. 고점 만료 복귀는 유지하고 진단으로 기록한다. 새 예측·횡보 필터·임계값 최적화를 추가하지 않는다.
- 신호 상태와 실제 보유 상태를 따로 다룬다.
- 체결: 기존 연구는 다음 거래일 종가, 현재 코드 후보는 다음 정규장 개장 1~15분 지정가 순차 주문이다. 둘을 혼동하지 않고, 바꾸면 별도 변경으로 기록한다.
- 구조: 신호(`signal`, `history`) → 엔진(`core`) → 브로커 연결부(`meritz`, `paper`/`replay`). 수량·환전·호가 검사는 `core`의 함수만 쓴다. `diagnose`도 같은 함수를 재사용한다.
- 기본 `mode`는 `observe`. `verified` 항목은 실제 검증 없이 true로 바꾸지 않는다.
- `doctor`는 GET 조회와 `POST /oauth2/token`만 보낸다. 환전·매수·매도·정정·취소를 보내지 않는다.
- 정수 주식만 주문한다(소수점 API 미확인).
- 신규 환전·주문 직전에 해외 예약주문을 조회한다. 활성 예약이 있거나 조회가 실패·불완전하면 신규 실행을 차단한다. 예약주문은 자동 취소하지 않는다. 이미 접수한 자기 주문의 상태 확인은 차단하지 않는다.
- 예약주문 조회는 접수일 기준이므로, 고정 기간 조회를 전체 확인으로 취급하지 않는다. `reservation_lookup_start`(계좌 개설일 또는 그 이전)가 없으면 범위 미검증이며 live 신규 실행을 차단한다.
- 미국 정규장은 거래소 달력, 증권사 현재 구간(KST HHMM, 자정 넘김), 현재 시각이 모두 일치할 때만 인정한다. 장운영 코드(`004`/`011`)만으로 판정하지 않는다.
- 금융 요청(환전·주문·취소)은 의도(`prepared`) → 전송 시작(`submitting`) → 결과 순으로 저장한 뒤 보낸다. 저장이 실패하면 보내지 않는다. 같은 DB에서 진행 중인 요청은 한 건뿐이다.
- 응답 코드가 없는 응답과, 명세에 처리 전 거부로 적히지 않은 환전 오류는 결과 불명으로 다룬다. 조회 결과가 비어 있다는 이유로 미실행을 추정하지 않는다.
- 신호 장애·잠금·예약주문 같은 신규 실행 차단이 있어도 자기 요청의 상태 확인은 먼저 한다. 신선도·실행창 검사는 전송 직전의 현재 시각으로 한다.
- 응답이 유실되면 재전송하지 않는다. 증권사 기록이 정확히 1건 일치할 때만 그 요청으로 확정한다. 0건은 미접수의 근거가 아니며, 2건 이상이면 고르지 않고 잠근다. 증권사에는 멱등키가 없으므로 서버 측 exactly-once를 주장하지 않는다.
- 일시 보류는 자동 재개한다. 해소 불가 잠금은 사람이 `resolve`/`unlock`으로 푼다. 잠금 중에도 조회와 자기 주문 대조는 계속하지만 새 요청(취소 포함)은 보내지 않는다. 실패 횟수·불명 상태는 재시작해도 초기화하지 않는다.
- 이월·장중 재주문은 실행상의 예외 처리다. 백테스트 체결 가정과 같다고 보지 않고 기록한다.
- 알림에는 키·토큰·계좌·주문번호·잔고·금액을 넣지 않는다. 알림 실패는 거래 상태를 바꾸지 않는다.
- 기존 비전략 보유(QQQM·BRK.B 등)는 자동 매도·전환하지 않는다. `migrate_symbols`에 사용자가 명시할 때만 예외다.
- 비밀(`secrets.env`, `token.json`)과 상태(`state*/`, `paper_*/`)는 Git에서 제외한다. 내용을 출력하지 않는다. `paper_self`와 상태 DB는 삭제하거나 초기화하지 않는다.
- 상시 실행 등록(launchctl), 실제 자금 이동, live 전환, 원격 업로드는 사용자의 명시적 결정 전에는 하지 않는다.
