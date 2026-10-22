import os
import json
from io import BytesIO
from datetime import datetime, timezone, timedelta
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import networkx as nx
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from supabase import create_client, Client

# 한국 시간(KST) 설정
KST = timezone(timedelta(hours=9))

st.set_page_config(
    page_title="우리 반 마음 & 교우관계 통합 시스템",
    page_icon="🏫",
    layout="wide"
)

# ---------------------------------------------------------
# 1. 환경변수 및 Secrets 로드 (Supabase / PIN / Gemini)
# ---------------------------------------------------------
def get_secret(key, default=""):
    if key in st.secrets:
        return st.secrets[key]
    return os.environ.get(key, default)

SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_KEY = get_secret("SUPABASE_KEY")
TEACHER_PIN = str(get_secret("TEACHER_PIN", "1234")) # 기본 PIN: 1234
GEMINI_API_KEY = get_secret("GEMINI_API_KEY")

@st.cache_resource
def init_supabase() -> Client | None:
    if SUPABASE_URL and SUPABASE_KEY:
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    return None

supabase = init_supabase()

# ---------------------------------------------------------
# 2. Gemini Vision OCR 스키마 (설문지 3종 통합)
# ---------------------------------------------------------
class SurveyExtraction(BaseModel):
    이름: str = Field(description="학생 이름 (적혀있지 않으면 '미기재')") #[cite: 1, 2, 3]
    마음날씨: str = Field(description="'아주 맑음', '구름 조금', '비 옴', '천둥 번개' 중 하나") #[cite: 3]
    현재기분: str = Field(description="'기분이 좋아요', '그냥 보통이에요', '조금 지쳐요', '속상해요', '화가 나요' 중 하나") #[cite: 1]
    깊이있는마음: str = Field(description="'평소처럼 괜찮고, 별로 힘든 게 없어요.', '가끔씩 짜증이 나거나 기분이 좀 가라앉아요.', '자주 짜증이 나고, 마음이 불안해요.', '많이 힘들고, 울고 싶을 때가 있어요.', '기분이 계속 안 좋고, 아무것도 하기 싫어요.' 중 하나") #[cite: 1]
    주요고민: list[str] = Field(description="체크된 고민 목록") #[cite: 1, 3]
    짜증순간: list[str] = Field(description="체크된 짜증나는 순간 목록") #[cite: 1]
    필요한것: list[str] = Field(description="체크된 필요한 것 목록") #[cite: 1]
    친한친구: str = Field(description="가장 친한/편안한 친구 이름들 (쉼표로 구분)") #[cite: 2, 3]
    자주노는친구: str = Field(description="쉬는 시간이나 놀이 시간에 자주 함께 노는 친구 이름들 (쉼표로 구분)") #[cite: 2, 3]
    친해지고싶은친구: str = Field(description="앞으로 더 친해지고 싶은 친구 이름들 (쉼표로 구분)") #[cite: 2, 3]
    멀리있는친구: str = Field(description="조금 멀리 있는/어려운 친구 이름들 (쉼표로 구분)") #[cite: 2, 3]
    관계어려움: list[str] = Field(description="친구 관계에서 어려웠던 경험 체크 항목들") #[cite: 2]
    점수_즐거움: int = Field(description="친구들과 함께 노는 것이 즐겁고 편안하다 점수 (1~5)") #[cite: 3]
    점수_힘든친구: int = Field(description="우리 반에 속상하게 하거나 힘들게 하는 친구가 있다 점수 (1~5)") #[cite: 3]
    점수_상담대상: int = Field(description="고민이 생겼을 때 마음을 터놓고 이야기할 대상이 있다 점수 (1~5)") #[cite: 3]
    비밀편지여부: str = Field(description="'비밀이에요!' 또는 '괜찮아요'") #[cite: 2, 3]
    하고싶은말: str = Field(description="선생님께 하고 싶은 말, 자랑하고 싶은 일, 고민 등 서술형 텍스트 전체") #[cite: 1, 2, 3]

# ---------------------------------------------------------
# 3. 데이터베이스 연동 함수 (Supabase + 로컬 백업 호환)
# ---------------------------------------------------------
def load_roster() -> pd.DataFrame:
    if supabase:
        try:
            res = supabase.table("student_roster").select("student_no, name").order("student_no").execute()
            if res.data:
                return pd.DataFrame(res.data)
        except Exception as e:
            st.warning(f"명렬표 로드 오류: {e}")
    # 로컬 폴백
    default_names = ["강하은", "권건우", "김민준", "박도윤", "서준서", "신지아", "오수아", "윤서아", "이서연", "임하윤", "장주원", "정시우", "조예준", "최지우", "한지호"]
    return pd.DataFrame({"student_no": range(1, len(default_names)+1), "name": default_names})

