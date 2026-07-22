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
git fetch origin --tags        # 원격 main·태그 최신화 (로컬 main은 stale일 수 있음)
git log origin/main..develop --oneline
```

- **반드시 로컬 `main`이 아니라 `origin/main` 기준으로 비교한다.** 로컬 `main`은 오래돼 divergent일
  수 있어(`main..develop`가 이미 릴리즈된 커밋까지 잡음), 잘못된 CHANGELOG·태그 위치로 이어진다.
- 커밋이 없으면 "릴리즈할 변경사항이 없습니다." 안내 후 종료한다.

#### 1-2. 현재 최신 태그 확인

```bash
git tag -l "v*" | sort -V | tail -1
```

- 태그가 없으면 기준 버전을 `0.0.0`으로 간주한다.

#### 1-3. 버전 자동 추천 (인자가 없을 때)

`$ARGUMENTS`가 비어 있으면, `git log origin/main..develop --oneline` 출력의 커밋 타입을 분석하여 다음 규칙으로 버전을 추천한다:

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

이 저장소는 **Python 패키지**다. 버전은 `pyproject.toml`의 `version = "X.Y.Z"` 한 곳에만 있다
(package.json·package-lock.json 없음, npm 사용 금지). Edit 도구로 그 줄만 교체한다:

```
version = "<이전>"   →   version = "$VERSION"
```

- 확인: `grep -m1 '^version' pyproject.toml` 이 `version = "$VERSION"` 인지.
- git tag는 여기서 만들지 않는다 — main merge 완료 후 8단계에서 생성한다.

### 3. CHANGELOG.md 업데이트

`git log origin/main..develop --oneline`의 출력을 파싱하여 아래 형식으로 CHANGELOG.md 상단에 prepend한다:

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
git add pyproject.toml CHANGELOG.md
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

### 8. 완료 처리 (태그 생성)

**merge 직후 원격을 다시 fetch하고, 태그는 `origin/main`(병합된 커밋)에 직접 건다.**
`git checkout main && git pull` 방식은 금지 — 로컬 `main`이 divergent이면 pull이 실패하고
태그가 **stale한 로컬 main의 옛 커밋에 잘못 찍힌다**(실제로 겪은 버그).

```bash
git fetch origin
git log origin/main --oneline -1     # 병합 커밋(release: v$VERSION merge)인지 확인
```

태그가 이미 존재하는지 확인한다:

```bash
git tag -l "v$VERSION"
```

- 비어 있으면 **`origin/main`을 가리키도록** 태그를 생성하고 push한다:
  ```bash
  git tag v$VERSION origin/main
  git push origin v$VERSION
  ```
- 이미 존재하면, 그 태그가 `origin/main`을 가리키는지 확인한다
  (`git rev-list -n1 v$VERSION` == `git rev-parse origin/main`). 다른 커밋을 가리키면 잘못
  찍힌 것이므로 삭제 후 재생성한다:
  ```bash
  git push origin :refs/tags/v$VERSION && git tag -d v$VERSION
  git tag v$VERSION origin/main && git push origin v$VERSION
  ```

로컬 `main`을 원격에 안전하게 맞춘다(체크아웃/merge 없이, divergent여도 안전):

```bash
git branch -f main origin/main
```

- 검증: `git show main:pyproject.toml | grep -m1 '^version'` 이 `version = "$VERSION"` 인지,
  `git rev-list -n1 v$VERSION`가 `origin/main` HEAD와 같은지 확인한다.
- 최종적으로 PR URL, 버전(`v$VERSION`), 태그 커밋 해시를 출력한다. 현재 브랜치는 `develop` 유지.
