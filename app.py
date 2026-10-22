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
from PIL import Image
from pdf2image import convert_from_bytes
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from supabase import create_client, Client

KST = timezone(timedelta(hours=9))

st.set_page_config(
    page_title="우리 반 마음 & 교우관계 통합 시스템",
    page_icon="🏫",
    layout="wide"
)

def get_secret(key, default=""):
    if key in st.secrets:
        return st.secrets[key]
    return os.environ.get(key, default)

SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_KEY = get_secret("SUPABASE_KEY")
GEMINI_API_KEY = get_secret("GEMINI_API_KEY")

@st.cache_resource
def init_supabase() -> Client | None:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            return create_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception as e:
            return None
    return None

supabase = init_supabase()

# ---------------------------------------------------------
# 1. Gemini Vision OCR 스키마
# ---------------------------------------------------------
class SurveyExtraction(BaseModel):
    이름: str = Field(description="학생 이름")
    마음날씨: str = Field(description="'아주 맑음', '구름 조금', '비 옴', '천둥 번개' 중 하나")
    현재기분: str = Field(description="'기분이 좋아요', '그냥 보통이에요', '조금 지쳐요', '속상해요', '화가 나요' 중 하나")
    깊이있는마음: str = Field(description="요즘 나의 마음과 가장 가까운 문장")
    주요고민: list[str] = Field(description="체크된 고민 목록")
    짜증순간: list[str] = Field(description="짜증나는 순간 목록")
    필요한것: list[str] = Field(description="필요한 것 목록")
    친한친구: str = Field(description="가장 친한 친구 이름 (쉼표 구분)")
    자주노는친구: str = Field(description="자주 함께 노는 친구 이름 (쉼표 구분)")
    친해지고싶은친구: str = Field(description="친해지고 싶은 친구 이름 (쉼표 구분)")
    멀리있는친구: str = Field(description="멀리 있는 친구 이름 (쉼표 구분)")
    관계어려움: list[str] = Field(description="친구 관계 어려움 항목")
    점수_즐거움: int = Field(description="1~5점")
    점수_힘든친구: int = Field(description="1~5점")
    점수_상담대상: int = Field(description="1~5점")
    비밀편지여부: str = Field(description="'비밀이에요!' 또는 '괜찮아요'")
    하고싶은말: str = Field(description="서술형 텍스트 전체")

# ---------------------------------------------------------
# 2. DB 연동 함수
# ---------------------------------------------------------
def get_all_teachers() -> list[dict]:
    if supabase:
        try:
            res = supabase.table("teacher_classes").select("*").execute()
            if res.data: return res.data
        except: pass
    return []

def register_teacher(class_code, teacher_name, pin_code):
    if supabase:
        try:
            supabase.table("teacher_classes").upsert({
                "class_code": class_code, "teacher_name": teacher_name, "pin_code": pin_code
            }).execute()
            return True, None
        except Exception as e: return False, str(e)
    return False, "Supabase 미연결"

def load_roster(class_code: str) -> pd.DataFrame:
    if supabase:
        try:
            res = supabase.table("student_roster").select("student_no, name").eq("class_code", class_code).order("student_no").execute()
            if res.data: return pd.DataFrame(res.data)
        except: pass
    return pd.DataFrame(columns=["student_no", "name"])

def save_roster(class_code: str, roster_df: pd.DataFrame):
    if supabase:
        supabase.table("student_roster").delete().eq("class_code", class_code).execute()
        records = []
        for _, row in roster_df.iterrows():
            try:
                s_no = int(row["student_no"])
                s_name = str(row["name"]).strip()
                if s_name and s_name != "nan":
                    records.append({"class_code": class_code, "student_no": s_no, "name": s_name})
            except: continue
        if records: supabase.table("student_roster").insert(records).execute()

def load_responses(class_code: str) -> pd.DataFrame:
    if supabase:
        try:
            res = supabase.table("survey_responses").select("*").eq("class_code", class_code).order("제출일시", desc=False).execute()
            if res.data: return pd.DataFrame(res.data).fillna("")
        except: pass
    return pd.DataFrame()

def insert_responses(records: list[dict]):
    if supabase: supabase.table("survey_responses").insert(records).execute()

def to_excel_bytes(df: pd.DataFrame):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="설문결과")
    return output.getvalue()