def save_roster(roster_df: pd.DataFrame):
    if supabase:
        supabase.table("student_roster").delete().neq("student_no", -999).execute()
        records = roster_df[["student_no", "name"]].to_dict(orient="records")
        if records:
            supabase.table("student_roster").insert(records).execute()

def load_responses() -> pd.DataFrame:
    if supabase:
        try:
            res = supabase.table("survey_responses").select("*").order("제출일시", desc=False).execute()
            if res.data:
                return pd.DataFrame(res.data).fillna("")
        except Exception as e:
            st.error(f"DB 조회 오류: {e}")
    return pd.DataFrame()

def insert_responses(records: list[dict]):
    if supabase:
        supabase.table("survey_responses").insert(records).execute()
    else:
        st.error("Supabase 연결 설정이 되어 있지 않습니다. Secrets 설정을 확인해주세요.")

def to_excel_bytes(df: pd.DataFrame):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="설문결과")
    return output.getvalue()

# ---------------------------------------------------------
# 4. 위험도 및 교우관계 지표 산출 함수
# ---------------------------------------------------------
def analyze_student_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    weather_score = {"아주 맑음": 0, "구름 조금": 1, "비 옴": 3, "천둥 번개": 4}
    mood_score = {"기분이 좋아요": 0, "그냥 보통이에요": 1, "조금 지쳐요": 2, "속상해요": 3, "화가 나요": 4}
    deep_score = {
        "평소처럼 괜찮고, 별로 힘든 게 없어요.": 0,
        "가끔씩 짜증이 나거나 기분이 좀 가라앉아요.": 1,
        "자주 짜증이 나고, 마음이 불안해요.": 3,
        "많이 힘들고, 울고 싶을 때가 있어요.": 4,
        "기분이 계속 안 좋고, 아무것도 하기 싫어요.": 5
    }

    in_degree_close = {name: 0 for name in df["이름"]}
    in_degree_want = {name: 0 for name in df["이름"]}
    in_degree_distant = {name: 0 for name in df["이름"]}

    for _, row in df.iterrows():
        for col, target_dict in [("친한친구", in_degree_close), ("친해지고싶은친구", in_degree_want), ("멀리있는친구", in_degree_distant)]:
            if pd.notna(row[col]) and str(row[col]).strip() != "":
                targets = [t.strip() for t in str(row[col]).split(",") if t.strip() in in_degree_close]
                for t in targets:
                    target_dict[t] += 1

    df["친한친구_피지목수"] = df["이름"].map(in_degree_close)
    df["희망친구_피지목수"] = df["이름"].map(in_degree_want)
    df["멀리있는친구_피지목수"] = df["이름"].map(in_degree_distant)

    risk_scores, status_labels = [], []
    for _, row in df.iterrows():
        score = 0
        score += weather_score.get(row["마음날씨"], 1)
        score += mood_score.get(row["현재기분"], 1)
        score += deep_score.get(row["깊이있는마음"], 1)
        score += (5 - int(row["점수_즐거움"]))
        score += (int(row["점수_힘든친구"]) - 1)
        score += (5 - int(row["점수_상담대상"]))
        if row["비밀편지여부"] == "비밀이에요!":
            score += 3
        if row["친한친구_피지목수"] == 0:
            score += 2

        risk_scores.append(score)
        if score >= 14 or row["비밀편지여부"] == "비밀이에요!":
            status_labels.append("🚨 집중 관심 (상담 권고)")
        elif score >= 7 or row["친한친구_피지목수"] == 0:
            status_labels.append("⚠️ 세심한 관찰")
        else:
            status_labels.append("🟢 안정 및 양호")

    df["위기점수"] = risk_scores
    df["관심단계"] = status_labels
    return df

