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
DEFAULT_GEMINI_API_KEY = get_secret("GEMINI_API_KEY")

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
# 1. Gemini Vision OCR 스키마 (다중 페이지 일괄 추출 지원)
# ---------------------------------------------------------
class SurveyExtraction(BaseModel):
    이름: str = Field(description="학생 이름 (미기재 시 '미기재')") #[cite: 1, 2, 3]
    마음날씨: str = Field(description="'아주 맑음', '구름 조금', '비 옴', '천둥 번개' 중 하나") #[cite: 3]
    현재기분: str = Field(description="'기분이 좋아요', '그냥 보통이에요', '조금 지쳐요', '속상해요', '화가 나요' 중 하나") #[cite: 1]
    깊이있는마음: str = Field(description="요즘 나의 마음과 가장 가까운 문장") #[cite: 1]
    주요고민: list[str] = Field(description="체크된 고민 목록") #[cite: 1, 3]
    짜증순간: list[str] = Field(description="짜증나는 순간 목록") #[cite: 1]
    필요한것: list[str] = Field(description="필요한 것 목록") #[cite: 1]
    친한친구: str = Field(description="가장 친한 친구 이름 (쉼표 구분)") #[cite: 2, 3]
    자주노는친구: str = Field(description="자주 함께 노는 친구 이름 (쉼표 구분)") #[cite: 2, 3]
    친해지고싶은친구: str = Field(description="친해지고 싶은 친구 이름 (쉼표 구분)") #[cite: 2, 3]
    멀리있는친구: str = Field(description="멀리 있는 친구 이름 (쉼표 구분)") #[cite: 2, 3]
    관계어려움: list[str] = Field(description="친구 관계 어려움 항목") #[cite: 2]
    점수_즐거움: int = Field(description="1~5점 (없으면 4)") #[cite: 3]
    점수_힘든친구: int = Field(description="1~5점 (없으면 1)") #[cite: 3]
    점수_상담대상: int = Field(description="1~5점 (없으면 4)") #[cite: 3]
    비밀편지여부: str = Field(description="'비밀이에요!' 또는 '괜찮아요'") #[cite: 2, 3]
    하고싶은말: str = Field(description="서술형 텍스트 전체") #[cite: 1, 2, 3]

class BatchSurveyExtraction(BaseModel):
    results: list[SurveyExtraction] = Field(description="업로드된 각 설문지 이미지 순서대로 추출된 결과 리스트")

# ---------------------------------------------------------
# 2. DB 연동 함수 (교사별 개별 API Key 저장 및 로드 포함)
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

t_info = None
if selected_class != "-- 학급을 선택하세요 --":
    if st.session_state["current_class_code"] != selected_class:
        st.session_state["is_teacher_auth"] = False  # 다른 학급 선택 시 재로그인 요구
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
    new_api_key = st.text_input(
        "개인 Gemini API Key (선택)",
        type="password",
        help="종이 설문지 AI 스캔을 사용하실 선생님은 Google AI Studio에서 발급받은 본인의 API 키를 입력해 주세요."
    )
    if st.form_submit_button("학급 등록하기"):
        if new_code and new_tname and new_pin:
            ok, err = register_teacher(new_code.strip(), new_tname.strip(), new_pin.strip(), new_api_key.strip())
            if ok:
                st.success("등록 완료! 위에서 학급을 선택하세요.")
                st.rerun()
            else: st.error(f"등록 실패: {err}")
        else: st.warning("학급 코드, 성함, 비밀번호는 필수입니다.")