# ---------------------------------------------------------
# 3. 분석 함수
# ---------------------------------------------------------
def analyze_student_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty: return df
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
                for t in targets: target_dict[t] += 1

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
        if row["비밀편지여부"] == "비밀이에요!": score += 3
        if row["친한친구_피지목수"] == 0: score += 2

        risk_scores.append(score)
        if score >= 14 or row["비밀편지여부"] == "비밀이에요!": status_labels.append("🚨 집중 관심 (상담 권고)")
        elif score >= 7 or row["친한친구_피지목수"] == 0: status_labels.append("⚠️ 세심한 관찰")
        else: status_labels.append("🟢 안정 및 양호")

    df["위기점수"] = risk_scores
    df["관심단계"] = status_labels
    return df

# ---------------------------------------------------------
# 4. 앱 세션 및 로그인 세팅
# ---------------------------------------------------------
if "current_class_code" not in st.session_state: st.session_state["current_class_code"] = ""
if "is_teacher_auth" not in st.session_state: st.session_state["is_teacher_auth"] = False

st.sidebar.title("🏫 우리 반 마음 시스템")
teachers = get_all_teachers()
class_options = [t["class_code"] for t in teachers]

st.sidebar.markdown("### 🎓 학급 선택 / 로그인")
selected_class = st.sidebar.selectbox("등록된 학급 선택", options=["-- 학급을 선택하세요 --"] + class_options)

if selected_class != "-- 학급을 선택하세요 --":
    st.session_state["current_class_code"] = selected_class
    t_info = next((t for t in teachers if t["class_code"] == selected_class), None)
    st.sidebar.markdown(f"**담당 선생님:** {t_info['teacher_name']} 님")
    pin_try = st.sidebar.text_input("교사용 비밀번호(PIN)", type="password", key="sidebar_pin")
    if st.sidebar.button("교사 모드 로그인"):
        if pin_try == t_info["pin_code"]:
            st.session_state["is_teacher_auth"] = True
            st.sidebar.success("로그인 성공!")
            st.rerun()
        else: st.sidebar.error("비밀번호가 틀렸습니다.")
else:
    st.session_state["current_class_code"] = ""
    st.session_state["is_teacher_auth"] = False

st.sidebar.divider()
st.sidebar.markdown("➕ **새로운 학급/선생님 등록**")
with st.sidebar.form("new_teacher_form"):
    new_code = st.text_input("학급 코드 (예: 3-7반)")
    new_tname = st.text_input("선생님 성함")
    new_pin = st.text_input("4자리 비밀번호 설정", type="password")
    if st.form_submit_button("학급 등록하기"):
        if new_code and new_tname and new_pin:
            ok, err = register_teacher(new_code.strip(), new_tname.strip(), new_pin.strip())
            if ok:
                st.success("등록 완료! 위에서 선택하세요.")
                st.rerun()
            else: st.error(f"등록 실패: {err}")
        else: st.warning("모든 항목을 입력하세요.")

active_class = st.session_state["current_class_code"]
roster_df = load_roster(active_class) if active_class else pd.DataFrame(columns=["student_no", "name"])
student_list = roster_df["name"].tolist() if not roster_df.empty else []
student_display_list = [f"{row['student_no']}번 {row['name']}" for _, row in roster_df.iterrows()] if not roster_df.empty else []

if "active_round" not in st.session_state:
    st.session_state["active_round"] = f"{datetime.now(KST).strftime('%Y-%m')}차 설문"

if not active_class:
    st.title("🌱 우리들의 마음 이야기 & 교우관계 통합 시스템")
    st.info("👈 좌측 사이드바에서 **[등록된 학급을 선택]**하거나 **[새로운 학급을 등록]**해주세요.")
    st.stop()

app_mode = st.sidebar.radio(
    "메뉴 선택",
    [
        "👦 1. 학생용 설문 참여",
        "📸 2. 종이 설문지 AI 스캔 (교사용)",
        "📊 3. 통합 분석 & 변화 추적 (교사용)",
        "📋 4. 엑셀로 명렬표 일괄 등록 & 회차 설정 (교사용)"
    ]
)

def require_teacher_auth() -> bool:
    if st.session_state["is_teacher_auth"]: return True
    st.warning("🔒 **교사 전용 메뉴입니다.** 좌측 사이드바에서 비밀번호(PIN)를 입력하고 로그인해 주세요.")
    return False

