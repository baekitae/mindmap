import os
import json
import time
from io import BytesIO
from datetime import datetime, timezone, timedelta
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import networkx as nx
from PIL import Image
import fitz  # PyMuPDF
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

@st.cache_resource
def init_supabase() -> Client | None:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            return create_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception:
            return None
    return None

supabase = init_supabase()

# ---------------------------------------------------------
# 1. Gemini Vision OCR 스키마
# ---------------------------------------------------------
class SurveyExtraction(BaseModel):
    이름: str = Field(description="학생 이름 (미기재 시 '미기재')")
    마음날씨: str = Field(description="'아주 맑음', '구름 조금', '비 옴', '천둥 번개' 중 하나")
    현재기분: str = Field(description="'기분이 좋아요', '그냥 보통이에요', '조금 지쳐요', '속상해요', '화가 나요' 중 하나")
    깊이있는마음: str = Field(description="요즘 나의 마음과 가장 가까운 문장")
    주요고민: list[str] = Field(description="체크된 고민 목록")
    짜증순간: list[str] = Field(description="짜증나는 순간 목록")
    필요한것: list[str] = Field(description="필요한 것 목록")
    친한친구_1순위: str = Field(description="1순위로 친한 친구 이름")
    친한친구_1순위_이유: str = Field(description="1순위 친구와 친한 이유")
    친한친구_2순위: str = Field(description="2순위로 친한 친구 이름")
    친한친구_2순위_이유: str = Field(description="2순위 친구와 친한 이유")
    친한친구_3순위: str = Field(description="3순위로 친한 친구 이름")
    친한친구_3순위_이유: str = Field(description="3순위 친구와 친한 이유")
    자주노는친구: str = Field(description="쉬는 시간이나 놀이 시간에 함께 노는 친구 이름 (쉼표 구분)")
    친해지고싶은친구: str = Field(description="앞으로 더 친해지고 싶은 친구 이름 (쉼표 구분)")
    멀리있는친구: str = Field(description="조금 멀리 있거나 어려운 친구 이름 (쉼표 구분)")
    관계어려움: list[str] = Field(description="친구 관계 어려움 체크 항목들")
    점수_즐거움: int = Field(description="1~5점 (없으면 4)")
    점수_힘든친구: int = Field(description="1~5점 (없으면 1)")
    점수_상담대상: int = Field(description="1~5점 (없으면 4)")
    비밀편지여부: str = Field(description="'비밀이에요!' 또는 '괜찮아요'")
    하고싶은말: str = Field(description="서술형 텍스트 전체")

class BatchSurveyExtraction(BaseModel):
    results: list[SurveyExtraction] = Field(description="업로드된 각 설문지 이미지 순서대로 추출된 결과 리스트")

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

def register_teacher(class_code, teacher_name, pin_code, gemini_api_key=""):
    if supabase:
        try:
            supabase.table("teacher_classes").upsert({
                "class_code": class_code,
                "teacher_name": teacher_name,
                "pin_code": pin_code,
                "gemini_api_key": gemini_api_key
            }, on_conflict="class_code").execute()
            return True, None
        except Exception as e: return False, str(e)
    return False, "Supabase 미연결"

def update_teacher_api_key(class_code: str, new_api_key: str):
    if supabase:
        supabase.table("teacher_classes").update({"gemini_api_key": new_api_key}).eq("class_code", class_code).execute()

def load_roster(class_code: str) -> pd.DataFrame:
    if supabase:
        try:
            res = supabase.table("student_roster").select("student_no, name, gender").eq("class_code", class_code).order("student_no").execute()
            if res.data: return pd.DataFrame(res.data)
        except: pass
    return pd.DataFrame(columns=["student_no", "name", "gender"])

def save_roster(class_code: str, roster_df: pd.DataFrame):
    if supabase:
        supabase.table("student_roster").delete().eq("class_code", class_code).execute()
        records = []
        for _, row in roster_df.iterrows():
            try:
                s_no = int(row["student_no"])
                s_name = str(row["name"]).strip()
                s_gender = str(row["gender"]).strip()
                if s_name and s_name != "nan":
                    records.append({"class_code": class_code, "student_no": s_no, "name": s_name, "gender": s_gender})
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
# 3. 분석 함수 (1~3순위 가중치 반영)
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
        # 순위별 가중치 부여 (1순위: 3점, 2순위: 2점, 3순위: 1점)
        for col, weight in [("친한친구_1순위", 3), ("친한친구_2순위", 2), ("친한친구_3순위", 1)]:
            if col in row and pd.notna(row[col]) and str(row[col]).strip() in in_degree_close:
                target = str(row[col]).strip()
                in_degree_close[target] += weight

        for col, target_dict in [("친해지고싶은친구", in_degree_want), ("멀리있는친구", in_degree_distant)]:
            if col in row and pd.notna(row[col]) and str(row[col]).strip() != "":
                targets = [t.strip() for t in str(row[col]).split(",") if t.strip() in in_degree_close]
                for t in targets: target_dict[t] += 1

    df["친한친구_가중피지목수"] = df["이름"].map(in_degree_close)
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
        if df.loc[df["이름"] == row["이름"], "친한친구_가중피지목수"].values[0] == 0: score += 2

        risk_scores.append(score)
        if score >= 14 or row["비밀편지여부"] == "비밀이에요!": status_labels.append("🚨 집중 관심 (상담 권고)")
        elif score >= 7 or df.loc[df["이름"] == row["이름"], "친한친구_가중피지목수"].values[0] == 0: status_labels.append("⚠️ 세심한 관찰")
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

