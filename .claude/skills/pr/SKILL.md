---
name: pr
description: 현재 브랜치로 develop PR 생성 → mergeable 확인 → squash merge. "/pr [제목]" 호출.
allowed-tools: Bash
metadata:
  argument-hint: "[PR 제목]"
---

# PR 생성 및 Merge 스킬

현재 브랜치를 기준으로 PR을 생성하고 merge까지 진행한다.

## Steps

### 1. 사전 확인

```bash
git branch --show-current   # 현재 브랜치 확인
git log develop..HEAD --oneline  # develop 이후 커밋 목록
```

- 현재 브랜치가 `develop` 또는 `main`이면 중단하고 안내한다.
- 커밋이 없으면 중단한다.

### 2. PR 생성

```bash
gh pr create --base develop --title "<제목>" --body "<본문>"
```

- `$ARGUMENTS`가 있으면 그대로 제목으로 사용한다.
- 없으면 `git log develop..HEAD --oneline`을 읽어 제목을 자동 생성한다.
  - 커밋이 1개: 그 커밋 메시지를 제목으로 사용
  - 커밋이 여러 개: 가장 대표적인 내용을 요약해 제목 생성
- 본문에는 커밋 목록을 bullet list로 포함한다.
- PR이 이미 존재하면(`gh pr view` 성공) 생성을 건너뛰고 다음 단계로 진행한다.

### 3. Mergeable 체크 (최대 5회 재시도, 3초 간격)

```bash
gh pr view --json mergeable,mergeStateStatus,number,url
```

- `UNKNOWN`이면 3초 대기 후 재시도한다.
- 5회 후에도 `UNKNOWN`이면 PR URL을 안내하고 종료한다.

### 4. 분기 처리

**CONFLICTING인 경우:**

```bash
gh pr view --json files
```

- 충돌 파일 목록을 출력한다.
- 아래 해결 가이드를 안내하고 종료한다:
  ```
  1. git fetch origin
  2. git merge origin/develop
  3. 충돌 파일 수동 해결 후 git add .
  4. git commit
  5. git push
  6. 이후 다시 /pr 실행
  ```

**MERGEABLE인 경우:**

```bash
gh pr merge <PR번호> --squash --delete-branch
```

- squash merge로 진행한다.
- merge 완료 후:
  ```bash
  git checkout develop
  git pull origin develop
  ```
- 최종적으로 merge된 PR URL과 커밋을 출력한다.