active_class = st.session_state["current_class_code"]
teacher_saved_api_key = (t_info.get("gemini_api_key") or "") if t_info else ""
effective_api_key = teacher_saved_api_key if teacher_saved_api_key else DEFAULT_GEMINI_API_KEY

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
        "📋 4. 명렬표·회차·API 키 설정 (교사용)"
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
                st.subheader("2️⃣ 지금 나의 마음 날씨는 어떤가요?") #[cite: 3]
                c_m1, c_m2 = st.columns(2)
                with c_m1:
                    weather_choice = st.radio("날씨 선택", ["☀️ 아주 맑음", "⛅ 구름 조금", "🌧️ 비 옴", "⛈️ 천둥 번개"]) #[cite: 3]
                    mood_choice = st.radio("기분 선택", ["😄 기분이 좋아요", "🙂 그냥 보통이에요", "😐 조금 지쳐요", "😢 속상해요", "😡 화가 나요"]) #[cite: 1]
                with c_m2:
                    deep_choice = st.radio("마음과 가까운 것", [
                        "평소처럼 괜찮고, 별로 힘든 게 없어요.", #[cite: 1]
                        "가끔씩 짜증이 나거나 기분이 좀 가라앉아요.", #[cite: 1]
                        "자주 짜증이 나고, 마음이 불안해요.", #[cite: 1]
                        "많이 힘들고, 울고 싶을 때가 있어요.", #[cite: 1]
                        "기분이 계속 안 좋고, 아무것도 하기 싫어요." #[cite: 1]
                    ])

                st.divider()
                st.subheader("3️⃣ 요즘 힘들거나 짜증나는 일") #[cite: 1, 3]
                c_w1, c_w2, c_w3 = st.columns(3)
                with c_w1:
                    worries_sel = st.multiselect("걱정되는 것", ["공부가 어렵거나 성적이 걱정돼요.", "숙제나 준비물을 챙기기 힘들어요.", "친구 관계 때문에 속상해요.", "선생님이나 부모님께 혼나는 것이 걱정돼요.", "몸이 피곤하고 에너지가 없어요."]) #[cite: 1, 3]
                    worry_etc = st.text_input("기타 힘든 점") #[cite: 1, 3]
                with c_w2:
                    moments_sel = st.multiselect("짜증나는 순간", ["공부할 때", "숙제나 준비를 할 때", "친구들과 있을 때", "쉬는 시간이나 놀이 시간에", "식사할 때", "집에 있을 때"]) #[cite: 1]
                with c_w3:
                    needs_sel = st.multiselect("필요한 것", ["따뜻한 말과 격려", "친구와 더 많은 시간", "공부에 대한 도움", "충분한 휴식과 수면", "좋아하는 활동(놀이, 취미 등)", "선생님과 이야기하기"]) #[cite: 1]

                st.divider()
                st.subheader("4️⃣ 친구 관계 이야기") #[cite: 2, 3]
                c_f1, c_f2 = st.columns(2)
                with c_f1:
                    close_friends = st.multiselect("💖 친하게 지내는 친구", options=other_friends) #[cite: 2, 3]
                    play_friends = st.multiselect("🏃 자주 함께 노는 친구", options=other_friends) #[cite: 2, 3]
                with c_f2:
                    want_friends = st.multiselect("🌟 더 친해지고 싶은 친구", options=other_friends) #[cite: 2, 3]
                    distant_friends = st.multiselect("💭 조금 멀리 있는(어려운) 친구", options=other_friends) #[cite: 2, 3]

                rel_diff_sel = st.multiselect("친구 관계 어려움", ["특별히 어려운 점은 없어요.", "놀이에 끼고 싶었지만 끼지 못했어요.", "친구들이 내 말을 잘 들어주지 않아요.", "친구와 자주 다투거나 오해가 생겨요.", "혼자 있는 시간이 많아요.", "친구 때문에 속상하지만 말하기 어려워요."]) #[cite: 2]

                sc1, sc2, sc3 = st.columns(3)
                with sc1: s_fun = st.slider("1. 함께 노는 것이 즐겁다", 1, 5, 4) #[cite: 3]
                with sc2: s_hard = st.slider("2. 나를 힘들게 하는 친구가 있다", 1, 5, 1) #[cite: 3]
                with sc3: s_talk = st.slider("3. 이야기할 대상이 있다", 1, 5, 4) #[cite: 3]

                st.divider()
                free_text = st.text_area("하고 싶은 말 / 자랑 / 고민") #[cite: 1, 2, 3]
                secret_opt = st.radio("비밀 편지 여부", ["😊 괜찮아요", "🔒 비밀이에요!"]) #[cite: 2, 3]

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
                        "관계어려움": ", ".join(rel_diff_sel) if rel_diff_sel else "특별히 없음",
                        "점수_즐거움": s_fun, "점수_힘든친구": s_hard, "점수_상담대상": s_talk,
                        "비밀편지여부": "비밀이에요!" if "비밀" in secret_opt else "괜찮아요",
                        "하고싶은말": free_text.strip(),
                        "제출방식": "태블릿 웹설문",
                        "제출일시": datetime.now(KST).isoformat()
                    }
                    insert_responses([record])
                    st.balloons()
                    st.success("제출이 완료되었습니다!")
    else:
        st.warning("⚠️ 등록된 학생 명렬표가 없습니다. [📋 4. 명렬표·회차·API 키 설정] 메뉴에서 먼저 명렬표를 등록해주세요.")