t_info = None
if selected_class != "-- 학급을 선택하세요 --":
    if st.session_state["current_class_code"] != selected_class:
        st.session_state["is_teacher_auth"] = False
        if "ocr_df" in st.session_state: del st.session_state["ocr_df"]
    st.session_state["current_class_code"] = selected_class
    t_info = next((t for t in teachers if t["class_code"] == selected_class), None)
    st.sidebar.markdown(f"**담당 선생님:** {t_info['teacher_name']} 님")
    pin_try = st.sidebar.text_input("교사용 비밀번호(PIN)", type="password", key=f"sidebar_pin_{selected_class}")
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
with st.sidebar.form("new_teacher_form", clear_on_submit=True):
    new_code = st.text_input("학급 코드 (예: 3-7반)")
    new_tname = st.text_input("선생님 성함")
    new_pin = st.text_input("4자리 비밀번호 설정", type="password")
    new_api_key = st.text_input("개인 Gemini API Key (선택)", type="password")
    if st.form_submit_button("학급 등록하기"):
        if new_code and new_tname and new_pin:
            ok, err = register_teacher(new_code.strip(), new_tname.strip(), new_pin.strip(), new_api_key.strip())
            if ok:
                st.success("등록 완료! 위에서 학급을 선택하세요.")
                st.rerun()
            else: st.error(f"등록 실패: {err}")
        else: st.warning("학급 코드, 성함, 비밀번호는 필수입니다.")

active_class = st.session_state["current_class_code"]
teacher_saved_api_key = (t_info.get("gemini_api_key") or "").strip() if t_info else ""

roster_df = load_roster(active_class) if active_class else pd.DataFrame(columns=["student_no", "name", "gender"])
student_list = roster_df["name"].tolist() if not roster_df.empty else []
student_display_list = [f"{row['student_no']}번 {row['name']} ({row['gender']})" for _, row in roster_df.iterrows()] if not roster_df.empty else []
student_gender_map = dict(zip(roster_df["name"], roster_df["gender"])) if not roster_df.empty else {}

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
        "📋 4. 명렬표·회차·API 키 설정 (교사용)"
    ]
)

def require_teacher_auth() -> bool:
    if st.session_state["is_teacher_auth"]: return True
    st.warning("🔒 **교사 전용 메뉴입니다.** 좌측 사이드바에서 비밀번호(PIN)를 입력하고 로그인해 주세요.")
    return False

