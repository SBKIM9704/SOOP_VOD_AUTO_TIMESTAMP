---
name: commit
description: Stage all changes and create a git commit. Use when asked to "commit", "커밋해줘", or "/commit <메시지>".
allowed-tools: Bash
metadata:
  argument-hint: "<커밋 메시지>"
---

# Git Commit

변경사항을 논리적 단위로 분석하여 **여러 커밋으로 나눠** 생성한다.

## Steps

1. **브랜치 확인**
   - `git branch --show-current` 로 현재 브랜치 확인
   - 현재 브랜치가 `develop` 또는 `main`이면 **커밋을 진행하지 않고** feature 브랜치 생성 여부를 사용자에게 먼저 확인한다.
     - 사용자가 생성을 원하면 `/branch` 흐름(또는 `git checkout -b <브랜치명>`)으로 feature 브랜치를 만든 뒤 커밋을 이어간다.
     - 프로젝트는 feature 브랜치 → `/pr`로 develop 병합 흐름이므로 공유 브랜치(`develop`/`main`)에 직접 커밋하지 않는다.
   - feature 브랜치라면 바로 다음 단계로 진행한다.

2. **현재 상태 확인**
   - `git status --short` 및 `git diff` 실행
   - 변경된 파일 목록과 diff 내용 파악

3. **논리적 그룹 분석**
   - 변경 파일들을 아래 기준으로 논리적 그룹으로 묶는다
   - `$ARGUMENTS`가 있으면 해당 메시지로 단일 커밋, 없으면 그룹 분리를 우선한다
   - 그룹 계획을 먼저 사용자에게 제시하고 진행한다

4. **그룹별 순차 커밋**
   - 각 그룹에 대해 아래를 반복한다:
     1. `git add <파일1> <파일2> ...` 로 해당 그룹 파일만 스테이징
     2. Conventional Commits 형식으로 커밋 메시지 작성
     3. `git commit -m "<message>"`

5. **결과 확인**
   - `git log --oneline -<커밋 수>` 로 생성된 커밋 전체 표시

## 그룹 분리 기준

다음 중 하나라도 해당하면 별도 커밋으로 분리한다:

| 기준 | 예시 |
|------|------|
| **레이어가 다름** | 신규 훅/컴포넌트 vs 이를 적용한 페이지 |
| **피처가 다름** | stt 변경 vs cer 변경 vs admin 변경 |
| **목적이 다름** | 유틸 함수 정리 vs 기능 추가 |
| **신규 파일 vs 기존 수정** | 새 공유 모듈 생성 vs 해당 모듈을 사용하도록 리팩토링 |
| **공유 코드 vs 기능 코드** | shared/, hooks/, components/ui/ vs features/ |

단, 변경 파일이 2개 이하이거나 변경 규모가 매우 작으면 단일 커밋도 허용한다.

## Commit Message 형식

```
type(scope): 한글 또는 영문 설명

타입 목록:
  feat     새 기능 추가
  fix      버그 수정
  refactor 기능 변경 없는 코드 개선
  style    포맷, 공백 등 스타일 변경
  docs     문서 수정 (README, CLAUDE.md 등)
  chore    빌드, 설정, 패키지 변경
  test     테스트 추가·수정

scope (선택): auth | dashboard | stt | cer | admin | keywords | logs | shared | hooks | ui

예시:
  feat(hooks): usePaginatedList 훅 추가
  refactor(shared): 중복 유틸 제거 및 date 유틸 통합
  refactor(cer): WavePlayer/cerUtils 공유 모듈로 분리
  refactor(pages): usePaginatedList 훅으로 페이지네이션 상태 통일
  fix(auth): 로그아웃 CSRF 오류 수정
  chore: xlsx 미사용 패키지 제거
```

## 주의사항

- 스테이징할 변경사항이 없으면 커밋 없이 현재 상태를 알려준다
- `git add -A` 는 사용하지 않는다. 항상 파일을 명시적으로 지정한다
- `.env*` 등 민감 파일이 포함되어 있으면 커밋 전에 사용자에게 알린다
