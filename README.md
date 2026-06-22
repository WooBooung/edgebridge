# edgebridge-aeb

> **[toddaustin07/edgebridge](https://github.com/toddaustin07/edgebridge)** 의 포크입니다.
> 원작 edgebridge 에 **AndroidEdgeBridge(AEB)** 의 기능 대부분(LLM·Bluetooth 제외)을 이식하고,
> **라즈베리파이 / 시놀로지 NAS 호환 멀티아치 Docker 이미지** 를 Docker Hub 로 자동 배포합니다.

[![Docker Pulls](https://img.shields.io/docker/pulls/woobooung/edgebridge-aeb)](https://hub.docker.com/r/woobooung/edgebridge-aeb)

`docker pull woobooung/edgebridge-aeb` · 지원 아키텍처: `linux/amd64`, `linux/arm64`

---

## 🙏 감사의 글 (Acknowledgements)

- **MQTT 브리지 참고 구현 코드**를 제공해주신 **두더싱(스마트싱스 네이버 카페) 산사나이님**께 진심으로 감사드립니다.
  이 포크의 MQTT 이식은 산사나이님이 직접 테스트하고 공유해주신 코드를 1차 레퍼런스로 삼았습니다.
- 원본 브리지 서버를 만들어 공개해주신 **Todd Austin (toddaustin07)** 님께 감사드립니다.

### 관련 링크
- 두더싱 카페(스마트싱스 네이버 카페, 비공식): <https://cafe.naver.com/dothesmartthings>
- AndroidEdgeBridge(AEB) 홈페이지: <https://aeb.dothesmartthings.com>
- AEB 개발자 가이드(이식한 API 원문): <https://aeb.dothesmartthings.com/dev-guide.html>
- 원작 edgebridge: <https://github.com/toddaustin07/edgebridge>

---

## 원작 대비 무엇이 다른가 (What's different)

| 기능 | 원작 edgebridge | **edgebridge-aeb** |
|------|-----------------|--------------------|
| `/api/forward` (HTTP 포워딩) | GET / POST / PUT | **+ DELETE / PATCH 추가** |
| **한글·멀티바이트 응답 truncation** | ❌ 응답이 잘림(버그) | ✅ **수정됨** (아래 설명) |
| `/mqtt/*` MQTT 브리지 (mTLS 구독 → 허브 포워딩) | 없음 | ✅ **이식** ([스펙](mqtt-bridge-spec-v0.3.md)) |
| `/api/redirect` (path→URL 영속 매핑 + 자동 프록시) | 없음 | ✅ **이식** |
| `/api/callback` (name 키로 임의 값 저장/조회) | 없음 | ✅ **이식** |
| **mDNS 자동 발견** (`_edgebridge._tcp`) | 없음 | ✅ **이식** (드라이버가 브리지 자동 검색, host 네트워크 필요) |
| `/api/ping` | 단순 200 | ✅ **AEB 호환 JSON** (battery=100, bridgeVersion, mqtt 세션 요약 등) |
| 데이터 영속화 | `.registrations` 만 | `.registrations` + `redirects.jsonl` + `callbacks.jsonl` (data dir) |
| 멀티아치 Docker 이미지 자동배포 | 수동 빌드 | ✅ **GitHub Actions → Docker Hub (amd64/arm64)** |
| 동시 요청 처리 | 단일 스레드 | **ThreadingHTTPServer (멀티 스레드)** |

### 이식하지 **않은** 것 (AEB 대비)
- `/api/llm` (LLM 직접 호출) — 의도적으로 제외, 호출 시 404.
- Bluetooth(BT) 경로 API — 제외.
- AEB ping의 `pub_key`(Config Sync), `supportedAiOptions`(LLM), OAuth 토큰 필드 — 미이식 기능이라 ping에서 빈값/false 처리.
- `/api/forward` 의 ST OAuth access_token 자동 갱신 주입 — 헤드리스 Docker 환경에 부적합하여 제외.
  대신 원작과 동일하게 **config 의 PAT(`SmartThings_Bearer_Token`)** 를 `api.smartthings.com` 요청에 자동 주입.

### 🐛 한글/멀티바이트 truncation 수정 (가장 중요한 호환성 개선)
원작 `edgebridge.py` 는 응답을 보낼 때 `Content-Length: len(문자열)` 로 헤더를 설정합니다.
Python `len()` 은 **문자 수**를 세지만 실제 전송은 **UTF-8 바이트**로 이루어집니다.
한글 1자 = UTF-8 3바이트이므로 Content-Length 가 실제보다 짧게 보고되고,
허브 Edge 드라이버의 LuaSocket 클라이언트는 그 길이만큼만 읽고 소켓을 닫아 **한글 직후 JSON 이 잘립니다**.
(NEIS 한글 응답에서 약 278바이트 손실 실측됨.)

**edgebridge-aeb** 는 forward 응답을 **업스트림 원본 바이트(`r.content`) 그대로** 전달하고
`Content-Length` 를 **바이트 길이**로 계산하며, 업스트림 `Content-Type` 을 그대로 통과시킵니다.
또한 `Accept-Encoding` 을 제거해 `requests` 가 gzip 을 투명하게 해제하도록 하여, 압축 응답도 안전하게 처리합니다.

---

## 🚀 빠른 시작 — Docker

### docker run
```sh
docker run -d --name edgebridge-aeb \
  -p 8088:8088 \
  -v $(pwd)/data:/data \
  --restart unless-stopped \
  woobooung/edgebridge-aeb:latest
```

### docker compose
저장소의 [`docker-compose.yml`](docker-compose.yml) 사용:
```sh
docker compose up -d
```

> 컨테이너는 데이터(`/data`)에 `.registrations`, `redirects.jsonl`, `callbacks.jsonl`, `mqtt_certs/` 를 영속 저장합니다.
> 설정 파일을 외부화하려면 호스트의 `edgebridge.cfg` 를 `/usr/src/app/edgebridge.cfg` 로 마운트하세요.

---

## 🟦 시놀로지 NAS 설치 (자세히)

> 대부분의 시놀로지(Intel/AMD = amd64, 최신 ARM 모델 = arm64)에서 동작합니다. 멀티아치라 CPU에 맞는 이미지가 자동 선택됩니다.

### 방법 A — Container Manager GUI (권장)
1. **DSM → Container Manager → 레지스트리** 탭에서 `woobooung/edgebridge-aeb` 검색 → **다운로드** → 태그 `latest` 선택.
2. (선택) **File Station** 에서 데이터 폴더를 미리 생성: 예) `/docker/edgebridge-aeb/data`
3. **이미지** 탭 → 받은 이미지 선택 → **실행**.
4. 컨테이너 설정:
   - **컨테이너 이름**: `edgebridge-aeb`
   - **자동 재시작 활성화** 체크
5. **고급 설정 → 포트 설정**: 로컬 포트 `8088` ↔ 컨테이너 포트 `8088` (TCP)
6. **고급 설정 → 볼륨 → 폴더 추가**:
   - 파일/폴더(NAS): `/docker/edgebridge-aeb/data`  →  마운트 경로(컨테이너): `/data`
   - (선택) `edgebridge.cfg` 파일 → 마운트 경로 `/usr/src/app/edgebridge.cfg`
7. **적용 → 완료** → 컨테이너 실행.

### 방법 B — Container Manager 프로젝트 (docker compose)
1. **File Station** 에서 폴더 생성 후 [`docker-compose.yml`](docker-compose.yml) 업로드.
2. **Container Manager → 프로젝트 → 생성** → 경로를 그 폴더로 지정 → 기존 `docker-compose.yml` 사용 → 빌드/실행.

### 동작 확인 (시놀로지)
- Container Manager → 컨테이너 → **로그** 탭에 아래가 보이면 정상:
  ```
  Forwarding Bridge Server v... [edgebridge-aeb]
   > Serving HTTP on <ip>:8088
  ```
- 같은 LAN PC에서 (NAS IP 가 `192.168.1.10` 이라고 가정):
  ```sh
  curl -i http://192.168.1.10:8088/api/ping      # 200 OK
  curl http://192.168.1.10:8088/api/redirect      # []  (빈 목록이 정상)
  ```

---

## 🍓 라즈베리파이 설치 (자세히)

> **Raspberry Pi OS 64-bit** 권장 (Pi 3 / 4 / 5 / Zero 2 W). 공식 이미지는 `linux/arm64`.

### 1) Docker 설치 (한 번만)
```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER     # 로그아웃 후 재로그인하면 sudo 없이 docker 사용
```

### 2) 실행
```sh
mkdir -p ~/edgebridge-aeb/data && cd ~/edgebridge-aeb
docker run -d --name edgebridge-aeb \
  -p 8088:8088 \
  -v $(pwd)/data:/data \
  --restart unless-stopped \
  woobooung/edgebridge-aeb:latest
```

### 3) 동작 확인 (라즈베리파이)
```sh
docker logs edgebridge-aeb            # 'Serving HTTP on ...:8088' 확인
curl -i http://localhost:8088/api/ping
docker ps                            # STATUS 가 Up 이면 정상, 부팅 시 자동 시작됨
```

### 4) 업데이트 / 중지 / 삭제
```sh
docker pull woobooung/edgebridge-aeb:latest   # 최신으로 갱신
docker rm -f edgebridge-aeb && (위 2) 재실행)  # 새 이미지로 재기동
docker stop edgebridge-aeb                     # 중지
```

> **32비트(armv7) Pi** (Pi 1 / 2 / Zero / Zero W) 는 공식 이미지에 없습니다.
> 64-bit OS 로 재설치하거나, 소스에서 직접 빌드하세요
> (`git clone` 후 `docker build -t edgebridge-aeb .`; cryptography 컴파일에 `build-essential libffi-dev cargo` 필요).
> 또는 아래 **venv 직접 실행** 방식을 쓰면 piwheels 가 cryptography 휠을 제공하므로 32비트에서도 동작합니다.

---

## 🔌 Edge 드라이버에서 브리지 사용
드라이버가 호출하는 기준 주소는 `http://<브리지-IP>:8088` 입니다. 예 (NAS/Pi IP = `192.168.1.10`):
```
GET http://192.168.1.10:8088/api/forward?url=https://api.smartthings.com/v1/devices
```
SmartThings API 호출 시 토큰을 자동 주입하려면 `edgebridge.cfg` 의 `SmartThings_Bearer_Token` 에 36자 PAT 를 넣으세요.
MQTT 브리지 연동은 참고 드라이버 [WooBooung/EdgeBridgeBaseDriver](https://github.com/WooBooung/EdgeBridgeBaseDriver) 의 `/mqtt/*` 흐름을 보세요.

---

## 🌐 네트워크 모드 & mDNS 자동 발견 (중요)

브리지의 **일부 기능은 "나에게 연락한 상대(허브)의 진짜 LAN IP"** 를 알아야 합니다:
- **device→hub 포워딩**, **MQTT 메시지 허브 전달**, **mDNS 자동 발견(`_edgebridge._tcp`)**.

그런데 Docker **bridge 네트워크**로 띄우면 들어오는 모든 요청의 출발지가 도커 게이트웨이(`172.17.0.1`)로 보여서
이 기능들이 제대로 동작하지 않습니다. (반면 `/api/forward` 같은 요청·응답형 기능은 **영향 없음**.)

### 권장: host 네트워크로 실행
```sh
docker run -d --name edgebridge-aeb \
  --network host \
  -v $(pwd)/data:/data \
  --restart unless-stopped \
  woobooung/edgebridge-aeb:latest
```
- host 모드는 **포트 매핑(`-p`)이 없습니다.** 브리지는 호스트의 `Server_Port`(기본 **8088**)로 직접 수신 → `http://<호스트-IP>:8088`.
- 8088이 충돌하면 `edgebridge.cfg` 의 `Server_Port` 를 바꾸세요.
- compose 는 [`docker-compose.host.yml`](docker-compose.host.yml) 사용: `docker compose -f docker-compose.host.yml up -d`

#### 시놀로지에서 host 네트워크로 바꾸기
Container Manager 는 **기존 컨테이너의 네트워크를 못 바꿉니다.** 재생성하세요:
1. 컨테이너 **중지 → 삭제** (이미지·데이터 폴더는 유지)
2. 이미지에서 **실행** → **네트워크** 단계에서 **"Docker 호스트와 동일한 네트워크 사용"** 선택
3. **포트 설정은 비워둠**(host 모드는 매핑 없음)
4. 볼륨 `/data` 연결 → 실행 → `http://<NAS-IP>:8088` 로 접속

### mDNS 자동 발견
host(또는 macvlan) 네트워크에서 브리지는 **`_edgebridge._tcp`** 서비스로 자신을 광고합니다
(instance `EdgeBridge-aeb`, TXT `install_id`/`version`). [EdgeBridgeBaseDriver](https://github.com/WooBooung/EdgeBridgeBaseDriver)
등 mDNS 발견을 지원하는 드라이버가 **브리지 IP/포트를 자동으로 잡습니다** — 드라이버에 주소를 수동 입력할 필요가 없습니다.
- 끄려면 `edgebridge.cfg` 의 `mDNS_enabled = no`, 이름은 `mDNS_name` 으로 변경.
- ⚠️ **bridge 네트워크에선 mDNS 멀티캐스트가 LAN 으로 안 나가서 동작하지 않습니다** (서버는 정상, 발견만 안 됨).

> 대안: 분리망이거나 host 모드를 못 쓰면 **macvlan** 으로 브리지에 자체 LAN IP 부여 — [`docker/docker-compose.yml`](docker/docker-compose.yml) 예시.

---

## ⚙️ /api/ping (AEB 호환 상태 JSON)
`GET|POST /api/ping` 은 AEB 와 동일한 형태의 JSON 을 반환합니다 (기존 200 체크 모니터와도 호환):
```jsonc
{
  "battery": 100,                 // 서버는 항상 전원 연결 → 100 고정
  "bridgeDevice": "server",
  "bridgeVersion": "1.x..._AEB",
  "serverStartTime": "06/23 14:30",
  "supportedAiOptions": [],       // LLM 미이식
  "stOauthConnected": false,      // OAuth 미이식 (config PAT 사용)
  "mqtt": { "total": 1, "connected": 1, "sessions": [ {"id":"sess_...","state":"CONNECTED","lastError":null} ] },
  "blocked": { "hosts": 0, "attempts": 0 }
}
```

---

## ⚠️ 운영 주의사항 (코드 버그 아님)
1. **forward 타임아웃 기본 5초** — 느린 외부 API 는 502 가 날 수 있습니다. `edgebridge.cfg` 의 `forwarding_timeout` 을 늘리세요.
2. **네트워크 모드** — 위 "🌐 네트워크 모드" 참고. MQTT/허브 포워딩/mDNS 를 쓰면 **host 네트워크 권장**.

---

## 🐍 Docker 없이 직접 실행 (venv) — 시놀로지/리눅스

> Docker 를 쓰지 않고 Python 으로 바로 돌리는 방법입니다. (가이드 제공: **두더싱 카페 산사나이님**)
> 전제: 시놀로지 패키지 센터 등에서 **Python 3** 가 설치되어 있어야 합니다 (산사나이님 가이드는 3.14 기준, 3.8+ 면 동작).

```sh
# 1) 스크립트가 있는 폴더로 이동 (경로는 본인 환경에 맞게)
cd /volume1/homes/aeb/aeb-bridge

# 2) 가상환경 생성 + 활성화 (프롬프트 앞에 (venv) 가 보이면 성공)
python3 -m venv venv
source venv/bin/activate

# 3) 필수 패키지 설치
pip install paho-mqtt requests cryptography
#   (또는)  pip install -r requirements.txt
```

`pip` 자체가 없다면:
```sh
wget https://bootstrap.pypa.io/get-pip.py
python3 get-pip.py
```

### 실행 / 종료 / 상태
```sh
# 실행 (로그 안 남김)
nohup python3 edgebridge.py > /dev/null 2>&1 &
# 실행 (로그 남김)
nohup python3 edgebridge.py > edgebridge.log 2>&1 &

# 종료
pkill -f edgebridge.py

# 상태 확인
ps -ef | grep edgebridge.py
```

> 데이터 파일(`.registrations`, `redirects.jsonl`, `callbacks.jsonl`, `mqtt_certs/`)은 기본적으로
> 실행 폴더(또는 `edgebridge.cfg` 의 `Data_Dir` / 환경변수 `EB_DATA_DIR`)에 저장됩니다.
> 부팅 시 자동 실행이 필요하면 원작 README 의 systemd 가이드를 참고하세요.

---

## 📡 새로 추가된 API

기준 주소는 `http://<bridge-ip>:8088` 입니다. (아래 예시는 `192.168.1.88`)

### 1) `/api/forward` — DELETE/PATCH 추가 + 인코딩 수정
```
GET|POST|PUT|DELETE|PATCH  http://192.168.1.88:8088/api/forward?url=<URL 문자열>
```
- `api.smartthings.com` 대상이고 호출자가 `Authorization` 을 안 보냈으면 config 의 PAT 를 자동 주입.
- 응답은 업스트림 바이트/Content-Type 그대로 전달 (한글 안전).

### 2) `/api/redirect` — path→URL 영속 매핑 + 자동 프록시
```
POST   /api/redirect?path=/tesla&target=https://owner-api.teslamotors.com/api
DELETE /api/redirect?path=/tesla
GET    /api/redirect                      # 등록 목록(JSON)
```
- 등록 후 브리지로 들어온 `/tesla/...` 요청은 `target + 나머지경로 + 쿼리` 로 **302 리다이렉트**됩니다.
- 매칭은 대소문자 무시 + 최장 prefix 우선.

### 3) `/api/callback` — name 키로 값 저장/조회
```
POST   /api/callback?name=mytoken         # 본문(plain text, 최대 64KB)을 저장
DELETE /api/callback?name=mytoken
GET    /api/callback                      # 전체 목록(JSON)
GET    /api/callback/mytoken              # 단건 값(plain text), 없으면 404
```
- `name` 은 `[A-Za-z0-9_-]+` 만 허용. OAuth 콜백 등 비동기 결과를 드라이버가 폴링하는 용도.

### 4) `/mqtt/*` — MQTT 브리지 (mTLS 구독 → 허브 포워딩)
외부 MQTT 브로커에 mTLS 로 **구독(수신 전용)** 하고, 받은 메시지를 허브 Edge 드라이버로 HTTP 포워딩합니다.
전체 계약은 **[mqtt-bridge-spec-v0.3.md](mqtt-bridge-spec-v0.3.md)** 참고.

| Method | Path | 역할 |
|--------|------|------|
| POST | `/mqtt/sessions` | 세션 + RSA2048 키쌍 + CSR(PEM) 생성 (개인키는 절대 반환 안 함) |
| POST | `/mqtt/sessions/{id}/connect` | mTLS 연결 + 토픽 구독 |
| PUT | `/mqtt/sessions/{id}/forward` | 허브 포워딩 타깃 등록(멱등) |
| GET | `/mqtt/sessions/{id}/status` | 상태/진단 조회 |
| GET | `/mqtt/sessions/{id}/messages?since=` | 버퍼된 메시지 폴링(링버퍼 200) |
| DELETE | `/mqtt/sessions/{id}` | 세션 종료 + 키/인증서 삭제 |

참고 Edge 드라이버: [WooBooung/EdgeBridgeBaseDriver](https://github.com/WooBooung/EdgeBridgeBaseDriver)

---

## ⚙️ 설정 파일 (`edgebridge.cfg`)
```ini
[config]
Server_IP =                       # 비우면 자동 감지
Server_Port = 8088
SmartThings_Bearer_Token =        # 선택: 36자 PAT (api.smartthings.com 자동 주입)
forwarding_timeout = 5
console_output = yes
logfile_output = no
logfile = edgebridge.log
Data_Dir =                        # 비우면 현재 디렉터리. Docker 는 EB_DATA_DIR=/data 사용
```
환경변수 `EB_DATA_DIR` 가 `Data_Dir` 보다 우선합니다.

---

## 🛠️ 직접 빌드/배포하기 (GitHub Actions → Docker Hub)

이 포크는 **`main` 브랜치에 push 하면 GitHub Actions 가 멀티아치 이미지를 빌드해 Docker Hub 로 자동 배포**합니다
([`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml)).

### 최초 1회 설정 (Docker Hub & Secrets)
1. <https://hub.docker.com> 가입 후, 리포지토리 `edgebridge-aeb` 생성 (push 시 자동 생성도 됨).
2. **Account Settings → Security → New Access Token** 에서 **Read/Write** 토큰 발급.
3. GitHub 저장소 → **Settings → Secrets and variables → Actions** 에 등록:
   - `DOCKERHUB_USERNAME` = Docker Hub 사용자명 (예: `woobooung`)
   - `DOCKERHUB_TOKEN` = 위에서 발급한 액세스 토큰
4. `main` 에 commit & push → **Actions** 탭에서 빌드 확인.
5. 버전 릴리스는 `git tag v1.0.0 && git push --tags` → `:1.0.0`, `:1.0`, `:latest` 태그가 함께 발행됩니다.

### 멀티아치 확인
```sh
docker buildx imagetools inspect woobooung/edgebridge-aeb:latest
# linux/amd64, linux/arm64 매니페스트가 보이면 정상
```

---

## 원작 문서

원작 edgebridge 의 사용 사례, 동반 Edge 드라이버 목록, systemd 자동 실행, 모니터링 드라이버 등
일반적인 사용법은 원작 README 를 참고하세요: <https://github.com/toddaustin07/edgebridge#readme>

## 라이선스
원작과 동일하게 **Apache License 2.0** 을 따릅니다. © Todd Austin / contributors.