# =========================================================
# [모드 1] 학생용 설문 참여 (1~3순위 + 이유 입력형 개편)
# =========================================================
if app_mode == "👦 1. 학생용 설문 참여":
    st.title(f"🌈 [{active_class}] 우리들의 마음 & 친구 관계 이야기")
    st.markdown(
        "##### ☀️ **지금 너의 마음은 어떤가요? 솔직한 마음을 적어주면, 선생님이 더 잘 이해하고 도와줄게요.**\n"
        "괜찮아요, 모든 감정은 소중해요. 선생님은 언제나 너의 이야기를 들어줄게요!"
    )
    st.divider()

    if student_list:
        st.subheader("👤 1단계: 내 이름 선택하기")
        selected_me_display = st.selectbox(
            "출석번호와 이름을 골라주세요 *",
            options=["-- 내 이름을 선택하세요 --"] + student_display_list
        )
        
        if selected_me_display != "-- 내 이름을 선택하세요 --":
            my_name = selected_me_display.split("번 ", 1)[1].split(" (")[0]
            other_friends = [name for name in student_list if name != my_name]

            with st.form("student_survey_form", clear_on_submit=True):
                st.markdown("### 🌤️ [1부] 요즘 나의 마음 날씨와 기분")
                col_m1, col_m2 = st.columns(2)
                with col_m1:
                    weather_map = {
                        "☀️ 아주 맑음 (매일매일 신나고 즐거워요!)": "아주 맑음",
                        "⛅ 구름 조금 (평범하지만 가끔 심심하거나 귀찮아요.)": "구름 조금",
                        "🌧️ 비 옴 (요즘 조금 속상하거나 걱정이 있어요.)": "비 옴",
                        "⛈️ 천둥 번개 (화가 나거나 많이 힘들고 지쳐 있어요.)": "천둥 번개"
                    }
                    weather_choice_full = st.radio("1. 요즘 나의 '마음 날씨'는 어떤가요?", options=list(weather_map.keys()))
                    st.write("")
                    mood_map = {
                        "😄 기분이 좋아요": "기분이 좋아요",
                        "🙂 그냥 보통이에요": "그냥 보통이에요",
                        "😐 조금 지쳐요": "조금 지쳐요",
                        "😢 속상해요": "속상해요",
                        "😡 화가 나요": "화가 나요"
                    }
                    mood_choice_full = st.radio("2. 지금 내 기분은 어떤가요?", options=list(mood_map.keys()))

                with col_m2:
                    deep_map = {
                        "😊 평소처럼 괜찮고, 별로 힘든 게 없어요.": "평소처럼 괜찮고, 별로 힘든 게 없어요.",
                        "🫤 가끔씩 짜증이 나거나 기분이 좀 가라앉아요.": "가끔씩 짜증이 나거나 기분이 좀 가라앉아요.",
                        "😟 자주 짜증이 나고, 마음이 불안해요.": "자주 짜증이 나고, 마음이 불안해요.",
                        "😣 많이 힘들고, 울고 싶을 때가 있어요.": "많이 힘들고, 울고 싶을 때가 있어요.",
                        "😞 기분이 계속 안 좋고, 아무것도 하기 싫어요.": "기분이 계속 안 좋고, 아무것도 하기 싫어요."
                    }
                    deep_choice_full = st.radio("3. 다음 중 요즘 나의 마음과 가장 가까운 것은 무엇인가요?", options=list(deep_map.keys()))

                st.divider()

                st.markdown("### 🧩 [2부] 요즘 나에게 힘든 일이 있나요?")
                c_w1, c_w2, c_w3 = st.columns(3)
                with c_w1:
                    st.markdown("**1. 나를 힘들게 하는 것**")
                    w_opts = ["📖 공부가 어렵거나 성적이 걱정돼요.", "🎒 숙제/준비물 챙기기 힘들어요.", "👫 친구 관계 문제로 속상해요.", "😥 혼나는 것이 걱정돼요.", "🔋 에너지가 없어요.", "🍀 특별한 힘든 일 없어요."]
                    worries_sel = []
                    for idx, opt in enumerate(w_opts):
                        if st.checkbox(opt, key=f"w_{idx}") and "특별한" not in opt: worries_sel.append(opt.split(" ", 1)[1])
                    worry_etc = st.text_input("✏️ 기타 걱정 직접 적기")

                with c_w2:
                    st.markdown("**2. 짜증나는 순간**")
                    m_opts = ["📚 공부할 때", "📝 숙제/준비할 때", "🗣️ 친구들과 있을 때", "🛝 놀이 시간에", "🍱 식사할 때", "🏠 집에 있을 때"]
                    moments_sel = []
                    for idx, opt in enumerate(m_opts):
                        if st.checkbox(opt, key=f"m_{idx}"): moments_sel.append(opt.split(" ", 1)[1])
                    moment_etc = st.text_input("✏️ 기타 순간 직접 적기")

                with c_w3:
                    st.markdown("**3. 필요한 것**")
                    n_opts = ["💖 따뜻한 말과 격려", "😊 친구와 더 많은 시간", "📖 공부 도움", "🛌 충분한 휴식", "🎵 좋아하는 활동", "💬 선생님과 대화"]
                    needs_sel = []
                    for idx, opt in enumerate(n_opts):
                        if st.checkbox(opt, key=f"n_{idx}"): needs_sel.append(opt.split(" ", 1)[1])
                    need_etc = st.text_input("✏️ 기타 필요한 것 직접 적기")

                st.divider()

                # -------------------------------------------------
                # [3장 개편] 1~3순위 친한 친구 선택 및 이유 적기
                # -------------------------------------------------
                st.markdown("### 🤝 [3부] 우리들의 친구 관계 이야기 (1~3순위 & 이유)")
                st.caption("우리 반에서 가장 마음이 편하고 친하게 지내는 친구 3명을 순위별로 적고, 왜 친한지 이유를 남겨주세요.")

                sc1, sc2, sc3 = st.columns(3)
                with sc1:
                    st.markdown("#### 🥇 **1순위 친구**")
                    friend_1 = st.selectbox("1순위 친구 선택", options=["-- 선택하세요 --"] + other_friends, key="f1")
                    reason_1 = st.text_input("친한 이유 (1순위)", placeholder="예: 같이 그림 그리며 놀아요", key="r1")
                with sc2:
                    st.markdown("#### 🥈 **2순위 친구**")
                    friend_2 = st.selectbox("2순위 친구 선택", options=["-- 선택하세요 --"] + other_friends, key="f2")
                    reason_2 = st.text_input("친한 이유 (2순위)", placeholder="예: 착하고 친절해요", key="r2")
                with sc3:
                    st.markdown("#### 🥉 **3순위 친구**")
                    friend_3 = st.selectbox("3순위 친구 선택", options=["-- 선택하세요 --"] + other_friends, key="f3")
                    reason_3 = st.text_input("친한 이유 (3순위)", placeholder="예: 성격이 잘 맞아요", key="r3")

                st.write("")
                c_f1, c_f2 = st.columns(2)
                with c_f1:
                    play_friends = st.multiselect("🏃 자주 함께 노는 친구", options=other_friends, placeholder="복수 선택 가능")
                    want_friends = st.multiselect("🌟 더 친해지고 싶은 친구", options=other_friends, placeholder="복수 선택 가능")
                with c_f2:
                    distant_friends = st.multiselect("💭 조금 멀리 있는(어려운) 친구", options=other_friends, placeholder="선택 사항")

                distant_reason = st.text_input("💔 멀리 있는 친구가 있다면 그 이유를 적어주세요 (선택)", placeholder="선생님만 볼 수 있어요.")

                st.write("")
                st.markdown("#### **친구 관계에서 어려웠던 경험이 있나요?**")
                r_opts = ["😊 특별히 어려운 점 없어요.", "🙋 놀이에 끼고 싶었지만 못 꼈어요.", "🙉 친구들이 내 말을 잘 안 들어줘요.", "⚡ 친구와 자주 다퉈요.", "🏝️ 혼자 있는 시간이 많아요.", "🤐 속상하지만 말하기 어려워요."]
                rel_diff_sel = []
                rc1, rc2 = st.columns(2)
                for idx, opt in enumerate(r_opts):
                    target_col = rc1 if idx % 2 == 0 else rc2
                    if target_col.checkbox(opt, key=f"rd_{idx}") and "특별히" not in opt: rel_diff_sel.append(opt.split(" ", 1)[1])
                rel_diff_etc = st.text_input("✏️ 기타 관계 어려움 직접 적기")

                st.divider()

                st.markdown("### 💌 [4부] 마음속 이야기 & 비밀 편지")
                happy_text = st.text_area("😊 1. 가장 기뻤던 일이나 자랑거리", placeholder="예: 모둠 활동을 잘 해냈어요!", height=70)
                free_text = st.text_area("❤️ 2. 선생님이나 친구에게 하고 싶은 말 / 고민", placeholder="어떤 내용이든 편하게 적어주세요.", height=90)

                secret_opt = st.radio("🔒 비밀 편지 여부", ["😊 괜찮아요. (선생님과 같이 이야기해도 돼요.)", "🔒 비밀이에요! (선생님만 보고 저를 도와주세요.)"])

                if st.form_submit_button("💌 작성 완료! 마음 편지 보내기", type="primary", use_container_width=True):
                    if worry_etc.strip(): worries_sel.append(f"기타: {worry_etc.strip()}")
                    if moment_etc.strip(): moments_sel.append(f"기타: {moment_etc.strip()}")
                    if need_etc.strip(): needs_sel.append(f"기타: {need_etc.strip()}")
                    if rel_diff_etc.strip(): rel_diff_sel.append(f"기타: {rel_diff_etc.strip()}")

                    combined_notes = []
                    if happy_text.strip(): combined_notes.append(f"[기쁜일/자랑] {happy_text.strip()}")
                    if distant_reason.strip(): combined_notes.append(f"[어려운친구이유] {distant_reason.strip()}")
                    if free_text.strip(): combined_notes.append(f"[고민/하고싶은말] {free_text.strip()}")
                    final_free_text = " / ".join(combined_notes) if combined_notes else ""

                    f1_val = friend_1 if friend_1 != "-- 선택하세요 --" else ""
                    f2_val = friend_2 if friend_2 != "-- 선택하세요 --" else ""
                    f3_val = friend_3 if friend_3 != "-- 선택하세요 --" else ""

                    record = {
                        "class_code": active_class,
                        "survey_round": st.session_state["active_round"],
                        "이름": my_name,
                        "마음날씨": weather_map[weather_choice_full],
                        "현재기분": mood_map[mood_choice_full],
                        "깊이있는마음": deep_map[deep_choice_full],
                        "주요고민": ", ".join(worries_sel) if worries_sel else "특별히 없음",
                        "짜증순간": ", ".join(moments_sel) if moments_sel else "없음",
                        "필요한것": ", ".join(needs_sel) if needs_sel else "없음",
                        "친한친구_1순위": f1_val,
                        "친한친구_1순위_이유": reason_1.strip(),
                        "친한친구_2순위": f2_val,
                        "친한친구_2순위_이유": reason_2.strip(),
                        "친한친구_3순위": f3_val,
                        "친한친구_3순위_이유": reason_3.strip(),
                        "자주노는친구": ", ".join(play_friends),
                        "친해지고싶은친구": ", ".join(want_friends),
                        "멀리있는친구": ", ".join(distant_friends),
                        "관계어려움": ", ".join(rel_diff_sel) if rel_diff_sel else "특별히 어려운 점은 없어요.",
                        "점수_즐거움": 4, "점수_힘든친구": 1, "점수_상담대상": 4,
                        "비밀편지여부": "비밀이에요!" if "비밀이에요!" in secret_opt else "괜찮아요",
                        "하고싶은말": final_free_text,
                        "제출방식": "크롬북/태블릿 웹설문",
                        "제출일시": datetime.now(KST).isoformat()
                    }
                    insert_responses([record])
                    st.balloons()
                    st.success("🎉 소중한 마음 이야기가 선생님께 잘 전달되었습니다! 고마워요 ❤️")
    else:
        st.warning("⚠️ 등록된 학생 명렬표가 없습니다. [📋 4. 명렬표·회차·API 키 설정] 메뉴에서 먼저 명렬표를 등록해주세요.")