# ---------------------------------------------------------
# 5. Gemini Vision OCR 호출 함수
# ---------------------------------------------------------
def extract_survey_from_image(image_bytes, mime_type, api_key, survey_round, valid_names):
    client = genai.Client(api_key=api_key)
    roster_hint = ", ".join(valid_names)
    prompt = f"""
    이 이미지는 초등학생이 작성한 '마음 이야기 / 감정 워크시트 / 친구 관계 이야기' 설문지입니다.
    체크된 항목과 손글씨를 정확히 읽어서 JSON 스키마에 맞게 추출하세요.
    - 우리 반 학생 명단: [{roster_hint}]
    - 손글씨로 적힌 본인의 이름이나 친구 이름이 위 명단에 있는 이름과 비슷하다면, 오타가 나지 않도록 명단의 정확한 이름으로 보정해서 적어주세요.
    - '마음날씨'가 없는 설문지 양식이면 '현재기분'에 맞춰('기분이 좋아요'->'아주 맑음', '그냥 보통이에요'->'구름 조금', '조금 지쳐요'/'속상해요'->'비 옴', '화가 나요'->'천둥 번개') 자동 매핑하세요.
    """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type), prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SurveyExtraction,
            temperature=0.1
        )
    )
    res = json.loads(response.text)
    return {
        "survey_round": survey_round,
        "이름": res.get("이름", "미기재"),
        "마음날씨": res.get("마음날씨", "구름 조금"),
        "현재기분": res.get("현재기분", "그냥 보통이에요"),
        "깊이있는마음": res.get("깊이있는마음", "평소처럼 괜찮고, 별로 힘든 게 없어요."),
        "주요고민": ", ".join(res.get("주요고민", [])) if res.get("주요고민") else "특별히 없음",
        "짜증순간": ", ".join(res.get("짜증순간", [])),
        "필요한것": ", ".join(res.get("필요한것", [])),
        "친한친구": res.get("친한친구", ""),
        "자주노는친구": res.get("자주노는친구", ""),
        "친해지고싶은친구": res.get("친해지고싶은친구", ""),
        "멀리있는친구": res.get("멀리있는친구", ""),
        "관계어려움": ", ".join(res.get("관계어려움", [])) if res.get("관계어려움") else "특별히 어려운 점은 없어요.",
        "점수_즐거움": int(res.get("점수_즐거움", 4)),
        "점수_힘든친구": int(res.get("점수_힘든친구", 1)),
        "점수_상담대상": int(res.get("점수_상담대상", 4)),
        "비밀편지여부": res.get("비밀편지여부", "괜찮아요"),
        "하고싶은말": res.get("하고싶은말", ""),
        "제출방식": "AI 스캔(OCR)",
        "제출일시": datetime.now(KST).isoformat()
    }

# ---------------------------------------------------------
# 6. 사이드바 & 교사용 PIN 잠금 인증
# ---------------------------------------------------------
st.sidebar.title("🏫 우리 반 마음 시스템")
app_mode = st.sidebar.radio(
    "메뉴 선택",
    [
        "👦 1. 학생용 설문 참여",
        "📸 2. 종이 설문지 AI 스캔 (교사용)",
        "📊 3. 통합 분석 & 변화 추적 (교사용)",
        "📋 4. 학급 명렬표 & 회차 설정 (교사용)"
    ]
)

roster_df = load_roster()
student_list = roster_df["name"].tolist()
student_display_list = [f"{row['student_no']}번 {row['name']}" for _, row in roster_df.iterrows()]

# 현재 활성 설문 회차 설정 (기본값: 이번 달 차수)
if "active_round" not in st.session_state:
    st.session_state["active_round"] = f"{datetime.now(KST).strftime('%Y-%m')}차 설문"

# 교사용 메뉴 진입 시 4자리 PIN 잠금 확인 함수
def check_teacher_pin() -> bool:
    if st.session_state.get("teacher_authenticated", False):
        if st.sidebar.button("🔒 교사 모드 잠그기 (로그아웃)"):
            st.session_state["teacher_authenticated"] = False
            st.rerun()
        return True

    st.warning("🔒 **선생님 전용 화면입니다.** 진입하려면 4자리 비밀번호(PIN)를 입력해주세요.")
    pin_col1, pin_col2 = st.columns([1, 2])
    with pin_col1:
        pin_input = st.text_input("비밀번호(PIN) 4자리", type="password", max_chars=8, placeholder="****")
        if st.button("잠금 해제", type="primary"):
            if pin_input == TEACHER_PIN:
                st.session_state["teacher_authenticated"] = True
                st.rerun()
            else:
                st.error("❌ 비밀번호가 일치하지 않습니다.")
    return False