# =========================================================
# [모드 2] 종이 설문지 AI 스캔 (배치 처리 & 교사별 API 키 자동 연동)
# =========================================================
elif app_mode == "📸 2. 종이 설문지 AI 스캔 (교사용)":
    if require_teacher_auth():
        st.title("📸 종이 설문지 AI 자동 인식 (묶음 배치 OCR)")
        if teacher_saved_api_key:
            st.success(f"🔑 현재 **{t_info['teacher_name']}** 선생님의 전용 Gemini API 키가 자동으로 연결되어 있습니다.")
        else:
            st.info("💡 전용 API 키가 아직 등록되지 않았습니다. 아래에 직접 입력하거나 [📋 4. 설정] 메뉴에서 본인의 API 키를 저장해 두시면 매번 입력할 필요가 없습니다.")
        
        c_opt1, c_opt2, c_opt3 = st.columns(3)
        with c_opt1:
            ocr_round = st.text_input("설문 회차", value=st.session_state["active_round"])
        with c_opt2:
            model_name = st.selectbox(
                "사용할 AI 모델 (한도 초과 시 변경)",
                ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-3.8-flash"]
            )
        with c_opt3:
            batch_size = st.slider("1회 호출당 묶음 페이지 수", min_value=5, max_value=15, value=10)

        api_k = st.text_input("적용될 Gemini API Key", value=effective_api_key, type="password")
        
        uploaded_files = st.file_uploader(
            "설문지 파일 업로드 (PDF 또는 이미지 다중 선택 가능)",
            type=["pdf", "png", "jpg", "jpeg"],
            accept_multiple_files=True
        )

        if uploaded_files:
            all_images_to_process = []
            for f in uploaded_files:
                file_bytes = f.getvalue()
                if f.name.lower().endswith(".pdf"):
                    try:
                        doc = fitz.open(stream=file_bytes, filetype="pdf")
                        for page in doc:
                            pix = page.get_pixmap(dpi=130)
                            all_images_to_process.append(pix.tobytes("jpeg"))
                    except Exception as e:
                        st.error(f"PDF 변환 오류 ({f.name}): {e}")
                else:
                    all_images_to_process.append(file_bytes)

            total_pages = len(all_images_to_process)
            st.write(f"📄 **총 감지된 페이지 수:** {total_pages}페이지 (예상 API 호출 횟수: **{(total_pages - 1) // batch_size + 1}회**)")

            page_range = st.slider(
                "판독할 페이지 범위 선택",
                min_value=1, max_value=max(1, total_pages), value=(1, max(1, total_pages))
            )

            if st.button("🤖 묶음 AI 판독 시작", type="primary"):
                if not api_k:
                    st.error("API 키를 입력해주세요.")
                else:
                    target_images = all_images_to_process[page_range[0]-1 : page_range[1]]
                    rows = []
                    prog = st.progress(0)
                    status_box = st.empty()
                    client = genai.Client(api_key=api_k)
                    
                    total_batches = (len(target_images) - 1) // batch_size + 1

                    for b_idx in range(total_batches):
                        batch_imgs = target_images[b_idx * batch_size : (b_idx + 1) * batch_size]
                        start_p = page_range[0] + b_idx * batch_size
                        end_p = start_p + len(batch_imgs) - 1
                        status_box.info(f"🔄 [{b_idx+1}/{total_batches} 묶음] {start_p}~{end_p}페이지 일괄 분석 중...")

                        contents_payload = [
                            types.Part.from_bytes(data=img_b, mime_type="image/jpeg")
                            for img_b in batch_imgs
                        ]
                        prompt = (
                            f"제공된 {len(batch_imgs)}장의 초등학생 설문지 이미지를 순서대로 빠짐없이 분석하세요. "
                            f"우리 반 명단[{', '.join(student_list)}]을 참고하여 손글씨 이름을 정확히 매칭하고, "
                            f"results 리스트에 각 페이지의 추출 결과를 담아 JSON으로 반환하세요."
                        )
                        contents_payload.append(prompt)

                        for attempt in range(3):
                            try:
                                res = client.models.generate_content(
                                    model=model_name,
                                    contents=contents_payload,
                                    config=types.GenerateContentConfig(
                                        response_mime_type="application/json",
                                        response_schema=BatchSurveyExtraction,
                                        temperature=0.1
                                    )
                                )
                                parsed_batch = json.loads(res.text).get("results", [])
                                for d in parsed_batch:
                                    rows.append({
                                        "class_code": active_class, "survey_round": ocr_round, "이름": d.get("이름", "미기재"),
                                        "마음날씨": d.get("마음날씨", "구름 조금"), "현재기분": d.get("현재기분", "그냥 보통이에요"),
                                        "깊이있는마음": d.get("깊이있는마음", ""),
                                        "주요고민": ", ".join(d.get("주요고민", [])), "짜증순간": ", ".join(d.get("짜증순간", [])),
                                        "필요한것": ", ".join(d.get("필요한것", [])), "친한친구": d.get("친한친구", ""),
                                        "자주노는친구": d.get("자주노는친구", ""), "친해지고싶은친구": d.get("친해지고싶은친구", ""),
                                        "멀리있는친구": d.get("멀리있는친구", ""), "관계어려움": ", ".join(d.get("관계어려움", [])),
                                        "점수_즐거움": d.get("점수_즐거움", 4), "점수_힘든친구": d.get("점수_힘든친구", 1),
                                        "점수_상담대상": d.get("점수_상담대상", 4), "비밀편지여부": d.get("비밀편지여부", "괜찮아요"),
                                        "하고싶은말": d.get("하고싶은말", ""), "제출방식": "AI 묶음스캔", "제출일시": datetime.now(KST).isoformat()
                                    })
                                break
                            except Exception as e:
                                if "503" in str(e) and attempt < 2:
                                    time.sleep(5)
                                    continue
                                else:
                                    st.warning(f"⚠️ {start_p}~{end_p}페이지 묶음 처리 중 오류: {e}")
                                    break

                        prog.progress((b_idx + 1) / total_batches)
                        if b_idx < total_batches - 1:
                            time.sleep(3)

                    if rows:
                        st.session_state["ocr_df"] = pd.DataFrame(rows)
                        status_box.success(f"✅ 총 {len(rows)}건의 설문 판독이 완료되었습니다! 아래 표에서 검수 후 저장하세요.")

        if "ocr_df" in st.session_state and not st.session_state["ocr_df"].empty:
            st.subheader("📝 판독 결과 검수 (더블클릭하여 수정 가능)")
            edited_df = st.data_editor(st.session_state["ocr_df"], num_rows="dynamic", use_container_width=True)
            col_b1, col_b2 = st.columns(2)
            with col_b1:
                if st.button("💾 검수 완료 데이터 DB 저장", type="primary", use_container_width=True):
                    insert_responses(edited_df.to_dict(orient="records"))
                    st.success("저장 완료!")
            with col_b2:
                st.download_button(
                    "📥 엑셀(.xlsx)로 다운로드",
                    data=to_excel_bytes(edited_df),
                    file_name=f"AI스캔결과_{ocr_round}.xlsx",
                    use_container_width=True
                )

