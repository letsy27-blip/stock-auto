# PC가 꺼져도 시장 데이터 수집하기

`.github/workflows/market-collector.yml`은 GitHub Actions에서 평일 한국 시간 09:07·09:37부터 15:07·15:37까지 `main.py --once`를 실행합니다. 실행이 끝나면 변경된 `stock_data.db`를 저장소에 자동 커밋합니다.

## GitHub Secrets 등록

GitHub 저장소에서 **Settings → Secrets and variables → Actions → New repository secret**을 열어 아래 값을 등록합니다. 값은 현재 PC의 `.env`에서 복사합니다. `.env` 파일 자체는 올리지 않습니다.

- `KIS_APP_KEY`
- `KIS_APP_SECRET`
- `KIS_BASE_URL`
- `KIS_ACCOUNT_NO`
- `KIS_ACCOUNT_CODE`
- `GEMINI_API_KEY`

`KIS_BASE_URL`은 모의투자라면 모의투자 주소, 실전이라면 실전 주소를 현재 `.env`와 동일하게 넣습니다.

## 최초 실행

1. 이 변경을 GitHub에 push합니다.
2. GitHub의 **Actions → Korean market collector → Run workflow**로 한 번 수동 실행합니다.
3. 실행 로그에서 수집이 완료됐는지 확인합니다.
4. 집·회사 PC에서는 대시보드를 열기 전 `git pull`로 최신 `stock_data.db`를 받습니다.

## 주의

- GitHub Actions는 예약 시각이 약간 지연될 수 있어 정각 대신 07분·37분으로 예약했습니다.
- Actions 실행 중에 PC에서도 같은 DB를 변경·push하면 SQLite 파일 충돌이 날 수 있습니다. 자동 수집은 GitHub Actions 한 곳만 사용하고, PC에서는 조회·개발 위주로 사용하세요.
- 이 워크플로는 주문을 내지 않고 데이터 수집·분석만 실행합니다.