# =========================================================
# [모드 1] 👦 학생용 설문 참여 (드롭다운 명렬표 연동)
# =========================================================
if app_mode == "👦 1. 학생용 설문 참여":
    st.title("🌈 우리들의 마음과 친구 관계 이야기")
    st.info("💡 **마음도 쉬어가도 괜찮아요!** 지금 느끼는 솔직한 마음과 친구 이야기를 선택해 주면, 선생님이 우리 반을 더 행복하게 만드는 데 큰 도움이 될 거예요.") #[cite: 1, 2, 3]

    # 1. 내 이름 선택 (폼 외부에서 선택하여 친구 목록에서 본인 이름 자동 제외)
    st.subheader("✏️ 1. 내 번호와 이름을 선택해주세요")
    selected_me_display = st.selectbox(
        "출석번호와 이름을 골라주세요 *",
        options=["-- 내 이름을 선택하세요 --"] + student_display_list
    )

    if selected_me_display != "-- 내 이름을 선택하세요 --":
        my_name = selected_me_display.split("번 ", 1)[1]
        other_friends = [name for name in student_list if name != my_name]

        with st.form("student_survey_form", clear_on_submit=True):
            st.subheader("2️⃣ 지금 나의 마음 날씨는 어떤가요?") #[cite: 3]
            c_m1, c_m2 = st.columns(2)
            with c_m1:
                weather_choice = st.radio(
                    "가장 마음에 드는 날씨를 골라보세요.", #[cite: 3]
                    [
                        "☀️ 아주 맑음 (매일매일 신나고 즐거워요!)", #[cite: 3]
                        "⛅ 구름 조금 (평범하지만 가끔 심심하거나 귀찮아요.)", #[cite: 3]
                        "🌧️ 비 옴 (요즘 조금 속상하거나 걱정이 있어요.)", #[cite: 3]
                        "⛈️ 천둥 번개 (화가 나거나 많이 힘들고 지쳐 있어요.)" #[cite: 3]
                    ]
                )
                mood_choice = st.radio(
                    "지금 내 기분은 어떤가요?", #[cite: 1]
                    ["😄 기분이 좋아요", "🙂 그냥 보통이에요", "😐 조금 지쳐요", "😢 속상해요", "😡 화가 나요"] #[cite: 1]
                )
            with c_m2:
                deep_choice = st.radio(
                    "다음 중 요즘 나의 마음과 가장 가까운 것은 무엇인가요?", #[cite: 1]
                    [
                        "평소처럼 괜찮고, 별로 힘든 게 없어요.", #[cite: 1]
                        "가끔씩 짜증이 나거나 기분이 좀 가라앉아요.", #[cite: 1]
                        "자주 짜증이 나고, 마음이 불안해요.", #[cite: 1]
                        "많이 힘들고, 울고 싶을 때가 있어요.", #[cite: 1]
                        "기분이 계속 안 좋고, 아무것도 하기 싫어요." #[cite: 1]
                    ]
                )

            st.divider()
            st.subheader("3️⃣ 요즘 힘들거나 짜증나는 일과 필요한 도움") #[cite: 1, 3]
            c_w1, c_w2, c_w3 = st.columns(3)
            with c_w1:
                worries_sel = st.multiselect(
                    "요즘 힘들거나 걱정되는 것 (여러 개 선택 가능)", #[cite: 1, 3]
                    [
                        "공부가 어렵거나 성적이 걱정돼요.", #[cite: 1]
                        "숙제나 준비물을 챙기기 힘들어요.", #[cite: 1, 3]
                        "친구 관계 때문에 속상해요.", #[cite: 1]
                        "선생님이나 부모님께 혼나는 것이 걱정돼요.", #[cite: 1]
                        "몸이 피곤하고 에너지가 없어요.", #[cite: 1, 3]
                        "특별히 없음"
                    ]
                )
                worry_etc = st.text_input("기타 힘든 점 직접 적기") #[cite: 1, 3]
            with c_w2:
                moments_sel = st.multiselect(
                    "요즘 짜증나는 순간은 언제인가요?", #[cite: 1]
                    ["공부할 때", "숙제나 준비를 할 때", "친구들과 있을 때", "쉬는 시간이나 놀이 시간에", "식사할 때", "집에 있을 때"] #[cite: 1]
                )
            with c_w3:
                needs_sel = st.multiselect(
                    "지금 나에게 가장 필요한 것은 무엇일까요?", #[cite: 1]
                    ["따뜻한 말과 격려", "친구와 더 많은 시간", "공부에 대한 도움", "충분한 휴식과 수면", "좋아하는 활동(놀이, 취미 등)", "선생님과 이야기하기"] #[cite: 1]
                )

            st.divider()
            st.subheader("4️⃣ 우리들의 친구 관계 이야기 (목록에서 친구 선택)") #[cite: 2, 3]
            st.caption("친구 이름을 목록에서 클릭해서 골라보세요! 여러 명을 고를 수 있어요.")
            
            c_f1, c_f2 = st.columns(2)
            with c_f1:
                close_friends = st.multiselect("💖 내가 가장 편안하고 친하게 지내는 친구", options=other_friends) #[cite: 2, 3]
                play_friends = st.multiselect("🏃 쉬는 시간이나 놀이 시간에 자주 함께하는 친구", options=other_friends) #[cite: 2, 3]
            with c_f2:
                want_friends = st.multiselect("🌟 앞으로 더 친해지고 싶은(함께 놀고 싶은) 친구", options=other_friends) #[cite: 2, 3]
                distant_friends = st.multiselect("💭 평소에 잘 이야기하지 못하거나 조금 멀리 있는(어려운) 친구", options=other_friends) #[cite: 2, 3]

            rel_diff_sel = st.multiselect(
                "친구 관계에서 속상하거나 어려웠던 경험이 있다면 골라주세요.", #[cite: 2]
                [
                    "특별히 어려운 점은 없어요.", #[cite: 2]
                    "놀이에 끼고 싶었지만 끼지 못했어요.", #[cite: 2]
                    "친구들이 내 말을 잘 들어주지 않아요.", #[cite: 2]
                    "친구와 자주 다투거나 오해가 생겨요.", #[cite: 2]
                    "혼자 있는 시간이 많아요.", #[cite: 2]
                    "친구 때문에 속상하지만 말하기 어려워요." #[cite: 2]
                ]
            )

            st.markdown("##### 📊 우리 반 생활을 점수로 표현해 볼까요? (1점: 전혀 아니다 ~ 5점: 매우 그렇다)") #[cite: 3]
            sc1, sc2, sc3 = st.columns(3)
            with sc1:
                s_fun = st.slider("1. 친구들과 함께 노는 것이 즐겁고 편안하다.", 1, 5, 4) #[cite: 3]
            with sc2:
                s_hard = st.slider("2. 우리 반에 나를 속상하게 하거나 힘들게 하는 친구가 있다.", 1, 5, 1) #[cite: 3]
            with sc3:
                s_talk = st.slider("3. 고민이 생겼을 때 마음을 터놓고 이야기할 친구나 선생님이 있다.", 1, 5, 4) #[cite: 3]

            st.divider()
            st.subheader("5️⃣ 선생님께 하고 싶은 말 & 비밀 편지 💌") #[cite: 1, 2, 3]
            free_text = st.text_area(
                "요즘 나를 기쁘게 했던 일, 자랑하고 싶은 것, 혹은 선생님께 하고 싶은 고민 이야기를 자유롭게 적어주세요.", #[cite: 3]
                height=100
            )
            secret_opt = st.radio(
                "🔒 선생님과의 비밀 편지: 이 내용 중에서 선생님이 혼자만 알고 도와주기를 바라는 비밀이 있나요?", #[cite: 2, 3]
                [
                    "😊 괜찮아요 (선생님이랑 편하게 이야기해도 돼요.)", #[cite: 2, 3]
                    "🔒 비밀이에요! (선생님만 보고 상담이나 도움이 필요할 때 저를 도와주세요.)" #[cite: 2, 3]
                ]
            )

            submitted = st.form_submit_button("💌 선생님께 내 마음 보내기 (제출)", use_container_width=True)

            if submitted:
                clean_weather = weather_choice.split(" (")[0].replace("☀️ ", "").replace("⛅ ", "").replace("🌧️ ", "").replace("⛈️ ", "")
                clean_mood = mood_choice.split(" ", 1)[1]
                clean_secret = "비밀이에요!" if "비밀이에요!" in secret_opt else "괜찮아요"
                if worry_etc.strip():
                    worries_sel.append(worry_etc.strip())

                record = {
                    "survey_round": st.session_state["active_round"],
                    "이름": my_name,
                    "마음날씨": clean_weather,
                    "현재기분": clean_mood,
                    "깊이있는마음": deep_choice,
                    "주요고민": ", ".join(worries_sel) if worries_sel else "특별히 없음",
                    "짜증순간": ", ".join(moments_sel),
                    "필요한것": ", ".join(needs_sel),
                    "친한친구": ", ".join(close_friends),
                    "자주노는친구": ", ".join(play_friends),
                    "친해지고싶은친구": ", ".join(want_friends),
                    "멀리있는친구": ", ".join(distant_friends),
                    "관계어려움": ", ".join(rel_diff_sel) if rel_diff_sel else "특별히 어려운 점은 없어요.",
                    "점수_즐거움": s_fun,
                    "점수_힘든친구": s_hard,
                    "점수_상담대상": s_talk,
                    "비밀편지여부": clean_secret,
                    "하고싶은말": free_text.strip(),
                    "제출방식": "태블릿 웹설문",
                    "제출일시": datetime.now(KST).isoformat()
                }
                insert_responses([record])
                st.balloons()
                st.success(f"🎉 **{my_name}** 친구의 소중한 마음 이야기가 선생님께 잘 전달되었어요! 고마워요!")