# =========================================================
# [모드 2] 종이 설문지 AI 스캔
# =========================================================
elif app_mode == "📸 2. 종이 설문지 AI 스캔 (교사용)":
    if require_teacher_auth():
        st.title("📸 종이 설문지 AI 자동 인식 (묶음 배치 OCR)")
        
        if teacher_saved_api_key:
            st.success(f"🔑 현재 **{t_info['teacher_name']}** 선생님의 전용 Gemini API 키가 적용되어 있습니다.")
        else:
            st.warning("⚠️ 현재 학급에 등록된 Gemini API 키가 없습니다. 아래에 본인의 API 키를 입력해 주세요.")

        c_opt1, c_opt2 = st.columns(2)
        with c_opt1: ocr_round = st.text_input("설문 회차", value=st.session_state["active_round"])
        with c_opt2: batch_size = st.slider("1회 호출당 묶음 페이지 수", min_value=5, max_value=15, value=10)

        model_name = "gemini-3.8-flash"
        api_k = st.text_input(f"[{active_class}] 적용될 Gemini API Key", value=teacher_saved_api_key, type="password", key=f"ocr_api_key_{active_class}")
        
        uploaded_files = st.file_uploader("설문지 파일 업로드 (PDF/이미지)", type=["pdf", "png", "jpg", "jpeg"], accept_multiple_files=True)

        if uploaded_files:
            all_images_to_process = []
            for f in uploaded_files:
                file_bytes = f.getvalue()
                if f.name.lower().endswith(".pdf"):
                    try:
                        doc = fitz.open(stream=file_bytes, filetype="pdf")
                        for page in doc:
                            pix = page.get_pixmap(dpi=150)
                            all_images_to_process.append(pix.tobytes("jpeg"))
                    except Exception as e: st.error(f"PDF 변환 오류 ({f.name}): {e}")
                else: all_images_to_process.append(file_bytes)

            total_pages = len(all_images_to_process)
            st.write(f"📄 **총 감지된 페이지 수:** {total_pages}페이지")
            page_range = st.slider("판독할 페이지 범위 선택", min_value=1, max_value=max(1, total_pages), value=(1, max(1, total_pages)))

            if st.button("🤖 묶음 AI 판독 시작", type="primary"):
                if not api_k.strip(): st.error("Gemini API 키를 입력해주세요.")
                else:
                    target_images = all_images_to_process[page_range[0]-1 : page_range[1]]
                    rows = []
                    prog = st.progress(0)
                    status_box = st.empty()
                    client = genai.Client(api_key=api_k.strip())
                    total_batches = (len(target_images) - 1) // batch_size + 1

                    for b_idx in range(total_batches):
                        batch_imgs = target_images[b_idx * batch_size : (b_idx + 1) * batch_size]
                        status_box.info(f"🔄 [{b_idx+1}/{total_batches} 묶음] 분석 중...")

                        contents_payload = [types.Part.from_bytes(data=img_b, mime_type="image/jpeg") for img_b in batch_imgs]
                        prompt = (
                            f"제공된 {len(batch_imgs)}장의 초등학생 설문지 이미지를 순서대로 분석하세요. "
                            f"우리 반 명단[{', '.join(student_list)}]을 참고하여 이름, 1~3순위 친구 이름과 이유, 체크 항목들을 정확히 읽어내어 JSON으로 반환하세요."
                        )
                        contents_payload.append(prompt)

                        for attempt in range(3):
                            try:
                                res = client.models.generate_content(
                                    model=model_name, contents=contents_payload,
                                    config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=BatchSurveyExtraction, temperature=0.1)
                                )
                                parsed_batch = json.loads(res.text).get("results", [])
                                for d in parsed_batch:
                                    rows.append({
                                        "class_code": active_class, "survey_round": ocr_round, "이름": d.get("이름", "미기재"),
                                        "마음날씨": d.get("마음날씨", "구름 조금"), "현재기분": d.get("현재기분", "그냥 보통이에요"),
                                        "깊이있는마음": d.get("깊이있는마음", ""), "주요고민": ", ".join(d.get("주요고민", [])),
                                        "짜증순간": ", ".join(d.get("짜증순간", [])), "필요한것": ", ".join(d.get("필요한것", [])),
                                        "친한친구_1순위": d.get("친한친구_1순위", ""), "친한친구_1순위_이유": d.get("친한친구_1순위_이유", ""),
                                        "친한친구_2순위": d.get("친한친구_2순위", ""), "친한친구_2순위_이유": d.get("친한친구_2순위_이유", ""),
                                        "친한친구_3순위": d.get("친한친구_3순위", ""), "친한친구_3순위_이유": d.get("친한친구_3순위_이유", ""),
                                        "자주노는친구": d.get("자주노는친구", ""), "친해지고싶은친구": d.get("친해지고싶은친구", ""),
                                        "멀리있는친구": d.get("멀리있는친구", ""), "관계어려움": ", ".join(d.get("관계어려움", [])),
                                        "점수_즐거움": 4, "점수_힘든친구": 1, "점수_상담대상": 4,
                                        "비밀편지여부": d.get("비밀편지여부", "괜찮아요"), "하고싶은말": d.get("하고싶은말", ""),
                                        "제출방식": "AI 묶음스캔", "제출일시": datetime.now(KST).isoformat()
                                    })
                                break
                            except Exception as e:
                                if "503" in str(e) and attempt < 2: time.sleep(5); continue
                                else: st.warning(f"⚠️ 오류: {e}"); break
                        prog.progress((b_idx + 1) / total_batches)

                    if rows:
                        st.session_state["ocr_df"] = pd.DataFrame(rows)
                        status_box.success("✅ 판독 완료! 아래 표에서 검수 후 저장하세요.")

        if "ocr_df" in st.session_state and not st.session_state["ocr_df"].empty:
            st.subheader("📝 판독 결과 검수")
            edited_df = st.data_editor(st.session_state["ocr_df"], num_rows="dynamic", use_container_width=True)
            col_b1, col_b2 = st.columns(2)
            with col_b1:
                if st.button("💾 DB 저장", type="primary", use_container_width=True):
                    insert_responses(edited_df.to_dict(orient="records"))
                    st.success("저장 완료!")
            with col_b2:
                st.download_button("📥 엑셀 다운로드", data=to_excel_bytes(edited_df), file_name=f"AI스캔_{ocr_round}.xlsx", use_container_width=True)