# =========================================================
# [모드 1] 학생용 설문 참여
# =========================================================
if app_mode == "👦 1. 학생용 설문 참여":
    st.title(f"🌈 [{active_class}] 마음과 친구 관계 이야기")
    if student_list:
        selected_me_display = st.selectbox("출석번호와 이름을 골라주세요 *", options=["-- 내 이름을 선택하세요 --"] + student_display_list)
        if selected_me_display != "-- 내 이름을 선택하세요 --":
            my_name = selected_me_display.split("번 ", 1)[1]
            other_friends = [name for name in student_list if name != my_name]

            with st.form("student_survey_form", clear_on_submit=True):
                st.subheader("2️⃣ 지금 나의 마음 날씨는 어떤가요?")
                c_m1, c_m2 = st.columns(2)
                with c_m1:
                    weather_choice = st.radio("날씨 선택", ["☀️ 아주 맑음", "⛅ 구름 조금", "🌧️ 비 옴", "⛈️ 천둥 번개"])
                    mood_choice = st.radio("기분 선택", ["😄 기분이 좋아요", "🙂 그냥 보통이에요", "😐 조금 지쳐요", "😢 속상해요", "😡 화가 나요"])
                with c_m2:
                    deep_choice = st.radio("마음과 가까운 것", [
                        "평소처럼 괜찮고, 별로 힘든 게 없어요.",
                        "가끔씩 짜증이 나거나 기분이 좀 가라앉아요.",
                        "자주 짜증이 나고, 마음이 불안해요.",
                        "많이 힘들고, 울고 싶을 때가 있어요.",
                        "기분이 계속 안 좋고, 아무것도 하기 싫어요."
                    ])

                st.divider()
                st.subheader("3️⃣ 요즘 힘들거나 짜증나는 일")
                c_w1, c_w2, c_w3 = st.columns(3)
                with c_w1:
                    worries_sel = st.multiselect("걱정되는 것", ["공부가 어렵거나 성적이 걱정돼요.", "숙제나 준비물을 챙기기 힘들어요.", "친구 관계 때문에 속상해요.", "선생님이나 부모님께 혼나는 것이 걱정돼요.", "몸이 피곤하고 에너지가 없어요."])
                    worry_etc = st.text_input("기타 힘든 점")
                with c_w2:
                    moments_sel = st.multiselect("짜증나는 순간", ["공부할 때", "숙제나 준비를 할 때", "친구들과 있을 때", "쉬는 시간이나 놀이 시간에", "식사할 때", "집에 있을 때"])
                with c_w3:
                    needs_sel = st.multiselect("필요한 것", ["따뜻한 말과 격려", "친구와 더 많은 시간", "공부에 대한 도움", "충분한 휴식과 수면", "좋아하는 활동(놀이, 취미 등)", "선생님과 이야기하기"])

                st.divider()
                st.subheader("4️⃣ 친구 관계 이야기")
                c_f1, c_f2 = st.columns(2)
                with c_f1:
                    close_friends = st.multiselect("💖 친하게 지내는 친구", options=other_friends)
                    play_friends = st.multiselect("🏃 자주 함께 노는 친구", options=other_friends)
                with c_f2:
                    want_friends = st.multiselect("🌟 더 친해지고 싶은 친구", options=other_friends)
                    distant_friends = st.multiselect("💭 조금 멀리 있는(어려운) 친구", options=other_friends)

                rel_diff_sel = st.multiselect("친구 관계 어려움", ["특별히 어려운 점은 없어요.", "놀이에 끼고 싶었지만 끼지 못했어요.", "친구들이 내 말을 잘 들어주지 않아요.", "친구와 자주 다투거나 오해가 생겨요.", "혼자 있는 시간이 많아요.", "친구 때문에 속상하지만 말하기 어려워요."])

                sc1, sc2, sc3 = st.columns(3)
                with sc1: s_fun = st.slider("1. 함께 노는 것이 즐겁다", 1, 5, 4)
                with sc2: s_hard = st.slider("2. 나를 힘들게 하는 친구가 있다", 1, 5, 1)
                with sc3: s_talk = st.slider("3. 이야기할 대상이 있다", 1, 5, 4)

                st.divider()
                free_text = st.text_area("하고 싶은 말 / 자랑 / 고민")
                secret_opt = st.radio("비밀 편지 여부", ["😊 괜찮아요", "🔒 비밀이에요!"])

                if st.form_submit_button("💌 제출하기", use_container_width=True):
                    if worry_etc.strip(): worries_sel.append(worry_etc.strip())
                    record = {
                        "class_code": active_class,
                        "survey_round": st.session_state["active_round"],
                        "이름": my_name,
                        "마음날씨": weather_choice.split(" ")[1],
                        "현재기분": mood_choice.split(" ", 1)[1],
                        "깊이있는마음": deep_choice,
                        "주요고민": ", ".join(worries_sel) if worries_sel else "특별히 없음",
                        "짜증순간": ", ".join(moments_sel),
                        "필요한것": ", ".join(needs_sel),
                        "친한친구": ", ".join(close_friends),
                        "자주노는친구": ", ".join(play_friends),
                        "친해지고싶은친구": ", ".join(want_friends),
                        "멀리있는친구": ", ".join(distant_friends),
                        "관계어려움": ",