# =========================================================
# [모드 2] 📸 종이 설문지 AI 스캔 (교사용 - PIN 보호)
# =========================================================
elif app_mode == "📸 2. 종이 설문지 AI 스캔 (교사용)":
    if check_teacher_pin():
        st.title("📸 종이 설문지 AI 자동 인식 (Gemini Vision OCR)")
        st.markdown("종이 설문지 스캔본을 업로드하면 **학급 명렬표와 대조하여 학생 이름 오타까지 자동 보정**한 뒤 Supabase DB와 엑셀로 변환합니다.")

        c_k1, c_k2 = st.columns(2)
        with c_k1:
            ocr_round = st.text_input("저장할 설문 회차 이름", value=st.session_state["active_round"])
        with c_k2:
            api_key_input = st.text_input("Gemini API Key", value=GEMINI_API_KEY, type="password")

        uploaded_images = st.file_uploader(
            "스캔한 설문지 이미지(.png, .jpg, .jpeg) 다중 업로드",
            type=["png", "jpg", "jpeg"],
            accept_multiple_files=True
        )

        if uploaded_images and st.button("🤖 Gemini Vision으로 일괄 판독 시작", type="primary"):
            if not api_key_input:
                st.error("⚠️ Gemini API Key를 입력해주세요.")
            else:
                extracted_rows = []
                prog = st.progress(0)
                for i, img_file in enumerate(uploaded_images):
                    mime_type = "image/jpeg" if img_file.name.lower().endswith(("jpg", "jpeg")) else "image/png"
                    row_dict = extract_survey_from_image(img_file.getvalue(), mime_type, api_key_input, ocr_round, student_list)
                    extracted_rows.append(row_dict)
                    prog.progress((i + 1) / len(uploaded_images))
                st.session_state["ocr_df"] = pd.DataFrame(extracted_rows)

        if "ocr_df" in st.session_state and not st.session_state["ocr_df"].empty:
            st.subheader("📝 AI 판독 결과 검수 (더블클릭하여 수정 가능)")
            edited_df = st.data_editor(st.session_state["ocr_df"], num_rows="dynamic", use_container_width=True)
            b1, b2 = st.columns(2)
            with b1:
                if st.button("💾 Supabase 클라우드 DB에 일괄 저장", type="primary", use_container_width=True):
                    insert_responses(edited_df.to_dict(orient="records"))
                    st.success("✅ 클라우드 DB에 저장 완료되었습니다!")
            with b2:
                st.download_button(
                    "📥 엑셀(.xlsx)로 다운로드",
                    data=to_excel_bytes(edited_df),
                    file_name=f"AI스캔결과_{ocr_round}.xlsx",
                    use_container_width=True
                )

