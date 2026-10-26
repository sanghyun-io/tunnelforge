# 2.21.0 터널 복구 실패 조사 (TF-STATUS-214)

2026-10-23. 사용자 증상: 켜 두었던 터널들이 연결 해제로 표시되고 외부
AI Agent도 접속할 수 없지만, 시작을 누르면 기존 연결 관련 오류가 난다.
10월 23일 작업은 원인 조사와 재현이었다. 10월 26일 사용자가 전체 수정과
배포를 승인해 아래 후속 수정을 구현했다. 초기 증거와 수정 후 검증은 구분한다.

## 2026-10-26 수정과 검증

- 시작 성공 시 실제 포트를 별도 보관한다. 죽은 SSH의 포트 property 조회가
  실패해도 `stop(force=True)`를 실행한다. 종료 실패는 객체와 설정을 유지하고
  UI에 `정리 재시도`를 표시하며, 새 listener를 겹쳐 생성하지 않는다.
- 재연결 작업이 설정 사본을 소유하므로 기존 터널을 지운 뒤 첫 접속이 실패해도
  다음 시도가 이어진다. 자동 할당 포트도 장애 전의 실제 포트를 유지한다.
- 터널별 작업 잠금과 요청 세대로 수동 시작/중지와 재연결을 직렬화한다.
  새 요청이 들어오면 오래된 작업의 생성 결과까지 정리한다. UI worker의 취소도
  해당 시도의 세대가 아직 유효할 때만 종료할 수 있다.
- 자동 재연결 해제/모니터 종료는 대기 중 작업을 취소하며, 이미 SSH 접속 중인
  작업도 완료 후 취소 여부를 재확인한다. 자동 복구는 새 호스트 키나 개인키
  비밀번호를 묻지 않는다. 기존 호스트 키 검증과 세션의 복호화 암호 캐시는 유지한다.
- 기존 strict xfail 3개를 정상 회귀 테스트로 교체하고 경쟁/취소/종료 실패를 추가했다.
  실제 로컬 Paramiko SSH 서버의 단일/3개 동시 단절 후 동일 포트 복구와 바이트
  송수신을 확인했다. 관련 158개 테스트 통과. 최종 전체/배포 검증은 상태 문서 참조.

아래는 수정 전 코드에 대한 10월 23일 조사 기록이다.

## 결론과 증거 범위

가장 강한 원인은 **죽은 SSH forwarder를 정리하지 못하는 종료 코드**다.
SSH 전송이 끊겨도 로컬 listener와 engine 등록이 남을 수 있고, UI는 SSH
전송만 보고 시작 버튼을 표시한다. 이때 수동 시작도 같은 종료 결함에 막힌다.
실제 sshtunnel 속성과 실제 로컬 바인드 소켓을 사용한 격리 재현으로 확인했다.
SSH 통신 장애 자체를 실제 서버에서 발생시킨 통합 재현은 아니다.

v2.21.0 릴리스 merge commit `33152f1b53823fdada578e6e24f2e7bfb9743712`와
현재 HEAD `76b4ea9`의 아래 파일 차이는 없다:
`tunnel_engine.py`, `tunnel_monitor.py`, `tunnel_health_checker.py`,
`db_connection_worker.py`, `preselected_connect_dialog.py`, `main_window.py`.
이번 결과는 최신 버전에서 이미 고쳐졌다는 근거가 아니다.

## Drilling 1: 왜 UI는 시작이고 포트는 남아 있는가

1. `TunnelEngine.is_running()`은 forwarder의 `is_active`를 반환한다.
2. sshtunnel 0.4.0의 `is_active`는 SSH transport 활성 여부만 검사한다.
3. `main_window._build_power_button()`은 이 값이 False이면 시작을 표시한다.
4. 그러나 `active_tunnels` 등록, 로컬 listening socket의 존재 여부와는 다르다.
   SSH가 죽은 뒤 Python 쪽 listener가 남아 있는 상태가 가능하다.

DB ping 실패는 별개다. `TunnelHealthChecker`는 실패에 -1을 반환하고,
monitor는 latency만 None으로 바꾼다. 따라서 ping 실패 하나가 모든 터널을
종료시키는 코드는 아니다. 반대로 SSH만 살아 있으면 DB 장애를 연결 중으로
표시하는 관측 공백이 있다.

## Drilling 2: 죽은 터널을 왜 정리하지 못하는가 (주 원인)

`TunnelEngine.stop_tunnel()`은 다음 순서로 실행한다:

```python
port = getattr(server, 'local_bind_port', None)
server.stop()
```

`local_bind_port`는 단순 필드가 아닌 property다. sshtunnel 내부에서
`_check_is_started()`를 호출하며 SSH transport가 죽었으면
`BaseSSHTunnelForwarderError: Server is not started. Please .start() first!`
예외를 낸다. getattr의 기본값 None은 이 예외를 막지 못한다.

