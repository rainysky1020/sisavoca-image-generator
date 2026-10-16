# 시사보카 연상 삽화 제작기

단어 CSV를 읽어 학습에 유익한 삽화만 제작하는 로컬 웹 프로그램입니다. `gpt-6-sol`이 먼저 제작 여부를 판단하고, `gpt-image-2`의 `quality=low`가 [다람쥐 캐릭터 시트](./다람쥐%20캐릭터%20시트.jpg)를 참고해 1024×1024 PNG를 만듭니다. 생성 후에는 뜻을 기억하는 데 쓸 만한 이미지를 우선 살립니다. 작은 색감·그라데이션 차이는 허용하고, 뜻 오류·원치 않는 다람쥐·손가락 또는 발가락 등 중요한 문제만 한 번 수정한 뒤 재검토합니다.

## 실행

Python 3.11 이상에서 추가 패키지 없이 실행할 수 있습니다.

```powershell
python app.py
```

브라우저에서 `http://127.0.0.1:8765`를 엽니다. `.env`에 `OPENAI_API_KEY=...`가 있어야 합니다. 운영체제 환경 변수로 제공해도 됩니다. 키는 로컬 서버에서만 읽습니다. 포트를 바꾸려면 `SISAVOCA_PORT` 환경 변수를 설정합니다.

## 입력 CSV

UTF-8 또는 UTF-8 BOM CSV를 사용합니다. [sample_vocabulary.csv](./sample_vocabulary.csv)를 복사해 시작할 수 있습니다.

| 열 | 필수 | 내용 |
| --- | --- | --- |
| `word` | 예 | 표제어 |
| `meaning_ko` | 예 | 삽화가 표현해야 할 한국어 뜻 |

파일은 최대 5MB, 1,000행입니다. 필수 값 누락·행 형식 오류는 해당 행을 `error`로 기록합니다. 언어는 원어 단어를 바탕으로 모델이 판단합니다. 같은 표제어가 여러 행에 있어도 각각 처리합니다. 작업을 시작한 뒤에는 현재 처리 중인 API 요청이 끝난 다음 중단됩니다. 재개 시 완료된 행은 다시 생성하지 않습니다. 처리 도중 끊긴 행은 처음부터 다시 처리할 수 있어 그 행의 API 호출이 중복될 수 있습니다.

## 결과

각 작업 결과는 `output/<작업 ID>/`에 저장됩니다. PNG 파일명은 원어 단어를 최대한 그대로 사용합니다(예: `umbrella.png`, `驚く.png`). 같은 이름이 다시 나오면 `umbrella (2).png`처럼 번호를 붙이며, 파일명에 사용할 수 없는 문자만 `_`로 바꿉니다. 웹 화면에서 납품 PNG를 개별 또는 ZIP 묶음으로 받고, `work_report.xlsx`를 내려받을 수 있습니다. 보고서의 `요약` 시트는 건수·모델 설정·성공한 API 호출 수·반환된 토큰 사용량을 담고, `단어별 상세` 시트는 모든 입력 행의 판단·검토·수정·납품 결과를 담습니다. 금액은 API 응답만으로 정확한 과금이 확인되지 않아 계산하지 않습니다.

자동 검토는 의미 전달과 기억 단서를 우선합니다. 화풍의 경미한 차이는 보고서에 참고 사항으로 남기고 납품할 수 있습니다. 생성 모델의 결과를 절대 보증하지는 않으므로 출판 전 편집자가 납품 이미지를 확인하는 절차를 권장합니다.

## 검증

```powershell
python -m unittest discover -s tests -v
```

이 테스트는 실제 API 비용 없이 모의 응답으로 제작 생략, 생성, 1회 수정, 폐기, 엑셀 건수 일치를 확인합니다.

## GitHub로 공유

출판사 내부 공유에는 비공개 저장소를 권장합니다. `.env`, `voca*.csv`, `voca*.xlsx`, `output/`은 Git에서 제외됩니다. 코드를 받은 사람은 `.env.example`을 `.env`로 복사하고 **자신의** API 키를 입력해야 합니다. 캐릭터 시트는 출판사가 사용 권한을 확인한 사람에게만 공유하세요.

이 폴더는 처음에는 Git 저장소가 아닙니다. GitHub 로그인 후 다음처럼 코드와 문서, 캐릭터 시트를 명시적으로 선택하여 올릴 수 있습니다.

```powershell
gh auth login
git init -b main
git add .gitignore .env.example README.md PRD.md requirements.txt app.py pipeline.py report_xlsx.py sample_vocabulary.csv static/index.html tests/test_http.py tests/test_workflow.py "다람쥐 캐릭터 시트.jpg"
git diff --cached --name-only
git commit -m "Add Sisavoca image generator"
gh repo create sisavoca-image-generator --private --source=. --remote=origin --push
```

저장소 링크를 동료에게 전달하고 개인 저장소라면 GitHub의 **Settings → Collaborators → Add people**에서 접근 권한을 부여합니다. 동료는 저장소를 복제한 뒤 `.env.example`을 `.env`로 복사하고 `python app.py`를 실행합니다. 저장소를 공개하기 전에는 캐릭터 시트와 PRD·코드·예시 데이터의 외부 공개 가능 여부를 확인하세요.

## OpenAI API 근거

- [GPT-6 Sol 모델](https://developers.openai.com/api/docs/models/gpt-6-sol)
- [이미지 편집 API](https://developers.openai.com/api/reference/resources/images/methods/edit)
- [구조화된 출력](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