# =========================================================
# [모드 3] 통합 분석 대시보드 (성별 구분 소시오그램 & 순위 가중치)
# =========================================================
elif app_mode == "📊 3. 통합 분석 & 변화 추적 (교사용)":
    if require_teacher_auth():
        st.title(f"📊 [{active_class}] 통합 분석 대시보드")
        all_df = load_responses(active_class)

        if all_df.empty:
            st.info("저장된 설문 응답이 없습니다.")
        else:
            rounds = sorted(all_df["survey_round"].unique().tolist(), reverse=True)
            sel_round = st.selectbox("조회 회차 선택", options=rounds)
            round_df = all_df[all_df["survey_round"] == sel_round].drop_duplicates(subset=["이름"], keep="last")
            df = analyze_student_metrics(round_df)

            t1, t2, t3, t4, t5 = st.tabs(["📊 전체 통계", "🕸️ 소시오그램 (성별 구분)", "🔍 위기 스크리닝", "🧒 개별 리포트", "📈 회차 변화 추이"])
            
            with t1:
                c1, c2, c3 = st.columns(3)
                c1.metric("응답 인원", f"{len(df)} / {len(student_list)}명")
                c2.metric("마음 안정", f"{len(df[df['마음날씨'].isin(['아주 맑음', '구름 조금'])])}명")
                c3.metric("비밀 상담 요청", f"{len(df[df['비밀편지여부'] == '비밀이에요!'])}명")
                w_cnt = df["마음날씨"].value_counts().reset_index()
                w_cnt.columns = ["마음날씨", "학생수"]
                st.plotly_chart(px.pie(w_cnt, names="마음날씨", values="학생수", hole=0.4, title="학급 마음 날씨 분포"), use_container_width=True)

            with t2:
                rel_type = st.radio("관계 유형", ["💖 친한 친구 (1~3순위 가중치)", "🏃 자주 함께 노는 친구", "🌟 더 친해지고 싶은 친구", "💔 멀리 있는 친구"], horizontal=True)
                
                # 열 매핑 (1순위~3순위 통합 분석 지원)
                if "친한 친구" in rel_type:
                    col_map_list = [("친한친구_1순위", 3, "#2ECC71"), ("친한친구_2순위", 2, "#27AE60"), ("친한친구_3순위", 1, "#1E8449")]
                else:
                    col_map_dict = {
                        "🏃 자주 함께 노는 친구": ("자주노는친구", 1, "#3498DB"),
                        "🌟 더 친해지고 싶은 친구": ("친해지고싶은친구", 1, "#F1C40F"),
                        "💔 멀리 있는 친구": ("멀리있는친구", 1, "#E74C3C")
                    }
                    col_map_list = [col_map_dict[rel_type]]

                G = nx.DiGraph()
                for _, row in df.iterrows():
                    g_val = student_gender_map.get(row["이름"], "미기재")
                    G.add_node(row["이름"], gender=g_val, weather=row["마음날씨"])

                for _, row in df.iterrows():
                    for t_col, weight, _ in col_map_list:
                        if t_col in row and pd.notna(row[t_col]) and str(row[t_col]).strip() != "":
                            targets = [t.strip() for t in str(row[t_col]).split(",") if t.strip() in G.nodes]
                            for target in targets:
                                if row["이름"] != target:
                                    if G.has_edge(row["이름"], target):
                                        G[row["이름"]][target]["weight"] += weight
                                    else:
                                        G.add_edge(row["이름"], target, weight=weight)

                pos = nx.spring_layout(G, k=0.8, seed=42)
                
                # 에지(선) 그리기
                edge_x, edge_y = [], []
                for u, v, data in G.edges(data=True):
                    edge_x.extend([pos[u][0], pos[v][0], None])
                    edge_y.extend([pos[u][1], pos[v][1], None])

                # 노드 색상 부여 (남: 파란계열, 여: 분홍계열, 미기재: 회색)
                node_colors = []
                node_sizes = []
                in_degrees = dict(G.in_degree(weight='weight'))
                for n in G.nodes():
                    gender = G.nodes[n].get("gender", "미기재")
                    if "남" in gender: node_colors.append("#3498DB")      # 파란색 (남학생)
                    elif "여" in gender: node_colors.append("#E91E63")    # 분홍색 (여학생)
                    else: node_colors.append("#95A5A6")                 # 회색
                    node_sizes.append(22 + in_degrees.get(n, 0) * 3)

                fig = go.Figure(data=[
                    go.Scatter(x=edge_x, y=edge_y, line=dict(width=1.5, color='#888888'), mode='lines', hoverinfo='none'),
                    go.Scatter(
                        x=[pos[n][0] for n in G.nodes()], y=[pos[n][1] for n in G.nodes()],
                        mode='markers+text', text=list(G.nodes()), textposition="top center",
                        marker=dict(size=node_sizes, color=node_colors, line=dict(width=2, color='#333333'))
                    )
                ], layout=go.Layout(showlegend=False, height=520, margin=dict(b=10, l=10, r=10, t=10)))
                st.plotly_chart(fig, use_container_width=True)
                st.caption("🔵 파란색 노드: 남학생 | 핑크색 노드: 여학생 | 노드 크기: 받은 지목 가중치 반영")

                # 자동 결과 해석
                st.divider()
                st.subheader("📋 소시오그램 결과 요약")
                sorted_in = sorted(in_degrees.items(), key=lambda x: x[1], reverse=True)
                top_students = [f"**{k}**({v}점)" for k, v in sorted_in if v > 0][:5]
                st.success("🌟 **교우관계 중심 학생 (인기/추천 리더):** " + (", ".join(top_students) if top_students else "데이터 없음"))

            with t3:
                st.dataframe(df.sort_values(by="위기점수", ascending=False)[["관심단계", "이름", "마음날씨", "현재기분", "친한친구_가중피지목수", "비밀편지여부", "주요고민", "하고싶은말"]], use_container_width=True, hide_index=True)

            with t4:
                sel_stu = st.selectbox("학생 선택", df["이름"].tolist())
                s_row = df[df["이름"] == sel_stu].iloc[0]
                st.markdown(f"### 🧒 **{s_row['이름']}** 학생 상세 리포트 ({s_row['관심단계']})")
                
                c_r1, c_r2 = st.columns(2)
                with c_r1:
                    st.markdown("#### **🌤️ 정서 및 마음 상태**")
                    st.write(f"- **마음 날씨:** {s_row['마음날씨']} / **현재 기분:** {s_row['현재기분']}")
                    st.write(f"- **깊이 있는 마음:** {s_row['깊이있는마음']}")
                    st.write(f"- **주요 고민:** {s_row['주요고민']}")
                    st.write(f"- **짜증나는 순간:** {s_row['짜증순간']}")
                    st.write(f"- **필요한 도움:** {s_row['필요한것']}")
                with c_r2:
                    st.markdown("#### **🤝 1~3순위 교우 관계**")
                    st.write(f"- **🥇 1순위 친구:** {s_row['친한친구_1순위']} (이유: {s_row['친한친구_1순위_이유']})")
                    st.write(f"- **🥈 2순위 친구:** {s_row['친한친구_2순위']} (이유: {s_row['친한친구_2순위_이유']})")
                    st.write(f"- **🥉 3순위 친구:** {s_row['친한친구_3순위']} (이유: {s_row['친한친구_3순위_이유']})")
                    st.write(f"- **자주 노는 친구:** {s_row['자주노는친구']}")
                    st.write(f"- **친해지고 싶은 친구:** {s_row['친해지고싶은친구']}")
                    st.write(f"- **멀리 있는 친구:** {s_row['멀리있는친구']}")
                
                st.info(f"💌 **마음속 이야기 & 하고 싶은 말 ({s_row['비밀편지여부']}):**\n\n{s_row['하고싶은말'] if s_row['하고싶은말'] else '작성된 서술형 내용이 없습니다.'}")

            with t5:
                hist_frames = [analyze_student_metrics(all_df[all_df["survey_round"] == r].drop_duplicates(subset=["이름"], keep="last")) for r in all_df["survey_round"].unique()]
                full_h = pd.concat(hist_frames, ignore_index=True)
                t_stu = st.selectbox("학생 선택", student_list, key="h_stu")
                t_df = full_h[full_h["이름"] == t_stu].sort_values("제출일시")
                if not t_df.empty:
                    st.plotly_chart(px.line(t_df, x="survey_round", y="위기점수", markers=True, title="위기 점수 변화 추이"), use_container_width=True)