그 결과 server.stop(), TLS 등록 해제, active_tunnels/tunnel_configs 삭제가
모두 실행되지 않고 False를 반환한다. 수동 start_tunnel()도 죽은 기존
forwarder 정리를 먼저 수행하므로 같은 지점에서 실패하며
`기존 터널을 종료하지 못했습니다. 다시 시도해주세요.`를 반환한다.

자동 재연결도 같은 stop_tunnel()을 호출하고 반환값을 무시한다.
이어 start_tunnel()이 정리를 재시도하지만 같은 예외로 막히므로,
정상 포트/SSH 연결 생성까지 도달하지 못하고 재시도 한도를 소진할 수 있다.

정확한 메시지 구분이 필요하다. 이 경로는 위의 '기존 터널 종료 실패'를 낸다.
`이미 실행 중입니다.`는 현재 코드에서 **성공(True)** 반환이다.
`포트 ... 이미 사용 중입니다.`는 기존 터널 정리 후의 bind 검사 실패다.
사용자가 기억하는 문구가 정확히 어느 것인지는 아직 확인하지 않았다.
따라서 이 결함이 현장 사건과 완전히 동일하다고 단정하지 않는다.

## Drilling 3: 정리가 성공해도 왜 자동 복구가 멈추는가

`_reconnect_after_delay()`는 매 호출마다 engine.tunnel_configs에서 설정을
가져온다. 첫 호출은 설정을 로컬 변수로 보관한 뒤 stop_tunnel()을 호출한다.
stop은 engine의 설정을 삭제한다. 이후 접속이 일시 장애로 실패하면
`_start_ssh_tunnel()`은 성공 분기에만 설정을 등록하므로 설정은 복원되지 않는다.

두 번째 예약은 설정을 찾지 못해 `재연결 실패: 터널 설정을 찾을 수 없음`으로
끝난다. 기본 5회 설정이어도 실제 SSH 시작은 1회뿐인 경우가 재현됐다.
기존 테스트의 stop_tunnel Mock은 설정 삭제 부작용을 구현하지 않았다.

## Drilling 4: 수동 복구가 다시 끊길 수 있는가

예약된 재연결은 sleep 뒤 monitor._running만 확인한다. 해당 터널의 연결 세대,
재연결 취소 여부, 현재 이미 복구되었는지는 검사하지 않는다.
따라서 대기 중 수동으로 정상 연결을 만들었어도 이전 예약이 새 forwarder를
stop할 수 있다. 엔진의 터널별 start/stop 직렬화도 없으며, UI의 in-flight
검사는 UI worker끼리만 적용되고 monitor 재연결은 포함하지 않는다.
새 수동 연결 객체를 준비한 결정적 스케줄 재현에서 stop이 1회 호출됐다.
실제 동시 시작으로 orphan listener가 생기는지까지는 이번에 재현하지 않았다.

## 최초 장애와 여러 터널의 동시 장애

같은 네트워크/VPN 변경, 절전 복귀, bastion 단절은 여러 transport에 동시에
영향을 줄 수 있는 후보지만 사용자 로그 없이 최초 계기를 확정할 수 없다.
이번에 증명한 것은 **연결이 끊긴 뒤 복구할 수 없는 코드 경로**다.
30초 keepalive 설정 자체가 응답 기한이나 복구 성공을 보장하지도 않는다.

## 재현과 다음 수정 순서

`tests/test_tunnel_recovery_investigation.py`에 정상 동작 계약 3개를 strict
xfail로 남겼다. 정상 계약을 통과했다는 뜻이 아니라 현재 결함을 재현한다는 뜻이다.
실제 DB, SSH 서버, 사용자 앱 인스턴스에는 접속하지 않았다.

```powershell
.\.venv\Scripts\python -m pytest tests/test_tunnel_recovery_investigation.py tests/test_tunnel_engine.py tests/test_tunnel_monitor.py -q -rx
# 77 passed, 3 xfailed, 2 warnings
.\.venv\Scripts\python -m pytest tests/test_tunnel_recovery_investigation.py --runxfail -q --tb=short
# 3 failed: 종료 False / 실제 시작 1회 / 새 연결 stop 1회
```

수정 순서:
1. 죽은 transport에서도 포트 조회 오류가 실제 listener 정리를 막지 않도록 한다.
   포트는 시작 성공 시 별도로 보관하고, 종료 실패와 정리 완료를 구분한다.
2. 재시도 작업이 원본 설정과 연결 세대를 소유하게 한다. stop의 registry 삭제와
   독립적으로 설정을 보존하고, stop 실패 반환값을 처리한다.
3. 수동 start/stop과 자동 재연결을 터널별로 직렬화하고 오래된 예약을 취소한다.
4. 격리한 실제 SSH 연결을 끊고 listener 해제, 재접속, 여러 터널, 수동 복구 경쟁을
   검증한다. 위 xfail은 각 수정 시 제거한다.