# =========================================================
# [모드 3] 📊 교사용 통합 분석 & 변화 추적 대시보드 (PIN 보호)
# =========================================================
elif app_mode == "📊 3. 통합 분석 & 변화 추적 (교사용)":
    if check_teacher_pin():
        st.title("📊 우리 반 마음 이야기 & 교우관계 통합 분석 대시보드")
        all_df = load_responses()

        if all_df.empty:
            st.info("아직 저장된 설문 응답이 없습니다. 학생용 설문이나 AI 스캔을 통해 데이터를 추가해 주세요.")
        else:
            # 설문 회차 필터링
            rounds = sorted(all_df["survey_round"].unique().tolist(), reverse=True)
            col_r1, col_r2 = st.columns([2, 1])
            with col_r1:
                selected_round = st.selectbox("📅 조회할 설문 회차를 선택하세요:", options=rounds)
            with col_r2:
                st.download_button(
                    "📥 전체 누적 데이터 엑셀 다운로드",
                    data=to_excel_bytes(all_df),
                    file_name=f"학급설문_전체누적_{datetime.now(KST).strftime('%Y%m%d')}.xlsx",
                    use_container_width=True
                )

            # 선택된 회차에서 학생별 가장 최신 응답 1건씩 추출
            round_df = all_df[all_df["survey_round"] == selected_round].drop_duplicates(subset=["이름"], keep="last")
            df = analyze_student_metrics(round_df)

            tab1, tab2, tab3, tab4, tab5 = st.tabs([
                "📊 1. 학급 전체 통계", 
                "🕸️ 2. 교우관계 소시오그램", 
                "🔍 3. 위기 스크리닝 & 체크리스트", 
                "🧒 4. 학생 개별 리포트",
                "📈 5. 시계열 변화 추이 (회차 비교)"
            ])

            # --- [탭 1] 학급 전체 통계 ---
            with tab1:
                c1, c2, c3, c4 = st.columns(4)
                total_s = len(df)
                sunny_s = len(df[df["마음날씨"].isin(["아주 맑음", "구름 조금"])])
                rainy_s = len(df[df["마음날씨"].isin(["비 옴", "천둥 번개"])])
                secret_s = len(df[df["비밀편지여부"] == "비밀이에요!"])

                c1.metric("응답 학생 수", f"{total_s} / {len(student_list)}명")
                c2.metric("☀️ 마음 맑음/구름", f"{sunny_s}명 ({sunny_s/max(1, total_s)*100:.0f}%)")
                c3.metric("🌧️ 마음 비/천둥", f"{rainy_s}명 ({rainy_s/max(1, total_s)*100:.0f}%)")
                c4.metric("🔒 비밀 상담 요청", f"{secret_s}명")

                col_l, col_r = st.columns(2)
                with col_l:
                    st.subheader("🌤️ 마음 날씨 분포") #[cite: 3]
                    w_cnt = df["마음날씨"].value_counts().reset_index()
                    w_cnt.columns = ["마음날씨", "학생수"]
                    fig_w = px.pie(w_cnt, names="마음날씨", values="학생수", hole=0.4,
                                   color="마음날씨", color_discrete_map={"아주 맑음": "#FFD93D", "구름 조금": "#6BCB77", "비 옴": "#4D96FF", "천둥 번개": "#FF6B6B"})
                    st.plotly_chart(fig_w, use_container_width=True)
                with col_r:
                    st.subheader("💭 주요 고민 분포") #[cite: 1, 3]
                    worries = df["주요고민"].str.split(",").explode().str.strip()
                    worry_cnt = worries[worries != ""].value_counts().reset_index()
                    worry_cnt.columns = ["고민", "응답수"]
                    fig_wr = px.bar(worry_cnt, x="응답수", y="고민", orientation="h", color="응답수", color_continuous_scale="Teal")
                    st.plotly_chart(fig_wr, use_container_width=True)

            # --- [탭 2] 교우관계 소시오그램 ---
            with tab2:
                st.subheader(f"🕸️ [{selected_round}] 교우관계 네트워크 맵")
                rel_type = st.radio(
                    "관계 유형 선택:",
                    ["💖 가장 친한 친구", "🏃 자주 함께 노는 친구", "🌟 더 친해지고 싶은 친구", "💔 조금 멀리 있는(어려운) 친구"], #[cite: 2, 3]
                    horizontal=True
                )
                col_map = {
                    "💖 가장 친한 친구": ("친한친구", "#2ECC71"),
                    "🏃 자주 함께 노는 친구": ("자주노는친구", "#3498DB"),
                    "🌟 더 친해지고 싶은 친구": ("친해지고싶은친구", "#F1C40F"),
                    "💔 조금 멀리 있는(어려운) 친구": ("멀리있는친구", "#E74C3C")
                }
                target_col, edge_color = col_map[rel_type]

                G = nx.DiGraph()
                for _, row in df.iterrows():
                    G.add_node(row["이름"], weather=row["마음날씨"])
                for _, row in df.iterrows():
                    src = row["이름"]
                    targets = [t.strip() for t in str(row[target_col]).split(",") if t.strip() in G.nodes]
                    for dst in targets:
                        G.add_edge(src, dst)

                pos = nx.spring_layout(G, k=0.8, iterations=60, seed=42)
                edge_x, edge_y = [], []
                for u, v in G.edges():
                    x0, y0 = pos[u]; x1, y1 = pos[v]
                    edge_x.extend([x0, x1, None]); edge_y.extend([y0, y1, None])

                edge_trace = go.Scatter(x=edge_x, y=edge_y, line=dict(width=2, color=edge_color), hoverinfo='none', mode='lines')
                node_x, node_y, node_text, node_size, node_color = [], [], [], [], []
                weather_colors = {"아주 맑음": "#FFD93D", "구름 조금": "#6BCB77", "비 옴": "#4D96FF", "천둥 번개": "#FF6B6B"}

                for node in G.nodes():
                    x, y = pos[node]
                    node_x.append(x); node_y.append(y)
                    in_d = G.in_degree(node); out_d = G.out_degree(node)
                    w = G.nodes[node]["weather"]
                    node_text.append(f"<b>{node}</b><br>받은 지목: {in_d}회 / 한 지목: {out_d}회<br>마음날씨: {w}")
                    node_size.append(22 + in_d * 8)
                    node_color.append(weather_colors.get(w, "#CCCCCC"))

                node_trace = go.Scatter(
                    x=node_x, y=node_y, mode='markers+text', text=list(G.nodes()), textposition="top center",
                    hoverinfo='text', hovertext=node_text,
                    marker=dict(showscale=False, color=node_color, size=node_size, line=dict(width=2, color='#333'))
                )
                fig_net = go.Figure(data=[edge_trace, node_trace], layout=go.Layout(
                    showlegend=False, hovermode='closest', margin=dict(b=10, l=10, r=10, t=30),
                    xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                    yaxis=dict(showgrid=False, zeroline=False, showticklabels=False), height=500
                ))
                st.plotly_chart(fig_net, use_container_width=True)

            # --- [탭 3] 위기 스크리닝 ---
            with tab3:
                st.subheader("🔍 우선 관심 및 상담 필요 학생 분류") #[cite: 3]
                display_cols = ["관심단계", "이름", "마음날씨", "현재기분", "깊이있는마음", "친한친구_피지목수", "비밀편지여부", "주요고민", "하고싶은말"]
                st.dataframe(df.sort_values(by="위기점수", ascending=False)[display_cols], use_container_width=True, hide_index=True)

            # --- [탭 4] 학생 개별 리포트 ---
            with tab4:
                sel_stu = st.selectbox("조회할 학생 선택:", df["이름"].tolist())
                s_row = df[df["이름"] == sel_stu].iloc[0]
                st.markdown(f"### **{s_row['이름']}** ({s_row['관심단계']})")
                st.write(f"- **마음 날씨 / 현재 기분:** {s_row['마음날씨']} / {s_row['현재기분']}") #[cite: 1, 3]
                st.write(f"- **내가 선택한 친한 친구:** {s_row['친한친구'] or '없음'}") #[cite: 2, 3]
                st.write(f"- **어려운/멀리 있는 친구:** {s_row['멀리있는친구'] or '없음'} ({s_row['관계어려움']})") #[cite: 2, 3]
                st.write(f"- **선생님께 하고 싶은 말 ({s_row['비밀편지여부']}):** {s_row['하고싶은말'] or '없음'}") #[cite: 1, 2, 3]

            # --- [탭 5] 시계열 변화 추이 (회차별 비교 분석) ---
            with tab5:
                st.subheader("📈 회차별 학생 마음 상태 & 교우관계 변화 추이")
                st.markdown("설문이 누적됨에 따라 학생의 **위기점수(마음 상태)**와 **친한 친구 피지목수(교우관계 중심성)**가 어떻게 변화했는지 추적합니다.")

                # 전체 회차별 지표 계산
                history_frames = []
                for r_name in all_df["survey_round"].unique():
                    sub_df = all_df[all_df["survey_round"] == r_name].drop_duplicates(subset=["이름"], keep="last")
                    history_frames.append(analyze_student_metrics(sub_df))
                full_history_df = pd.concat(history_frames, ignore_index=True)

                target_stu_hist = st.selectbox("변화 추이를 확인할 학생 선택:", student_list, key="hist_stu")
                stu_trend_df = full_history_df[full_history_df["이름"] == target_stu_hist].sort_values("제출일시")

                if len(stu_trend_df) < 1:
                    st.info("해당 학생의 설문 기록이 아직 없습니다.")
                else:
                    tc1, tc2 = st.columns(2)
                    with tc1:
                        fig_t1 = px.line(
                            stu_trend_df, x="survey_round", y="위기점수", markers=True,
                            title=f"{target_stu_hist} 학생 마음 위기점수 변화 (낮을수록 안정)"
                        )
                        st.plotly_chart(fig_t1, use_container_width=True)
                    with tc2:
                        fig_t2 = px.line(
                            stu_trend_df, x="survey_round", y="친한친구_피지목수", markers=True,
                            title=f"{target_stu_hist} 학생 친한 친구 피지목 횟수 변화"
                        )
                        st.plotly_chart(fig_t2, use_container_width=True)

                    st.markdown("##### 📋 회차별 상세 응답 이력 비교표")
                    hist_cols = ["survey_round", "제출일시", "관심단계", "마음날씨", "현재기분", "친한친구", "멀리있는친구", "친한친구_피지목수", "하고싶은말"]
                    st.dataframe(stu_trend_df[hist_cols], use_container_width=True, hide_index=True)