# =========================================================
# [모드 3] 통합 분석 대시보드
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

            t1, t2, t3, t4, t5 = st.tabs(["📊 전체 통계", "🕸️ 소시오그램", "🔍 위기 스크리닝", "🧒 개별 리포트", "📈 회차 변화 추이"])
            
            with t1:
                c1, c2, c3 = st.columns(3)
                c1.metric("응답 인원", f"{len(df)} / {len(student_list)}명")
                c2.metric("마음 안정", f"{len(df[df['마음날씨'].isin(['아주 맑음', '구름 조금'])])}명")
                c3.metric("비밀 상담", f"{len(df[df['비밀편지여부'] == '비밀이에요!'])}명")
                
                w_cnt = df["마음날씨"].value_counts().reset_index()
                w_cnt.columns = ["마음날씨", "학생수"]
                st.plotly_chart(px.pie(w_cnt, names="마음날씨", values="학생수", hole=0.4), use_container_width=True)

            with t2:
                rel_type = st.radio("관계 유형", ["💖 가장 친한 친구", "🏃 자주 함께 노는 친구", "🌟 더 친해지고 싶은 친구", "💔 멀리 있는 친구"], horizontal=True)
                col_map = {"💖 가장 친한 친구": ("친한친구", "#2ECC71"), "🏃 자주 함께 노는 친구": ("자주노는친구", "#3498DB"), "🌟 더 친해지고 싶은 친구": ("친해지고싶은친구", "#F1C40F"), "💔 멀리 있는 친구": ("멀리있는친구", "#E74C3C")}
                t_col, e_col = col_map[rel_type]

                G = nx.DiGraph()
                for _, row in df.iterrows(): G.add_node(row["이름"], weather=row["마음날씨"])
                for _, row in df.iterrows():
                    for target in [t.strip() for t in str(row[t_col]).split(",") if t.strip() in G.nodes]:
                        G.add_edge(row["이름"], target)

                pos = nx.spring_layout(G, seed=42)
                edge_x, edge_y = [], []
                for u, v in G.edges():
                    edge_x.extend([pos[u][0], pos[v][0], None])
                    edge_y.extend([pos[u][1], pos[v][1], None])
                
                fig = go.Figure(data=[
                    go.Scatter(x=edge_x, y=edge_y, line=dict(width=2, color=e_col), mode='lines', hoverinfo='none'),
                    go.Scatter(x=[pos[n][0] for n in G.nodes()], y=[pos[n][1] for n in G.nodes()], mode='markers+text', text=list(G.nodes()), textposition="top center", marker=dict(size=25, color='#FFD93D'))
                ], layout=go.Layout(showlegend=False, height=500, margin=dict(b=0, l=0, r=0, t=0)))
                st.plotly_chart(fig, use_container_width=True)

            with t3:
                st.dataframe(df.sort_values(by="위기점수", ascending=False)[["관심단계", "이름", "마음날씨", "친한친구_피지목수", "비밀편지여부", "주요고민", "하고싶은말"]], use_container_width=True, hide_index=True)

            with t4:
                sel_stu = st.selectbox("학생 선택", df["이름"].tolist())
                s_row = df[df["이름"] == sel_stu].iloc[0]
                st.write(f"### **{s_row['이름']}** ({s_row['관심단계']})")
                st.write(f"- **고민:** {s_row['주요고민']} / **하고 싶은 말:** {s_row['하고싶은말']}")

            with t5:
                hist_frames = [analyze_student_metrics(all_df[all_df["survey_round"] == r].drop_duplicates(subset=["이름"], keep="last")) for r in all_df["survey_round"].unique()]
                full_h = pd.concat(hist_frames, ignore_index=True)
                t_stu = st.selectbox("학생 선택", student_list, key="h_stu")
                t_df = full_h[full_h["이름"] == t_stu].sort_values("제출일시")
                if not t_df.empty:
                    st.plotly_chart(px.line(t_df, x="survey_round", y="위기점수", markers=True, title="위기 점수 변화 추이"), use_container_width=True)

