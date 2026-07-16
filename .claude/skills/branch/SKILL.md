---
name: branch
description: develop 최신화 후 새 feature 브랜치를 생성. "브랜치 만들어줘", "새 브랜치", "/branch <name>" 호출.
allowed-tools: Bash
metadata:
  argument-hint: "<branch-name>"
---

# Branch 생성 스킬

Linear에서 복사한 브랜치명으로 새 브랜치를 생성합니다.

## 인자

사용자가 입력한 브랜치명: $ARGUMENTS

## 실행 순서

1. **인자 검증**: `$ARGUMENTS`가 비어 있으면 사용법을 안내하고 종료합니다.
2. **develop 체크아웃**: `git checkout develop`
3. **develop 최신화**: `git pull origin develop`
4. **새 브랜치 생성**: `git checkout -b $ARGUMENTS`
5. 결과를 간결하게 보고합니다.

## 규칙

- 인자가 없으면 실행하지 말고 사용 예시를 보여주세요:
  ```
  /branch feature/PV-38-이슈명
  ```
- 각 git 명령 실패 시 에러 메시지를 그대로 출력하고 이후 단계를 건너뜁니다.
- 성공 시 `git branch --show-current`로 현재 브랜치를 확인하고 출력합니다.