# =========================================================
# [모드 4] 📋 학급 명렬표 & 설문 회차 설정 (교사용 - PIN 보호)
# =========================================================
elif app_mode == "📋 4. 학급 명렬표 & 회차 설정 (교사용)":
    if check_teacher_pin():
        st.title("📋 학급 명렬표 및 설문 회차 관리")
        
        st.subheader("1️⃣ 현재 진행 중인 설문 회차 설정")
        st.caption("회차 이름을 바꾸면(예: '3월 1차', '5월 2차', '2학기 상담주간'), 학생들이 제출하는 설문이 새로운 회차로 구분되어 저장됩니다.")
        new_round = st.text_input("현재 활성 설문 회차명", value=st.session_state["active_round"])
        if st.button("회차 이름 적용하기"):
            st.session_state["active_round"] = new_round
            st.success(f"현재 설문 회차가 **[{new_round}]**로 설정되었습니다.")

        st.divider()
        st.subheader("2️⃣ 우리 반 명렬표 편집 (출석번호 · 이름)")
        st.caption("표 하단의 빈칸을 클릭해 학생을 추가하거나 수정한 뒤 [명렬표 저장] 버튼을 누르면 학생용 설문 드롭다운에 즉시 반영됩니다.")
        edited_roster = st.data_editor(roster_df, num_rows="dynamic", use_container_width=True)
        if st.button("💾 명렬표 Supabase DB에 저장하기", type="primary"):
            save_roster(edited_roster)
            st.success("✅ 학급 명렬표가 업데이트되었습니다!")
            st.rerun()