# =========================================================
# [모드 4] 명렬표 · 설문 회차 · 교사 개별 API 키 설정
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
            st.caption("다른 선생님과 API 무료 한도가 겹치지 않도록 본인의 API 키를 저장해 두세요.")
            updated_key = st.text_input("개인 Gemini API Key", value=teacher_saved_api_key, type="password")
            if st.button("🔑 내 API Key 저장하기"):
                update_teacher_api_key(active_class, updated_key.strip())
                st.success("✅ 학급 전용 API Key가 저장되었습니다!")
                st.rerun()

        st.divider()
        st.subheader("3️⃣ 엑셀로 학생 명렬표 일괄 등록")
        st.markdown("엑셀 파일에 **`student_no`**(출석번호)와 **`name`**(이름) 두 개의 열(Column)만 포함하여 업로드해 주세요.")

        sample_r = pd.DataFrame({"student_no": [1, 2, 3], "name": ["김철수", "이영희", "박민수"]})
        st.download_button("📥 명렬표 업로드용 엑셀 양식 다운로드", data=to_excel_bytes(sample_r), file_name="명렬표_업로드양식.xlsx")

        up_excel = st.file_uploader("작성된 명렬표 엑셀 파일 업로드 (.xlsx)", type=["xlsx"])
        if up_excel is not None:
            up_df = pd.read_excel(up_excel)
            if "student_no" in up_df.columns and "name" in up_df.columns:
                st.success(f"총 {len(up_df)}명의 학생 명렬표가 확인되었습니다.")
                st.dataframe(up_df, use_container_width=True)
                if st.button("💾 이 명렬표를 Supabase DB에 일괄 등록하기", type="primary"):
                    save_roster(active_class, up_df)
                    st.success("✅ 명렬표 등록이 완료되었습니다! 이제 학생용 설문에서 드롭다운으로 선택할 수 있습니다.")
                    st.rerun()
            else:
                st.error("⚠️ 엑셀 파일 안에 'student_no'와 'name' 열 이름이 정확히 있어야 합니다.")

        st.divider()
        st.subheader("현재 등록된 학생 명렬표 보기")
        if not roster_df.empty:
            st.dataframe(roster_df, use_container_width=True)
        else:
            st.info("등록된 학생이 없습니다. 위에서 엑셀 파일을 업로드해주세요.")
