# GitHub App 설정 가이드

DataFlare Tunnel Manager의 자동 이슈 보고 기능은 GitHub App을 통해 인증합니다.

## 왜 GitHub App인가?

| 항목 | Personal Access Token | GitHub App |
|------|----------------------|------------|
| 권한 | 사용자 전체 권한 | 앱에 부여된 권한만 (이슈 생성만 가능) |
| 만료 | 수동 설정 필요 | Installation Token 1시간 자동 만료 |
| 추적 | 사용자 이름으로 표시 | 앱 이름으로 표시 (봇임이 명확) |
| 보안 | 토큰 유출 시 위험 | Private Key + 짧은 토큰 수명으로 안전 |

---

## 1단계: GitHub App 생성

1. GitHub에서 **Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**

2. 기본 정보 입력:
   - **GitHub App name**: `DataFlare Issue Reporter` (원하는 이름)
   - **Homepage URL**: `https://github.com/your-org/your-repo`
   - **Webhook**: ☐ Active (체크 해제)

3. 권한 설정 (Permissions):
   - **Repository permissions**:
     - **Issues**: `Read and write`
   - 다른 권한은 모두 `No access`

4. 설치 범위:
   - **Where can this GitHub App be installed?**: `Only on this account`

5. **Create GitHub App** 클릭

---

## 2단계: App ID 확인

App 생성 후 설정 페이지에서 **App ID**를 확인합니다.

```
App ID: 123456  ← 이 숫자를 메모
```

---

## 3단계: Private Key 생성

1. App 설정 페이지 하단의 **Private keys** 섹션
2. **Generate a private key** 클릭
3. `.pem` 파일이 자동 다운로드됨
4. 이 파일을 안전한 위치에 보관

```bash
# 예: ~/.dataflare/github-app-private-key.pem
```

---

## 4단계: App 설치

1. App 설정 페이지 → 좌측 메뉴 **Install App**
2. 이슈를 생성할 리포지토리 선택
3. **Install** 클릭
4. 설치 후 URL에서 **Installation ID** 확인:

```
https://github.com/settings/installations/12345678
                                           ^^^^^^^^
                                           Installation ID
```

---

## 5단계: 환경변수 설정

### Linux/macOS

```bash
# ~/.bashrc 또는 ~/.zshrc에 추가
export DATAFLARE_GITHUB_APP_ID="123456"
export DATAFLARE_GITHUB_APP_PRIVATE_KEY="/path/to/private-key.pem"
export DATAFLARE_GITHUB_APP_INSTALLATION_ID="12345678"
export DATAFLARE_GITHUB_REPO="your-org/your-repo"
```

### Windows (PowerShell)

```powershell
# 시스템 환경변수로 설정
[Environment]::SetEnvironmentVariable("DATAFLARE_GITHUB_APP_ID", "123456", "User")
[Environment]::SetEnvironmentVariable("DATAFLARE_GITHUB_APP_PRIVATE_KEY", "C:\path\to\private-key.pem", "User")
[Environment]::SetEnvironmentVariable("DATAFLARE_GITHUB_APP_INSTALLATION_ID", "12345678", "User")
[Environment]::SetEnvironmentVariable("DATAFLARE_GITHUB_REPO", "your-org/your-repo", "User")
```

### Windows (CMD)

```cmd
setx DATAFLARE_GITHUB_APP_ID "123456"
setx DATAFLARE_GITHUB_APP_PRIVATE_KEY "C:\path\to\private-key.pem"
setx DATAFLARE_GITHUB_APP_INSTALLATION_ID "12345678"
setx DATAFLARE_GITHUB_REPO "your-org/your-repo"
```

---

## 6단계: 확인

환경변수 설정 후 앱을 재시작하면 설정 → GitHub 이슈 자동 보고에서:

```
✅ GitHub App이 설정되어 있습니다.
```

가 표시됩니다.

---

## .exe 빌드 시 내장 설정

배포용 `.exe` 파일에 인증 정보를 내장하려면:

```python
from src.core.github_app_auth import GitHubAppAuth

# Private Key 내용 읽기
with open('private-key.pem', 'r') as f:
    private_key = f.read()

# 난독화된 코드 생성
code = GitHubAppAuth.generate_embedded_code(
    app_id="123456",
    private_key=private_key,
    installation_id="12345678",
    repo="your-org/your-repo"
)
print(code)
```

출력된 코드를 `src/core/github_app_auth.py`의 해당 변수에 삽입합니다.

---

## 환경변수 요약

| 환경변수 | 설명 | 예시 |
|---------|------|-----|
| `DATAFLARE_GITHUB_APP_ID` | GitHub App ID | `123456` |
| `DATAFLARE_GITHUB_APP_PRIVATE_KEY` | Private Key 파일 경로 또는 PEM 내용 | `/path/to/key.pem` |
| `DATAFLARE_GITHUB_APP_INSTALLATION_ID` | App Installation ID | `12345678` |
| `DATAFLARE_GITHUB_REPO` | 이슈를 생성할 리포지토리 | `owner/repo` |

---

## 문제 해결

### "GitHub App이 설정되지 않았습니다"

1. 환경변수가 올바르게 설정되었는지 확인
2. Private Key 파일 경로가 올바른지 확인
3. 앱 재시작

### "Installation Token 발급 실패"

1. App ID, Installation ID가 올바른지 확인
2. Private Key가 해당 App의 것인지 확인
3. App이 해당 리포지토리에 설치되었는지 확인

### "이슈 생성 권한 없음"

1. App의 Repository permissions에서 Issues가 `Read and write`인지 확인
2. App이 해당 리포지토리에 설치되었는지 확인
