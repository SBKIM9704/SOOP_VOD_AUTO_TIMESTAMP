---
name: release
description: develop → main 릴리즈 PR 생성 및 merge (버전 bump·CHANGELOG·태그). "/release [version]" 호출.
allowed-tools: Bash, Read, Write, Edit
metadata:
  argument-hint: "[version]"
---

# Release 스킬

`develop` 브랜치를 `main`에 merge하여 릴리즈합니다.

## 인자

버전 번호: `$ARGUMENTS` (선택 사항. 생략하면 자동 추천)


## 실행 순서

### 1. 버전 결정

#### 1-1. develop 최신화 및 커밋 목록 수집

```bash
git checkout develop
git pull origin develop
git log main..develop --oneline
```

- 커밋이 없으면 "릴리즈할 변경사항이 없습니다." 안내 후 종료한다.

#### 1-2. 현재 최신 태그 확인

```bash
git tag -l "v*" | sort -V | tail -1
```

- 태그가 없으면 기준 버전을 `0.0.0`으로 간주한다.

#### 1-3. 버전 자동 추천 (인자가 없을 때)

`$ARGUMENTS`가 비어 있으면, `git log main..develop --oneline` 출력의 커밋 타입을 분석하여 다음 규칙으로 버전을 추천한다:

| 조건 | 범프 종류 | 예시 |
|------|-----------|------|
| `BREAKING CHANGE` 또는 `!:` 포함 커밋 존재 | **major** | 1.0.0 → 2.0.0 |
| `feat` 커밋 존재 (breaking 없음) | **minor** | 1.0.0 → 1.1.0 |
| `fix`, `chore`, `docs` 등만 존재 | **patch** | 1.0.0 → 1.0.1 |

추천 버전을 사용자에게 출력하고 확인을 받는다:

```
추천 버전: vX.Y.Z (minor bump — feat 커밋 포함)
이 버전으로 릴리즈할까요? [y/N]
```

- 사용자가 `y`이면 해당 버전으로 진행한다.
- 사용자가 다른 버전을 입력하면 그 버전을 사용한다.
- `N` 또는 취소이면 종료한다.

#### 1-4. 버전 유효성 검사 (인자가 있을 때)

`$ARGUMENTS`가 있으면:
- semver 형식(`X.Y.Z`)이 아니면 오류를 안내하고 종료한다.
- 최신 태그 이하이면 "오류: v$ARGUMENTS는 최신 태그(vX.Y.Z) 이하입니다." 안내 후 종료한다.


이하 단계에서 `$VERSION`은 위에서 결정된 최종 버전 문자열을 의미한다.

### 2. 버전 bump

```bash
npm version $VERSION --no-git-tag-version
```

- `package.json`과 `package-lock.json`이 동시에 업데이트된다.
- `--no-git-tag-version`: git tag는 main merge 완료 후 생성한다.

### 3. CHANGELOG.md 업데이트

`git log main..develop --oneline`의 출력을 파싱하여 아래 형식으로 CHANGELOG.md 상단에 prepend한다:

```markdown
## [v$VERSION] - YYYY-MM-DD

### Added
- feat(scope): 설명 (커밋해시)

### Fixed
- fix(scope): 설명 (커밋해시)

### Changed
- refactor/chore/style/docs: 설명 (커밋해시)
```

- `feat` 커밋 → `### Added`
- `fix` 커밋 → `### Fixed`
- `refactor`, `chore`, `style`, `docs`, `test` 커밋 → `### Changed`
- 분류되지 않은 커밋도 `### Changed`에 포함한다.
- 해당 섹션에 커밋이 없으면 그 섹션을 생략한다.
- CHANGELOG.md가 없으면 새로 생성한다.

### 4. 버전 bump 커밋

```bash
git add package.json package-lock.json CHANGELOG.md
git commit -m "chore: release v$VERSION"
git push origin develop
```

### 5. PR 생성

```bash
gh pr list --base main --json number,url,title
```

- 이미 PR이 존재하면 생성을 건너뛰고 다음 단계로 진행한다.
- 없으면 생성한다:

```bash
gh pr create --base main --head develop --title "release: v$VERSION" --body "..."
```

본문에는 CHANGELOG의 해당 버전 섹션 내용을 포함한다.

### 6. Mergeable 체크 (최대 5회, 3초 간격)

```bash
gh pr view <PR번호> --json mergeable,mergeStateStatus,number,url
```

- `UNKNOWN`이면 3초 대기 후 재시도한다.
- 5회 후에도 `UNKNOWN`이면 PR URL을 안내하고 종료한다.

### 7. 분기 처리

**CONFLICTING인 경우:**

- 충돌 파일 목록을 출력한다.
- 아래 해결 가이드를 안내하고 종료한다:
  ```
  1. git fetch origin
  2. git merge origin/main
  3. 충돌 파일 수동 해결 후 git add .
  4. git commit
  5. git push origin develop
  6. 이후 다시 /release 실행
  ```

**MERGEABLE인 경우:**

```bash
gh pr merge <PR번호> --merge --delete-branch=false
```

- **merge commit** 방식 사용 (develop 커밋 이력을 main에 보존)
- develop 브랜치는 삭제하지 않는다.

### 8. 완료 처리

```bash
git checkout main
git pull origin main
```

태그가 이미 존재하는지 확인한다:

```bash
git tag -l "v$VERSION"
```

- 비어 있으면 태그를 생성하고 push한다:
  ```bash
  git tag v$VERSION
  git push origin v$VERSION
  ```
- 이미 존재하면 "태그 v$VERSION 이미 존재, 건너뜀" 안내만 출력한다.

```bash
git checkout develop
```

- 최종적으로 PR URL, 버전(`v$VERSION`), 태그 상태를 출력한다.
