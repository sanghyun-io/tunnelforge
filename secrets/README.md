# Secrets Directory

이 디렉토리는 GitHub App Private Key와 같은 민감한 인증 정보를 저장합니다.

## 파일 목록

- `github-app-private-key.pem`: GitHub App Private Key (`.gitignore`에 의해 버전 관리 제외)
- `github-app-private-key.pem.example`: Private Key 템플릿 파일

## Private Key 설정 방법

1. GitHub App 설정 페이지에서 Private Key 생성
2. 다운로드된 `.pem` 파일을 이 디렉토리에 복사:
   ```bash
   cp ~/Downloads/your-app-name.2024-01-27.private-key.pem secrets/github-app-private-key.pem
   ```

3. `.env` 파일에서 경로 설정:
   ```bash
   GITHUB_APP_PRIVATE_KEY=secrets/github-app-private-key.pem
   ```

## 보안 주의사항

- **절대로** Private Key 파일을 Git에 커밋하지 마세요
- Private Key는 팀원과 공유하지 마세요 (각자 별도 GitHub App 사용 권장)
- `.pem` 파일은 `.gitignore`에 의해 자동으로 제외됩니다
- 파일 권한 설정 (Linux/macOS):
  ```bash
  chmod 600 secrets/github-app-private-key.pem
  ```

## Troubleshooting

### "Private Key 파일을 찾을 수 없습니다"

1. 파일이 올바른 위치에 있는지 확인:
   ```
   tunnel-manager/
   └── secrets/
       └── github-app-private-key.pem
   ```

2. `.env` 파일의 경로가 올바른지 확인

### "Permission denied"

Linux/macOS에서 파일 권한을 확인하세요:
```bash
ls -l secrets/github-app-private-key.pem
chmod 600 secrets/github-app-private-key.pem
```