# =========================================================
# [모드 4] 명렬표 · 회차 · API 키 설정 (성별 gender 열 추가 양식 지원)
# =========================================================
elif app_mode == "📋 4. 명렬표·회차·API 키 설정 (교사용)":
    if require_teacher_auth():
        st.title(f"📋 [{active_class}] 학급 설정 관리")

        col_s1, col_s2 = st.columns(2)
        with col_s1:
            st.subheader("1️⃣ 설문 회차 설정")
            new_round = st.text_input("현재 회차명", value=st.session_state["active_round"])
            if st.button("회차 저장"):
                st.session_state["active_round"] = new_round
                st.success("회차가 변경되었습니다.")

        with col_s2:
            st.subheader("2️⃣ 내 학급 전용 Gemini API Key 등록/변경")
            updated_key = st.text_input(f"[{active_class}] 개인 Gemini API Key", value=teacher_saved_api_key, type="password", key=f"setting_api_key_{active_class}")
            if st.button("🔑 내 API Key 저장하기"):
                update_teacher_api_key(active_class, updated_key.strip())
                st.success("✅ 학급 전용 API Key가 안전하게 저장되었습니다!")
                st.rerun()

        st.divider()
        st.subheader("3️⃣ 엑셀로 학생 명렬표 일괄 등록 (성별 포함)")
        st.markdown("엑셀 파일에 **`student_no`**(출석번호), **`name`**(이름), **`gender`**(성별: 남/여) 세 개의 열을 포함하여 업로드해 주세요.")

        sample_r = pd.DataFrame({"student_no": [1, 2, 3], "name": ["김철수", "이영희", "박민수"], "gender": ["남", "여", "남"]})
        st.download_button("📥 명렬표 업로드용 엑셀 양식 다운로드", data=to_excel_bytes(sample_r), file_name="명렬표_업로드양식_성별포함.xlsx")

        up_excel = st.file_uploader("작성된 명렬표 엑셀 파일 업로드 (.xlsx)", type=["xlsx"])
        if up_excel is not None:
            up_df = pd.read_excel(up_excel)
            if "student_no" in up_df.columns and "name" in up_df.columns and "gender" in up_df.columns:
                st.success(f"총 {len(up_df)}명의 학생 명렬표가 확인되었습니다.")
                st.dataframe(up_df, use_container_width=True)
                if st.button("💾 이 명렬표를 Supabase DB에 일괄 등록하기", type="primary"):
                    save_roster(active_class, up_df)
                    st.success("✅ 명렬표 등록이 완료되었습니다!")
                    st.rerun()
            else:
                st.error("⚠️ 엑셀 파일 안에 'student_no', 'name', 'gender' 열 이름이 정확히 있어야 합니다.")

        st.divider()
        st.subheader("현재 등록된 학생 명렬표 보기")
        if not roster_df.empty:
            st.dataframe(roster_df, use_container_width=True)
        else:
            st.info("등록된 학생이 없습니다. 위에서 명렬표를 업로드해주세요.")
