"""
마음날씨 — 우리 반 마음건강 & 교우관계 통합 관리 시스템
v2.0.0

주요 변경 사항 (v1 → v2)
- 브랜드 디자인 시스템(Pretendard, 카드 UI, 일관된 차트 팔레트)
- 학생 화면 / 교사 화면 완전 분리 (교사 로그인 시 학생 메뉴 숨김)
- 학급 목록 노출 제거 → 학급 코드(또는 QR/링크)로 입장
- 학급 등록 시 기존 학급 덮어쓰기(upsert) 취약점 제거, 고유 코드 자동 발급
- PIN 해시 저장(PBKDF2), Gemini API 키 암호화 저장(Fernet)
- 로그인 시도 제한, 교사 세션 자동 로그아웃
- 비밀 편지를 별도 테이블로 분리 (설문 분석 데이터 오염 버그 수정)
- 설문 회차를 DB에 저장 (교사가 바꾼 회차가 학생 기기에도 반영)
- 학교생활 점수 3문항 실제 수집 (기존 고정값 4/1/4 버그 수정)
- OCR 이름 자동 보정, 이미지 축소로 비용 절감
- 미응답 학생, 받은 지목 목록, 학급 추이, 데이터 파기 기능 추가
"""

import os
import json
import time
import hmac
import html
import string
import hashlib
import difflib
import secrets as pysecrets
from io import BytesIO
from datetime import datetime, timezone, timedelta

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import networkx as nx
from PIL import Image, ImageOps
import fitz  # PyMuPDF
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from supabase import create_client, Client

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # 암호화 라이브러리가 없으면 API 키 서버 저장 비활성화
    Fernet, InvalidToken = None, Exception

try:
    import qrcode
except ImportError:
    qrcode = None


# =========================================================
# 0. 기본 설정
# =========================================================
APP_NAME = "마음날씨"
APP_TAGLINE = "우리 반 마음건강 · 교우관계 통합 관리"
APP_VERSION = "2.0.0"
SUPPORT_CONTACT = "support@example.com"  # 판매 시 실제 문의처로 변경

KST = timezone(timedelta(hours=9))
TEACHER_SESSION_MINUTES = 30
MAX_LOGIN_ATTEMPTS = 5
LOCK_SECONDS = 60
MAX_RISK_SCORE = 29

st.set_page_config(
    page_title=f"{APP_NAME} | {APP_TAGLINE}",
    page_icon="🌤️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def get_secret(key, default=""):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key, default)


SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_KEY = get_secret("SUPABASE_KEY")
GEMINI_MODEL = get_secret("GEMINI_MODEL", "gemini-3.8-flash")
APP_BASE_URL = get_secret("APP_BASE_URL", "")
ENC_KEY = get_secret("APP_ENCRYPTION_KEY", "")

# 색상 팔레트
C = {
    "primary": "#4F6BED", "primary_dark": "#3A52C9", "primary_soft": "#EEF1FE",
    "accent": "#FF8A65", "ok": "#22A06B", "warn": "#E2A03F", "danger": "#E5484D",
    "ink": "#1F2937", "muted": "#6B7280", "line": "#E5E7EB", "bg": "#F7F8FC",
    "boy": "#4F8EF7", "girl": "#F06292", "unknown": "#A0AEC0",
}
CHART_SEQ = ["#4F6BED", "#FF8A65", "#22A06B", "#E2A03F", "#9B7BEA", "#26B5C9", "#E5484D"]
WEATHER_COLORS = {"아주 맑음": "#F6C445", "구름 조금": "#8EC5FC", "비 옴": "#5B8DEF", "천둥 번개": "#6B4FBB"}
CARE_COLORS = {"🔴 우선 상담": C["danger"], "🟡 관찰 필요": C["warn"], "🟢 안정": C["ok"]}


# =========================================================
# 1. 디자인 시스템
# =========================================================
def inject_css():
    st.markdown(
        f"""
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css');

html, body, .stApp, .stApp p, .stApp li, .stApp label, .stApp div, .stApp input,
.stApp textarea, .stApp button, .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5,
.stApp span:not([data-testid="stIconMaterial"]) {{
  font-family: 'Pretendard Variable', Pretendard, -apple-system, BlinkMacSystemFont, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
}}
.stApp {{ background: {C['bg']}; color: {C['ink']}; }}
#MainMenu, footer {{ visibility: hidden; }}
header[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ padding-top: 2rem; padding-bottom: 4rem; max-width: 1200px; }}
h1, h2, h3 {{ letter-spacing: -0.02em; }}

/* 사이드바 */
[data-testid="stSidebar"] {{ background: #FFFFFF; border-right: 1px solid {C['line']}; }}
.brand {{ display:flex; align-items:center; gap:10px; padding: 4px 0 14px; border-bottom:1px solid {C['line']}; margin-bottom: 14px; }}
.brand-logo {{ width:40px; height:40px; border-radius:12px; display:flex; align-items:center; justify-content:center;
  font-size:22px; background: linear-gradient(135deg, {C['primary']}, #8E7CF3); }}
.brand-name {{ font-weight:800; font-size:1.15rem; color:{C['ink']}; line-height:1.1; }}
.brand-sub {{ font-size:.74rem; color:{C['muted']}; }}
.class-card {{ background:{C['primary_soft']}; border-radius:14px; padding:12px 14px; margin-bottom:12px; }}
.class-card .t {{ font-weight:700; color:{C['primary_dark']}; }}
.class-card .s {{ font-size:.8rem; color:{C['muted']}; margin-top:2px; }}

/* 히어로 */
.hero {{ display:flex; gap:18px; align-items:center; padding: 26px 30px; border-radius: 22px; margin-bottom: 22px;
  background: linear-gradient(120deg, {C['primary']} 0%, #7B6CF0 55%, #A98BF5 100%); color:#fff;
  box-shadow: 0 10px 30px rgba(79,107,237,.25); }}
.hero-icon {{ font-size: 46px; line-height:1; }}
.hero h1 {{ color:#fff; font-size: 1.75rem; font-weight: 800; margin:0; padding:0; }}
.hero p {{ color: rgba(255,255,255,.92); margin: 6px 0 0; font-size: .98rem; }}
.hero.soft {{ background: linear-gradient(120deg, #FFF5EE, #FFFFFF); color:{C['ink']}; box-shadow:none; border:1px solid {C['line']}; }}
.hero.soft h1 {{ color:{C['ink']}; }} .hero.soft p {{ color:{C['muted']}; }}

/* 카드 */
.card {{ background:#fff; border:1px solid {C['line']}; border-radius:18px; padding:20px 22px; height:100%;
  box-shadow: 0 1px 2px rgba(16,24,40,.04); }}
.card h4 {{ margin: 6px 0 6px; font-size:1.05rem; }}
.card p {{ color:{C['muted']}; font-size:.9rem; margin:0; }}
.card .ico {{ font-size: 28px; }}

/* KPI */
.kpi {{ background:#fff; border:1px solid {C['line']}; border-radius:16px; padding:14px 16px; border-left: 5px solid {C['primary']}; }}
.kpi-label {{ font-size:.82rem; color:{C['muted']}; font-weight:600; }}
.kpi-value {{ font-size:1.7rem; font-weight:800; color:{C['ink']}; margin-top:2px; }}
.kpi-sub {{ font-size:.78rem; color:{C['muted']}; }}
.kpi-ok {{ border-left-color:{C['ok']}; }} .kpi-warn {{ border-left-color:{C['warn']}; }}
.kpi-danger {{ border-left-color:{C['danger']}; }} .kpi-accent {{ border-left-color:{C['accent']}; }}

/* 단계 헤더 */
.step {{ display:flex; gap:12px; align-items:flex-start; margin: 2px 0 14px; }}
.step .num {{ min-width:32px; height:32px; border-radius:50%; background:{C['primary']}; color:#fff;
  display:flex; align-items:center; justify-content:center; font-weight:800; }}
.step .tt {{ font-weight:800; font-size:1.08rem; }}
.step .dd {{ color:{C['muted']}; font-size:.88rem; margin-top:2px; }}

/* 배지 */
.badge {{ display:inline-block; padding:3px 10px; border-radius:999px; font-size:.76rem; font-weight:700; margin-right:4px; }}
.b-primary {{ background:{C['primary_soft']}; color:{C['primary_dark']}; }}
.b-ok {{ background:#E3F5EC; color:#167A50; }} .b-warn {{ background:#FCF1DE; color:#9A6614; }}
.b-danger {{ background:#FDE7E8; color:#B4262B; }} .b-muted {{ background:#F1F2F4; color:{C['muted']}; }}

/* 위젯 */
.stButton > button, [data-testid="stFormSubmitButton"] > button, .stDownloadButton > button {{
  border-radius: 12px; font-weight: 700; padding: .55rem 1.1rem; transition: all .15s ease; }}
.stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover {{ transform: translateY(-1px); }}
.stTabs [data-baseweb="tab-list"] {{ gap: 6px; border-bottom: 1px solid {C['line']}; }}
.stTabs [data-baseweb="tab"] {{ border-radius: 12px 12px 0 0; padding: 10px 16px; font-weight: 700; }}
[data-testid="stMetric"] {{ background:#fff; border:1px solid {C['line']}; border-radius:16px; padding:14px 16px; }}
[data-testid="stExpander"] {{ background:#fff; border-radius:14px; }}

.legend-chip {{ display:inline-flex; align-items:center; gap:6px; margin-right:14px; font-size:.82rem; color:{C['muted']}; }}
.legend-dot {{ width:12px; height:12px; border-radius:50%; display:inline-block; }}
.app-footer {{ text-align:center; color:{C['muted']}; font-size:.78rem; margin-top: 3rem; padding-top: 1rem; border-top:1px solid {C['line']}; }}
.help-box {{ background:#FFF8F3; border:1px solid #FFE1D1; border-radius:14px; padding:12px 16px; font-size:.88rem; }}
</style>
""",
        unsafe_allow_html=True,
    )


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def hero(title, subtitle, icon="🌤️", soft=False):
    st.markdown(
        f'<div class="hero{" soft" if soft else ""}"><div class="hero-icon">{icon}</div>'
        f"<div><h1>{esc(title)}</h1><p>{esc(subtitle)}</p></div></div>",
        unsafe_allow_html=True,
    )


def step_header(num, title, desc=""):
    st.markdown(
        f'<div class="step"><div class="num">{num}</div><div><div class="tt">{esc(title)}</div>'
        f'{f"<div class=dd>{esc(desc)}</div>" if desc else ""}</div></div>',
        unsafe_allow_html=True,
    )


def badge(text, tone="primary"):
    return f'<span class="badge b-{tone}">{esc(text)}</span>'


def kpi_row(items):
    cols = st.columns(len(items))
    for col, (label, value, sub, tone) in zip(cols, items):
        col.markdown(
            f'<div class="kpi kpi-{tone}"><div class="kpi-label">{esc(label)}</div>'
            f'<div class="kpi-value">{esc(value)}</div><div class="kpi-sub">{esc(sub)}</div></div>',
            unsafe_allow_html=True,
        )


def feature_card(icon, title, desc):
    st.markdown(
        f'<div class="card"><div class="ico">{icon}</div><h4>{esc(title)}</h4><p>{esc(desc)}</p></div>',
        unsafe_allow_html=True,
    )


def style_fig(fig, height=None):
    fig.update_layout(
        font=dict(family="Pretendard Variable, Pretendard, sans-serif", color=C["ink"], size=13),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=50, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=-0.25, xanchor="center", x=0.5),
        title=dict(font=dict(size=16)),
    )
    if height:
        fig.update_layout(height=height)
    return fig


def footer():
    st.markdown(
        f'<div class="app-footer">{APP_NAME} v{APP_VERSION} · 학생 응답은 진단이 아닌 상담 참고 자료입니다 · 문의 {esc(SUPPORT_CONTACT)}</div>',
        unsafe_allow_html=True,
    )


inject_css()


# =========================================================
# 2. 보안 유틸 (PIN 해시 / API 키 암호화)
# =========================================================
def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    iters = 200_000
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iters)
    return f"pbkdf2${iters}${salt.hex()}${dk.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    if not stored or not pin:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, iters, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt_hex), int(iters))
            return hmac.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    return hmac.compare_digest(pin, stored)  # v1 평문 PIN 호환 (로그인 시 자동 해시 전환)


_fernet = None
if Fernet and ENC_KEY:
    try:
        _fernet = Fernet(ENC_KEY.encode() if isinstance(ENC_KEY, str) else ENC_KEY)
    except Exception:
        _fernet = None


def encrypt_text(s: str):
    if not s:
        return ""
    if _fernet is None:
        return None
    return "enc:" + _fernet.encrypt(s.encode()).decode()


def decrypt_text(s: str) -> str:
    if not s:
        return ""
    if s.startswith("enc:"):
        if _fernet is None:
            return ""
        try:
            return _fernet.decrypt(s[4:].encode()).decode()
        except InvalidToken:
            return ""
    return s  # v1 평문 키 호환


def gen_class_code(n=6) -> str:
    alphabet = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1IL")
    return "".join(pysecrets.choice(alphabet) for _ in range(n))


# =========================================================
# 3. DB 계층
# =========================================================
@st.cache_resource
def init_supabase() -> Client | None:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            return create_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception:
            return None
    return None


supabase = init_supabase()


def db() -> Client:
    if supabase is None:
        hero("데이터베이스 연결 필요", "SUPABASE_URL / SUPABASE_KEY 설정을 확인해 주세요.", "🔌", soft=True)
        st.stop()
    return supabase


def _fetch_class_row(code: str):
    res = db().table("teacher_classes").select("*").eq("class_code", code).limit(1).execute()
    return res.data[0] if res.data else None


@st.cache_data(ttl=60, show_spinner=False)
def get_class_public(code: str):
    """비밀정보(PIN, API 키)를 제외한 학급 정보"""
    try:
        row = _fetch_class_row(code)
    except Exception:
        return None
    if not row:
        return None
    return {k: v for k, v in row.items() if k not in ("pin_code", "gemini_api_key")}


def class_title(cls: dict) -> str:
    name = cls.get("class_name") or cls["class_code"]
    school = cls.get("school_name") or ""
    return f"{school} {name}".strip()


def default_round_name() -> str:
    now = datetime.now(KST)
    return f"{now.year}년 {now.month}월 설문"


def active_round_of(cls: dict) -> str:
    return (cls or {}).get("active_round") or default_round_name()


def register_class(school, class_name, teacher_name, pin):
    code = None
    for _ in range(8):
        cand = gen_class_code()
        if _fetch_class_row(cand) is None:
            code = cand
            break
    if not code:
        return None, "코드 생성에 실패했습니다. 다시 시도해 주세요."
    try:
        db().table("teacher_classes").insert({
            "class_code": code, "school_name": school, "class_name": class_name,
            "teacher_name": teacher_name, "pin_code": hash_pin(pin),
            "gemini_api_key": "", "active_round": default_round_name(),
        }).execute()
    except Exception as e:
        return None, f"등록 실패: {e} (schema.sql 마이그레이션을 실행했는지 확인하세요)"
    get_class_public.clear()
    return code, None


def update_class(code, fields: dict):
    db().table("teacher_classes").update(fields).eq("class_code", code).execute()
    get_class_public.clear()


def verify_teacher(code, pin) -> bool:
    row = _fetch_class_row(code)
    if not row:
        return False
    stored = row.get("pin_code") or ""
    ok = verify_pin(pin, stored)
    if ok and not stored.startswith("pbkdf2$"):
        try:
            update_class(code, {"pin_code": hash_pin(pin)})  # 평문 → 해시 자동 전환
        except Exception:
            pass
    return ok


def get_api_key(code) -> str:
    row = _fetch_class_row(code)
    return decrypt_text((row or {}).get("gemini_api_key") or "").strip()


def save_api_key(code, key) -> bool:
    enc = encrypt_text(key.strip())
    if enc is None:
        return False
    update_class(code, {"gemini_api_key": enc})
    return True


@st.cache_data(ttl=60, show_spinner=False)
def load_roster(code: str) -> pd.DataFrame:
    try:
        res = db().table("student_roster").select("student_no, name, gender").eq("class_code", code).order("student_no").execute()
        if res.data:
            return pd.DataFrame(res.data)
    except Exception:
        pass
    return pd.DataFrame(columns=["student_no", "name", "gender"])


def normalize_gender(v) -> str:
    s = str(v).strip().lower()
    if s in ("남", "남자", "m", "male", "boy", "남학생"):
        return "남"
    if s in ("여", "여자", "f", "female", "girl", "여학생"):
        return "여"
    return "미기재"


def save_roster(code: str, df: pd.DataFrame):
    records, seen = [], set()
    for _, row in df.iterrows():
        try:
            s_no = int(row["student_no"])
        except Exception:
            continue
        name = str(row["name"]).strip()
        if not name or name == "nan" or name in seen:
            continue
        seen.add(name)
        records.append({"class_code": code, "student_no": s_no, "name": name, "gender": normalize_gender(row.get("gender", ""))})
    if not records:
        raise ValueError("등록할 학생이 없습니다.")
    db().table("student_roster").delete().eq("class_code", code).execute()
    db().table("student_roster").insert(records).execute()
    load_roster.clear()
    return len(records)


@st.cache_data(ttl=20, show_spinner=False)
def load_responses(code: str) -> pd.DataFrame:
    try:
        res = db().table("survey_responses").select("*").eq("class_code", code).order("제출일시", desc=False).execute()
        if res.data:
            return pd.DataFrame(res.data).fillna("")
    except Exception:
        pass
    return pd.DataFrame()


def insert_responses(records: list[dict]):
    db().table("survey_responses").insert(records).execute()
    load_responses.clear()


@st.cache_data(ttl=20, show_spinner=False)
def load_secret_letters(code: str) -> pd.DataFrame:
    try:
        res = db().table("secret_letters").select("*").eq("class_code", code).order("created_at", desc=True).execute()
        return pd.DataFrame(res.data or [])
    except Exception:
        return pd.DataFrame()


def insert_secret_letter(code, name, content):
    db().table("secret_letters").insert({"class_code": code, "student_name": name, "content": content}).execute()
    load_secret_letters.clear()


def mark_letter_read(letter_id, value=True):
    db().table("secret_letters").update({"is_read": value}).eq("id", letter_id).execute()
    load_secret_letters.clear()


def delete_round(code, round_name):
    db().table("survey_responses").delete().eq("class_code", code).eq("survey_round", round_name).execute()
    load_responses.clear()


def delete_all_data(code):
    db().table("survey_responses").delete().eq("class_code", code).execute()
    try:
        db().table("secret_letters").delete().eq("class_code", code).execute()
    except Exception:
        pass
    load_responses.clear()
    load_secret_letters.clear()


def to_excel_bytes(df: pd.DataFrame, sheet="설문결과"):
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=sheet)
    return out.getvalue()


# =========================================================
# 4. 분석 로직
# =========================================================
WEATHER_SCORE = {"아주 맑음": 0, "구름 조금": 1, "비 옴": 3, "천둥 번개": 4}
MOOD_SCORE = {"기분이 좋아요": 0, "그냥 보통이에요": 1, "조금 지쳐요": 2, "속상해요": 3, "화가 나요": 4}
DEEP_SCORE = {
    "평소처럼 괜찮고, 별로 힘든 게 없어요.": 0,
    "가끔씩 짜증이 나거나 기분이 좀 가라앉아요.": 1,
    "자주 짜증이 나고, 마음이 불안해요.": 3,
    "많이 힘들고, 울고 싶을 때가 있어요.": 4,
    "기분이 계속 안 좋고, 아무것도 하기 싫어요.": 5,
}
DEEP_ALERT = {"많이 힘들고, 울고 싶을 때가 있어요.", "기분이 계속 안 좋고, 아무것도 하기 싫어요."}
CLOSE_COLS = [("친한친구_1순위", 3), ("친한친구_2순위", 2), ("친한친구_3순위", 1)]


def split_names(v) -> list[str]:
    if v is None:
        return []
    s = str(v).strip()
    if s in ("", "-", "nan", "None"):
        return []
    return [t.strip() for t in s.split(",") if t.strip()]


def to_int(v, default, lo=1, hi=5) -> int:
    try:
        return max(lo, min(hi, int(float(v))))
    except Exception:
        return default


def survey_rows(all_df: pd.DataFrame) -> pd.DataFrame:
    """비밀편지(v1 방식) 행을 제외한 설문 데이터"""
    if all_df.empty:
        return all_df
    d = all_df
    if "제출방식" in d.columns:
        d = d[d["제출방식"] != "비밀편지함"]
    return d


def round_latest(all_df: pd.DataFrame, round_name: str) -> pd.DataFrame:
    d = survey_rows(all_df)
    if d.empty:
        return d
    d = d[d["survey_round"] == round_name]
    return d.sort_values("제출일시").drop_duplicates(subset=["이름"], keep="last")


def ordered_rounds(all_df: pd.DataFrame) -> list[str]:
    d = survey_rows(all_df)
    if d.empty:
        return []
    return d.groupby("survey_round")["제출일시"].min().sort_values().index.tolist()


def analyze_student_metrics(df: pd.DataFrame, roster_names=None) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    names = set(df["이름"]) | set(roster_names or [])
    close = dict.fromkeys(names, 0)
    want = dict.fromkeys(names, 0)
    distant = dict.fromkeys(names, 0)

    for _, row in df.iterrows():
        src = row["이름"]
        for col, w in CLOSE_COLS:
            t = str(row.get(col, "")).strip()
            if t in close and t != src:
                close[t] += w
        for t in split_names(row.get("친해지고싶은친구")):
            if t in want and t != src:
                want[t] += 1
        for t in split_names(row.get("멀리있는친구")):
            if t in distant and t != src:
                distant[t] += 1

    df["친한친구_가중피지목수"] = df["이름"].map(close).fillna(0).astype(int)
    df["희망친구_피지목수"] = df["이름"].map(want).fillna(0).astype(int)
    df["멀리있는친구_피지목수"] = df["이름"].map(distant).fillna(0).astype(int)

    scores, labels, alerts, wants_talk, reasons_all = [], [], [], [], []
    for _, row in df.iterrows():
        reasons = []
        s = WEATHER_SCORE.get(row.get("마음날씨"), 1)
        s += MOOD_SCORE.get(row.get("현재기분"), 1)
        deep = row.get("깊이있는마음")
        s += DEEP_SCORE.get(deep, 1)
        joy = to_int(row.get("점수_즐거움"), 4)
        hard = to_int(row.get("점수_힘든친구"), 1)
        talk = to_int(row.get("점수_상담대상"), 4)
        s += (5 - joy) + (hard - 1) + (5 - talk)

        if row["친한친구_가중피지목수"] == 0:
            s += 2
            reasons.append("친한 친구로 지목받지 못함")
        if row["멀리있는친구_피지목수"] >= 3:
            s += 1
            reasons.append(f"어려운 친구로 {row['멀리있는친구_피지목수']}회 지목")
        rel = str(row.get("관계어려움", ""))
        if "혼자 있는 시간" in rel or "말하기 어려워" in rel:
            s += 1
            reasons.append("관계 어려움 호소")
        if hard >= 4:
            reasons.append(f"힘들게 하는 친구 있음({hard}점)")
        if talk <= 2:
            reasons.append("고민 털어놓을 사람 부족")

        alert = deep in DEEP_ALERT or (row.get("마음날씨") == "천둥 번개" and row.get("현재기분") in ("속상해요", "화가 나요"))
        if alert:
            reasons.insert(0, "정서 문항 고위험 응답")
        talk_req = row.get("비밀편지여부") == "비밀이에요!"
        if talk_req:
            reasons.append("선생님과 상담 희망")

        if alert or s >= 14:
            label = "🔴 우선 상담"
        elif s >= 7 or talk_req or row["친한친구_가중피지목수"] == 0:
            label = "🟡 관찰 필요"
        else:
            label = "🟢 안정"

        scores.append(s)
        labels.append(label)
        alerts.append(alert)
        wants_talk.append(talk_req)
        reasons_all.append(" · ".join(reasons) if reasons else "-")

    df["위기점수"] = scores
    df["관심단계"] = labels
    df["즉시확인"] = alerts
    df["상담희망"] = wants_talk
    df["관심요인"] = reasons_all
    return df


def count_items(series: pd.Series, exclude=("특별히 없음", "없음", "특별히 어려운 점은 없어요.", "-", "")) -> pd.DataFrame:
    items = []
    for v in series.fillna(""):
        for t in str(v).split(","):
            t = t.strip()
            if t and t not in exclude:
                items.append("기타" if t.startswith("기타:") else t)
    if not items:
        return pd.DataFrame(columns=["항목", "학생수"])
    vc = pd.Series(items).value_counts().reset_index()
    vc.columns = ["항목", "학생수"]
    return vc


# =========================================================
# 5. 소시오그램
# =========================================================
def build_graph(df, roster_names, edge_cols):
    G = nx.DiGraph()
    for n in list(roster_names) + [n for n in df["이름"] if n not in roster_names]:
        G.add_node(n)
    for _, row in df.iterrows():
        src = row["이름"]
        for col, w in edge_cols:
            for t in split_names(row.get(col)):
                if t in G and t != src:
                    if G.has_edge(src, t):
                        G[src][t]["weight"] += w
                    else:
                        G.add_edge(src, t, weight=w)
    return G


def plot_sociogram(G, gender_map, care_map, responded, edge_color):
    pos = nx.spring_layout(G, k=0.9, seed=42, iterations=120)
    in_w = dict(G.in_degree(weight="weight"))
    out_d = dict(G.out_degree())

    annotations = []
    for u, v, d in G.edges(data=True):
        mutual = G.has_edge(v, u)
        annotations.append(dict(
            x=pos[v][0], y=pos[v][1], ax=pos[u][0], ay=pos[u][1],
            xref="x", yref="y", axref="x", ayref="y",
            showarrow=True, arrowhead=3, arrowsize=1.1,
            arrowwidth=min(4, 0.8 + d["weight"] * 0.45),
            arrowcolor=C["accent"] if mutual else edge_color,
            opacity=0.75 if mutual else 0.45, standoff=13, startstandoff=10,
        ))

    xs, ys, texts, hovers, sizes, colors, lines = [], [], [], [], [], [], []
    for n in G.nodes():
        xs.append(pos[n][0])
        ys.append(pos[n][1])
        texts.append(n)
        g = gender_map.get(n, "미기재")
        colors.append(C["boy"] if g == "남" else C["girl"] if g == "여" else C["unknown"])
        sizes.append(min(60, 20 + in_w.get(n, 0) * 3))
        care = care_map.get(n, "미응답")
        lines.append(CARE_COLORS.get(care, "#CBD5E1"))
        hovers.append(f"<b>{esc(n)}</b><br>받은 지목: {in_w.get(n, 0)}<br>보낸 지목: {out_d.get(n, 0)}<br>상태: {esc(care)}"
                      + ("" if n in responded else "<br><i>이번 회차 미응답</i>"))

    fig = go.Figure(go.Scatter(
        x=xs, y=ys, mode="markers+text", text=texts, textposition="top center",
        textfont=dict(size=12, color=C["ink"]),
        hovertext=hovers, hoverinfo="text",
        marker=dict(size=sizes, color=colors, line=dict(width=3, color=lines), opacity=0.95),
    ))
    fig.update_layout(
        annotations=annotations, showlegend=False, height=600,
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF",
        margin=dict(l=10, r=10, t=10, b=10),
        font=dict(family="Pretendard Variable, Pretendard, sans-serif"),
        hoverlabel=dict(bgcolor="#fff", font_size=13),
    )
    return fig, in_w, out_d


# =========================================================
# 6. Gemini OCR
# =========================================================
class SurveyExtraction(BaseModel):
    이름: str = Field(description="학생 이름 (미기재 시 '미기재')")
    마음날씨: str = Field(description="'아주 맑음', '구름 조금', '비 옴', '천둥 번개' 중 하나")
    현재기분: str = Field(description="'기분이 좋아요', '그냥 보통이에요', '조금 지쳐요', '속상해요', '화가 나요' 중 하나")
    깊이있는마음: str = Field(description="요즘 나의 마음과 가장 가까운 문장 (보기 문장 그대로)")
    주요고민: list[str] = Field(description="체크된 고민 목록")
    짜증순간: list[str] = Field(description="짜증나는 순간 목록")
    필요한것: list[str] = Field(description="필요한 것 목록")
    친한친구_1순위: str = Field(description="가장 친한 친구 1순위 이름")
    친한친구_1순위_이유: str = Field(description="1순위 친구와 친한 이유")
    친한친구_2순위: str = Field(description="2순위 친구 이름")
    친한친구_2순위_이유: str = Field(description="2순위 친구와 친한 이유")
    친한친구_3순위: str = Field(description="3순위 친구 이름")
    친한친구_3순위_이유: str = Field(description="3순위 친구와 친한 이유")
    자주노는친구: str = Field(description="함께 노는 친구 이름 (쉼표 구분)")
    친해지고싶은친구: str = Field(description="더 친해지고 싶은 친구 이름 (쉼표 구분)")
    멀리있는친구: str = Field(description="조금 멀리 있거나 어려운 친구 이름 (쉼표 구분)")
    관계어려움: list[str] = Field(description="친구 관계 어려움 체크 항목들")
    점수_즐거움: int = Field(description="학교생활 즐거움 1~5점 (없으면 4)")
    점수_힘든친구: int = Field(description="힘들게 하는 친구 1~5점 (없으면 1)")
    점수_상담대상: int = Field(description="고민 상담할 사람 1~5점 (없으면 4)")
    비밀편지여부: str = Field(description="'비밀이에요!' 또는 '괜찮아요'")
    하고싶은말: str = Field(description="서술형 텍스트 전체")


class BatchSurveyExtraction(BaseModel):
    results: list[SurveyExtraction] = Field(description="업로드된 각 설문지 이미지 순서대로 추출된 결과")


def prep_image(b: bytes, max_side=1600) -> bytes:
    img = Image.open(BytesIO(b))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def match_name(raw: str, roster_names: list[str]):
    raw = (raw or "").strip()
    if not raw or not roster_names or raw in roster_names:
        return raw, raw in roster_names if raw else False
    m = difflib.get_close_matches(raw, roster_names, n=1, cutoff=0.5)
    return (m[0], True) if m else (raw, False)


def match_name_list(raw: str, roster_names: list[str]) -> str:
    return ", ".join(match_name(n, roster_names)[0] for n in split_names(raw))


# =========================================================
# 7. 세션 상태
# =========================================================
ss = st.session_state
for k, v in {
    "class_code": "", "teacher_auth": False, "last_active": 0.0,
    "login_fail": 0, "lock_until": 0.0, "survey_nonce": 0, "letter_nonce": 0,
    "survey_done": None, "letter_done": False, "new_class_code": None,
}.items():
    if k not in ss:
        ss[k] = v


def enter_class(code_raw: str) -> bool:
    code = (code_raw or "").strip()
    if not code:
        return False
    info = get_class_public(code) or get_class_public(code.upper())
    if not info:
        return False
    if ss.class_code != info["class_code"]:
        ss.teacher_auth = False
        ss.pop("ocr_df", None)
    ss.class_code = info["class_code"]
    return True


def leave_class():
    ss.class_code = ""
    ss.teacher_auth = False
    ss.pop("ocr_df", None)
    try:
        st.query_params.clear()
    except Exception:
        pass


def teacher_login(code, pin) -> bool:
    now = time.time()
    if now < ss.lock_until:
        st.error(f"로그인 시도가 많아 잠시 잠겼습니다. {int(ss.lock_until - now)}초 후 다시 시도하세요.")
        return False
    if verify_teacher(code, pin):
        ss.teacher_auth = True
        ss.last_active = now
        ss.login_fail = 0
        return True
    ss.login_fail += 1
    if ss.login_fail >= MAX_LOGIN_ATTEMPTS:
        ss.lock_until = now + LOCK_SECONDS
        ss.login_fail = 0
    st.error("비밀번호가 올바르지 않습니다.")
    return False


def teacher_logout():
    ss.teacher_auth = False
    ss.pop("ocr_df", None)


# 링크/QR 입장 (?class=CODE)
try:
    qp_code = st.query_params.get("class")
except Exception:
    qp_code = None
if qp_code and not ss.class_code:
    enter_class(qp_code)

# 교사 세션 자동 만료
if ss.teacher_auth:
    if time.time() - ss.last_active > TEACHER_SESSION_MINUTES * 60:
        teacher_logout()
        st.toast("🔒 보안을 위해 교사 모드가 자동 로그아웃되었습니다.")
    else:
        ss.last_active = time.time()


# =========================================================
# 8. 사이드바
# =========================================================
STUDENT_PAGES = ["📝 마음 설문 참여", "💌 선생님께 비밀 편지"]
TEACHER_PAGES = ["📊 분석 대시보드", "👀 학생 설문 미리보기", "📸 종이 설문 AI 스캔", "💌 비밀 편지함", "⚙️ 학급 설정"]

with st.sidebar:
    st.markdown(
        f'<div class="brand"><div class="brand-logo">🌤️</div><div><div class="brand-name">{APP_NAME}</div>'
        f'<div class="brand-sub">{APP_TAGLINE}</div></div></div>',
        unsafe_allow_html=True,
    )

    cls = get_class_public(ss.class_code) if ss.class_code else None
    if ss.class_code and not cls:
        leave_class()

    page = None
    if not cls:
        st.markdown("**학급 입장**")
        code_in = st.text_input("학급 코드", placeholder="선생님이 알려준 코드 (예: K7Q2MX)", label_visibility="collapsed")
        if st.button("입장하기", type="primary", use_container_width=True):
            if enter_class(code_in):
                st.rerun()
            else:
                st.error("학급 코드를 찾을 수 없어요. 다시 확인해 주세요.")

        st.divider()
        with st.expander("👩‍🏫 선생님: 새 학급 만들기"):
            with st.form("new_class_form", clear_on_submit=True, border=False):
                n_school = st.text_input("학교명", placeholder="예: 해솔초등학교")
                n_class = st.text_input("학급명", placeholder="예: 3학년 7반")
                n_teacher = st.text_input("선생님 성함")
                n_pin = st.text_input("교사 비밀번호 (숫자 6자리 권장)", type="password")
                n_pin2 = st.text_input("비밀번호 확인", type="password")
                agree = st.checkbox("학생 개인정보를 학교 지침에 따라 수집·관리하며, 보호자 동의를 받겠습니다.")
                if st.form_submit_button("학급 만들기", type="primary", use_container_width=True):
                    if not (n_school.strip() and n_class.strip() and n_teacher.strip() and n_pin):
                        st.warning("모든 항목을 입력해 주세요.")
                    elif len(n_pin) < 4 or not n_pin.isdigit():
                        st.warning("비밀번호는 숫자 4자리 이상이어야 합니다.")
                    elif n_pin != n_pin2:
                        st.warning("비밀번호가 서로 다릅니다.")
                    elif not agree:
                        st.warning("개인정보 관리 동의가 필요합니다.")
                    else:
                        code, err = register_class(n_school.strip(), n_class.strip(), n_teacher.strip(), n_pin)
                        if err:
                            st.error(err)
                        else:
                            ss.new_class_code = code
                            enter_class(code)
                            teacher_login(code, n_pin)
                            st.rerun()
    else:
        st.markdown(
            f'<div class="class-card"><div class="t">🏫 {esc(class_title(cls))}</div>'
            f'<div class="s">담임 {esc(cls.get("teacher_name", ""))} 선생님 · 코드 {esc(cls["class_code"])}</div>'
            f'<div class="s">진행 회차: {esc(active_round_of(cls))}</div></div>',
            unsafe_allow_html=True,
        )
        if ss.teacher_auth:
            st.markdown(badge("교사 모드", "primary") + badge(f"{TEACHER_SESSION_MINUTES}분 후 자동 로그아웃", "muted"), unsafe_allow_html=True)
            st.write("")
            page = st.radio("메뉴", TEACHER_PAGES, key="nav_teacher", label_visibility="collapsed")
            st.divider()
            if st.button("🔒 교사 모드 로그아웃", use_container_width=True):
                teacher_logout()
                st.rerun()
            st.caption("학생에게 기기를 넘기기 전에 꼭 로그아웃해 주세요.")
        else:
            page = st.radio("메뉴", STUDENT_PAGES, key="nav_student", label_visibility="collapsed")
            st.divider()
            with st.expander("👩‍🏫 선생님 로그인"):
                pin_try = st.text_input("교사 비밀번호", type="password", key=f"pin_{cls['class_code']}")
                if st.button("로그인", use_container_width=True):
                    if teacher_login(cls["class_code"], pin_try):
                        st.rerun()
            if st.button("↩ 다른 학급으로", use_container_width=True):
                leave_class()
                st.rerun()


# =========================================================
# 9. 랜딩 페이지
# =========================================================
def page_landing():
    hero(f"{APP_NAME}", "아이들의 마음 날씨와 친구 관계를 한눈에. 담임선생님을 위한 학급 마음건강 관리 도구", "🌤️")
    c1, c2, c3 = st.columns(3)
    with c1:
        feature_card("📝", "3분 마음 설문", "크롬북·태블릿으로 참여하는 아이 눈높이 설문. 종이 설문지도 AI가 자동으로 읽어요.")
    with c2:
        feature_card("🕸️", "교우관계 지도", "1~3순위 가중치 소시오그램으로 단짝, 소그룹, 관계에서 소외된 학생을 한눈에 확인해요.")
    with c3:
        feature_card("🔔", "관심 학생 알림", "정서 문항과 관계 데이터를 종합해 먼저 살펴볼 학생을 알려주고 회차별 변화를 추적해요.")
    st.write("")
    with st.container(border=True):
        st.markdown("#### 시작하는 방법")
        s1, s2, s3 = st.columns(3)
        with s1:
            step_header(1, "학급 만들기", "왼쪽 메뉴에서 새 학급을 만들면 고유 학급 코드가 발급돼요.")
        with s2:
            step_header(2, "명렬표 등록", "엑셀 양식에 출석번호·이름·성별을 넣어 올려요.")
        with s3:
            step_header(3, "학생 참여", "학급 코드나 QR을 학생들에게 알려주면 바로 설문에 참여해요.")
    st.info("👈 학생은 선생님이 알려준 **학급 코드**를 왼쪽에 입력하세요.")
    footer()


if not cls:
    if ss.new_class_code:
        st.success(f"학급이 만들어졌습니다! 학급 코드: **{ss.new_class_code}**")
        ss.new_class_code = None
    page_landing()
    st.stop()


CODE = cls["class_code"]
ROUND = active_round_of(cls)
roster_df = load_roster(CODE)
roster_names = roster_df["name"].tolist() if not roster_df.empty else []
gender_map = dict(zip(roster_df["name"], roster_df["gender"])) if not roster_df.empty else {}
display_to_name = {f"{r.student_no}번 {r.name}": r.name for r in roster_df.itertuples()} if not roster_df.empty else {}


def empty_roster_notice():
    with st.container(border=True):
        st.markdown("#### 🙋 아직 명렬표가 없어요")
        st.write("선생님이 학급 설정에서 학생 명렬표를 등록하면 설문에 참여할 수 있어요.")


# =========================================================
# 10. 학생: 마음 설문
# =========================================================
def checkbox_group(options, key_prefix, skip_word=None):
    sel = []
    for i, opt in enumerate(options):
        if st.checkbox(opt, key=f"{key_prefix}_{i}"):
            if skip_word and skip_word in opt:
                continue
            sel.append(opt.split(" ", 1)[1] if " " in opt else opt)
    return sel


def page_student_survey(preview=False):
    if preview:
        st.info("👀 **미리보기 모드** — 학생에게 보이는 화면 그대로입니다. 여기서 제출해도 저장되지 않아요.")
    hero(f"{class_title(cls)} 마음 & 친구 이야기",
         "솔직한 마음을 적어주면 선생님이 더 잘 이해하고 도와줄게요. 모든 감정은 소중해요!", "🌈")

    if ss.survey_done:
        st.balloons()
        st.success(f"🎉 {ss.survey_done} 학생, 설문을 제출했어요. 고마워요!")
        ss.survey_done = None

    if not roster_names:
        empty_roster_notice()
        return

    n = ss.survey_nonce
    with st.container(border=True):
        step_header(1, "내 이름 선택하기", "출석번호와 이름을 골라주세요.")
        sel = st.selectbox("내 이름", ["-- 내 이름을 선택하세요 --"] + list(display_to_name.keys()),
                           key=f"me_{n}", label_visibility="collapsed")
    if sel not in display_to_name:
        footer()
        return
    my_name = display_to_name[sel]
    others = [x for x in roster_names if x != my_name]

    prev = round_latest(load_responses(CODE), ROUND)
    if not prev.empty and my_name in set(prev["이름"]):
        st.info("이번 회차에 이미 제출했어요. 다시 제출하면 새 응답으로 바뀌어요.")

    with st.form(f"survey_{n}", clear_on_submit=False, border=False):
        # ---- 2. 마음 날씨
        with st.container(border=True):
            step_header(2, "요즘 나의 마음 날씨와 기분")
            c1, c2 = st.columns(2)
            with c1:
                weather_map = {
                    "☀️ 아주 맑음 (매일매일 신나고 즐거워요!)": "아주 맑음",
                    "⛅ 구름 조금 (평범하지만 가끔 심심하거나 귀찮아요.)": "구름 조금",
                    "🌧️ 비 옴 (요즘 조금 속상하거나 걱정이 있어요.)": "비 옴",
                    "⛈️ 천둥 번개 (화가 나거나 많이 힘들고 지쳐 있어요.)": "천둥 번개",
                }
                w_choice = st.radio("요즘 나의 '마음 날씨'는 어떤가요?", list(weather_map.keys()), key=f"wt_{n}")
                mood_map = {"😄 기분이 좋아요": "기분이 좋아요", "🙂 그냥 보통이에요": "그냥 보통이에요",
                            "😐 조금 지쳐요": "조금 지쳐요", "😢 속상해요": "속상해요", "😡 화가 나요": "화가 나요"}
                m_choice = st.radio("지금 내 기분은 어떤가요?", list(mood_map.keys()), key=f"md_{n}", horizontal=True)
            with c2:
                deep_opts = list(DEEP_SCORE.keys())
                deep_emoji = ["😊", "🫤", "😟", "😣", "😞"]
                deep_map = {f"{e} {t}": t for e, t in zip(deep_emoji, deep_opts)}
                d_choice = st.radio("요즘 나의 마음과 가장 가까운 것은?", list(deep_map.keys()), key=f"dp_{n}")

        # ---- 3. 학교생활 점수 (v1에서 고정값이던 항목)
        with st.container(border=True):
            step_header(3, "나의 학교생활 점수", "가장 가까운 칸으로 옮겨 주세요.")
            s1, s2, s3 = st.columns(3)
            joy_lbl = {1: "전혀 안 즐거워요", 2: "별로예요", 3: "보통이에요", 4: "즐거워요", 5: "아주 즐거워요"}
            hard_lbl = {1: "전혀 없어요", 2: "조금 있어요", 3: "가끔 있어요", 4: "자주 있어요", 5: "아주 많아요"}
            talk_lbl = {1: "아무도 없어요", 2: "거의 없어요", 3: "조금 있어요", 4: "있어요", 5: "많이 있어요"}
            with s1:
                joy = st.select_slider("😊 학교생활이 즐거운가요?", options=[1, 2, 3, 4, 5], value=4,
                                       format_func=lambda x: joy_lbl[x], key=f"sj_{n}")
            with s2:
                hard = st.select_slider("😣 나를 힘들게 하는 친구가 있나요?", options=[1, 2, 3, 4, 5], value=1,
                                        format_func=lambda x: hard_lbl[x], key=f"sh_{n}")
            with s3:
                talk = st.select_slider("🗣️ 고민을 이야기할 사람이 있나요?", options=[1, 2, 3, 4, 5], value=4,
                                        format_func=lambda x: talk_lbl[x], key=f"st_{n}")

        # ---- 4. 힘든 일
        with st.container(border=True):
            step_header(4, "요즘 나에게 힘든 일이 있나요?", "해당하는 것을 모두 골라요.")
            w1, w2, w3 = st.columns(3)
            with w1:
                st.markdown("**나를 힘들게 하는 것**")
                worries = checkbox_group(["📖 공부가 어렵거나 성적이 걱정돼요.", "🎒 숙제/준비물 챙기기 힘들어요.",
                                          "👫 친구 관계 문제로 속상해요.", "😥 혼나는 것이 걱정돼요.",
                                          "🔋 에너지가 없어요.", "🍀 특별한 힘든 일 없어요."], f"w_{n}", "특별한")
                worry_etc = st.text_input("✏️ 기타", key=f"we_{n}")
            with w2:
                st.markdown("**짜증나는 순간**")
                moments = checkbox_group(["📚 공부할 때", "📝 숙제/준비할 때", "🗣️ 친구들과 있을 때",
                                          "🛝 놀이 시간에", "🍱 식사할 때", "🏠 집에 있을 때"], f"m_{n}")
                moment_etc = st.text_input("✏️ 기타", key=f"me2_{n}")
            with w3:
                st.markdown("**나에게 필요한 것**")
                needs = checkbox_group(["💖 따뜻한 말과 격려", "😊 친구와 더 많은 시간", "📖 공부 도움",
                                        "🛌 충분한 휴식", "🎵 좋아하는 활동", "💬 선생님과 대화"], f"nd_{n}")
                need_etc = st.text_input("✏️ 기타", key=f"ne_{n}")

        # ---- 5. 친구 관계
        with st.container(border=True):
            step_header(5, "우리들의 친구 관계 이야기", "가장 마음이 편하고 친한 친구 3명을 순위대로 골라주세요. 친구들은 볼 수 없어요.")
            f_cols = st.columns(3)
            friends, reasons = [], []
            for i, (col, medal, ph) in enumerate(zip(f_cols, ["🥇 1순위", "🥈 2순위", "🥉 3순위"],
                                                     ["같이 그림 그리며 놀아요", "착하고 친절해요", "성격이 잘 맞아요"])):
                with col:
                    st.markdown(f"**{medal} 친구**")
                    friends.append(st.selectbox(f"{medal} 친구", ["-- 선택하세요 --"] + others,
                                                key=f"f{i}_{n}", label_visibility="collapsed"))
                    reasons.append(st.text_input("친한 이유", placeholder=f"예: {ph}", key=f"r{i}_{n}"))
            st.write("")
            p1, p2 = st.columns(2)
            with p1:
                play = st.multiselect("🏃 자주 함께 노는 친구", others, placeholder="여러 명 고를 수 있어요", key=f"pl_{n}")
                want = st.multiselect("🌟 더 친해지고 싶은 친구", others, placeholder="여러 명 고를 수 있어요", key=f"wn_{n}")
            with p2:
                distant = st.multiselect("💭 조금 어려운 친구 (선택)", others, placeholder="선택하지 않아도 괜찮아요", key=f"ds_{n}")
                distant_reason = st.text_input("💔 어려운 이유가 있다면 (선택)", placeholder="선생님만 볼 수 있어요.", key=f"dr_{n}")
            st.markdown("**친구 관계에서 어려웠던 경험이 있나요?**")
            rel_opts = ["😊 특별히 어려운 점 없어요.", "🙋 놀이에 끼고 싶었지만 못 꼈어요.", "🙉 친구들이 내 말을 잘 안 들어줘요.",
                        "⚡ 친구와 자주 다퉈요.", "🏝️ 혼자 있는 시간이 많아요.", "🤐 속상하지만 말하기 어려워요."]
            rc1, rc2 = st.columns(2)
            rel_sel = []
            for i, opt in enumerate(rel_opts):
                if (rc1 if i % 2 == 0 else rc2).checkbox(opt, key=f"rd{i}_{n}") and "특별히" not in opt:
                    rel_sel.append(opt.split(" ", 1)[1])
            rel_etc = st.text_input("✏️ 기타 어려움", key=f"re_{n}")

        # ---- 6. 마음속 이야기
        with st.container(border=True):
            step_header(6, "마음속 이야기")
            happy = st.text_area("😊 가장 기뻤던 일이나 자랑거리", placeholder="예: 모둠 활동을 잘 해냈어요!", height=80, key=f"hp_{n}")
            free = st.text_area("❤️ 선생님이나 친구에게 하고 싶은 말", placeholder="어떤 내용이든 편하게 적어주세요.", height=100, key=f"fr_{n}")
            want_talk = st.checkbox("🙋 선생님과 따로 이야기하고 싶어요", key=f"wt2_{n}")
            st.caption("💡 선생님에게만 몰래 말하고 싶은 이야기는 왼쪽 메뉴의 **💌 선생님께 비밀 편지**를 이용해 주세요.")

        submitted = st.form_submit_button("💌 설문 제출하기", type="primary", use_container_width=True)

    if submitted and preview:
        st.success("미리보기 모드라 저장하지 않았어요. 학생 화면에서는 정상 제출됩니다.")
        return
    if submitted:
        picked = [f for f in friends if f != "-- 선택하세요 --"]
        if len(picked) != len(set(picked)):
            st.error("1~3순위에 같은 친구를 두 번 고를 수 없어요. 다시 확인해 주세요.")
            return
        if worry_etc.strip():
            worries.append(f"기타: {worry_etc.strip()}")
        if moment_etc.strip():
            moments.append(f"기타: {moment_etc.strip()}")
        if need_etc.strip():
            needs.append(f"기타: {need_etc.strip()}")
        if rel_etc.strip():
            rel_sel.append(f"기타: {rel_etc.strip()}")
        notes = []
        if happy.strip():
            notes.append(f"[기쁜일/자랑] {happy.strip()}")
        if distant_reason.strip():
            notes.append(f"[어려운친구이유] {distant_reason.strip()}")
        if free.strip():
            notes.append(f"[하고싶은말] {free.strip()}")
        fv = [f if f != "-- 선택하세요 --" else "" for f in friends]

        record = {
            "class_code": CODE, "survey_round": ROUND, "이름": my_name,
            "마음날씨": weather_map[w_choice], "현재기분": mood_map[m_choice], "깊이있는마음": deep_map[d_choice],
            "주요고민": ", ".join(worries) or "특별히 없음", "짜증순간": ", ".join(moments) or "없음",
            "필요한것": ", ".join(needs) or "없음",
            "친한친구_1순위": fv[0], "친한친구_1순위_이유": reasons[0].strip(),
            "친한친구_2순위": fv[1], "친한친구_2순위_이유": reasons[1].strip(),
            "친한친구_3순위": fv[2], "친한친구_3순위_이유": reasons[2].strip(),
            "자주노는친구": ", ".join(play), "친해지고싶은친구": ", ".join(want), "멀리있는친구": ", ".join(distant),
            "관계어려움": ", ".join(rel_sel) or "특별히 어려운 점은 없어요.",
            "점수_즐거움": int(joy), "점수_힘든친구": int(hard), "점수_상담대상": int(talk),
            "비밀편지여부": "비밀이에요!" if want_talk else "괜찮아요",
            "하고싶은말": " / ".join(notes), "제출방식": "웹설문",
            "제출일시": datetime.now(KST).isoformat(),
        }
        try:
            insert_responses([record])
        except Exception as e:
            st.error(f"제출 중 문제가 생겼어요. 선생님께 알려주세요. ({e})")
            return
        ss.survey_done = my_name
        ss.survey_nonce += 1
        st.rerun()
    footer()


# =========================================================
# 11. 학생: 비밀 편지
# =========================================================
def page_student_letter():
    hero("선생님께 비밀 편지", "친구들은 볼 수 없어요. 선생님만 조용히 읽고 꼭 도와줄게요.", "🔒")
    if ss.letter_done:
        st.success("🔒 비밀 편지가 선생님께 안전하게 전달되었어요. 용기 내줘서 고마워요 ❤️")
        ss.letter_done = False
    if not roster_names:
        empty_roster_notice()
        return
    n = ss.letter_nonce
    with st.container(border=True):
        sel = st.selectbox("내 이름", ["-- 내 이름을 선택하세요 --"] + list(display_to_name.keys()), key=f"lt_me_{n}")
        content = st.text_area("편지 내용", height=200, key=f"lt_ct_{n}",
                               placeholder="예: 요즘 친구 때문에 속상한 일이 있어요... / 선생님과 따로 이야기하고 싶어요.")
        if st.button("💌 선생님께 보내기", type="primary", use_container_width=True):
            if sel not in display_to_name:
                st.warning("내 이름을 먼저 골라주세요.")
            elif not content.strip():
                st.warning("편지 내용을 적어주세요.")
            else:
                try:
                    insert_secret_letter(CODE, display_to_name[sel], content.strip())
                except Exception as e:
                    st.error(f"전송 중 문제가 생겼어요. 선생님께 직접 말해 주세요. ({e})")
                    return
                ss.letter_done = True
                ss.letter_nonce += 1
                st.rerun()
    st.markdown(
        '<div class="help-box">🆘 <b>지금 많이 힘들다면</b> 편지를 기다리지 말고 바로 선생님이나 보호자에게 말해 주세요. '
        '청소년상담전화 <b>1388</b>에서도 언제든 이야기를 들어줘요.</div>',
        unsafe_allow_html=True,
    )
    footer()


# =========================================================
# 12. 교사: 대시보드
# =========================================================
def page_dashboard():
    hero(f"{class_title(cls)} 분석 대시보드", "학생 응답은 진단이 아닌 관찰·상담을 위한 참고 자료입니다.", "📊")
    all_df = load_responses(CODE)
    rounds = ordered_rounds(all_df)
    if not rounds:
        with st.container(border=True):
            st.markdown("#### 아직 설문 응답이 없어요")
            st.write("학생들에게 학급 코드나 QR을 안내하거나, 종이 설문지를 AI 스캔으로 불러오세요.")
        footer()
        return

    rounds_desc = list(reversed(rounds))
    default_idx = rounds_desc.index(ROUND) if ROUND in rounds_desc else 0
    top1, top2 = st.columns([3, 1])
    with top1:
        sel_round = st.selectbox("조회 회차", rounds_desc, index=default_idx)
    df = analyze_student_metrics(round_latest(all_df, sel_round), roster_names)
    with top2:
        st.write("")
        export_cols = [c for c in ["관심단계", "위기점수", "관심요인", "이름", "마음날씨", "현재기분", "깊이있는마음",
                                   "점수_즐거움", "점수_힘든친구", "점수_상담대상", "친한친구_1순위", "친한친구_2순위",
                                   "친한친구_3순위", "친한친구_가중피지목수", "주요고민", "필요한것", "관계어려움",
                                   "하고싶은말", "제출일시"] if c in df.columns]
        st.download_button("📥 이번 회차 엑셀", to_excel_bytes(df[export_cols]), file_name=f"{CODE}_{sel_round}.xlsx",
                           use_container_width=True)

    letters = load_secret_letters(CODE)
    unread = int((~letters["is_read"].astype(bool)).sum()) if not letters.empty and "is_read" in letters else 0
    total = len(roster_names) or len(df)
    n_red = int((df["관심단계"] == "🔴 우선 상담").sum())
    n_yel = int((df["관심단계"] == "🟡 관찰 필요").sum())
    kpi_row([
        ("응답률", f"{len(df)}/{total}명", f"{(len(df) / total * 100 if total else 0):.0f}% 참여", "primary"),
        ("우선 상담", f"{n_red}명", "먼저 만나볼 학생", "danger"),
        ("관찰 필요", f"{n_yel}명", "세심한 관찰 권장", "warn"),
        ("상담 희망", f"{int(df['상담희망'].sum())}명", "선생님과 대화 원함", "accent"),
        ("새 비밀 편지", f"{unread}통", "비밀 편지함 확인", "ok"),
    ])

    urgent = df[df["즉시확인"]]
    if not urgent.empty:
        st.write("")
        st.error("🚨 **오늘 꼭 확인해 주세요** — 정서 문항에서 높은 수준의 어려움을 응답한 학생: "
                 + ", ".join(urgent["이름"].tolist())
                 + "\n\n필요 시 학교 Wee클래스·상담교사와 협의하고 학교 위기대응 절차를 따르세요.")

    care_map = dict(zip(df["이름"], df["관심단계"]))
    st.write("")
    t1, t2, t3, t4, t5 = st.tabs(["📊 학급 개요", "🕸️ 교우관계 지도", "🔍 관심 학생", "🧒 개별 리포트", "📈 변화 추이"])

    with t1:
        c1, c2 = st.columns(2)
        with c1:
            w = df["마음날씨"].value_counts().reindex(list(WEATHER_SCORE.keys())).fillna(0).reset_index()
            w.columns = ["마음날씨", "학생수"]
            fig = px.pie(w, names="마음날씨", values="학생수", hole=0.55, title="🌤️ 마음 날씨 분포",
                         color="마음날씨", color_discrete_map=WEATHER_COLORS)
            fig.update_traces(textinfo="label+value", sort=False)
            st.plotly_chart(style_fig(fig, 360), use_container_width=True)
        with c2:
            m = df["현재기분"].value_counts().reindex(list(MOOD_SCORE.keys())).fillna(0).reset_index()
            m.columns = ["기분", "학생수"]
            fig = px.bar(m, x="기분", y="학생수", title="🙂 지금 기분", color_discrete_sequence=[C["primary"]])
            fig.update_traces(marker_line_width=0, texttemplate="%{y}", textposition="outside")
            st.plotly_chart(style_fig(fig, 360), use_container_width=True)
        c3, c4 = st.columns(2)
        for col, src, title, color in [(c3, "주요고민", "🧩 많이 고른 고민", C["accent"]), (c4, "필요한것", "💖 아이들이 원하는 것", C["ok"])]:
            with col:
                vc = count_items(df[src]) if src in df else pd.DataFrame()
                if vc.empty:
                    st.info(f"{title}: 응답 없음")
                else:
                    fig = px.bar(vc.head(8).iloc[::-1], x="학생수", y="항목", orientation="h", title=title,
                                 color_discrete_sequence=[color])
                    st.plotly_chart(style_fig(fig, 340), use_container_width=True)
        missing = [x for x in roster_names if x not in set(df["이름"])]
        with st.container(border=True):
            st.markdown(f"**📮 미응답 학생 ({len(missing)}명)**")
            st.write(", ".join(missing) if missing else "모든 학생이 응답했어요! 🎉")

    with t2:
        rel_type = st.radio("관계 유형", ["💖 친한 친구 (1~3순위 가중치)", "🏃 자주 함께 노는 친구",
                                       "🌟 더 친해지고 싶은 친구", "💔 어려운 친구"], horizontal=True)
        if "친한" in rel_type:
            edge_cols, ecolor = CLOSE_COLS, C["primary"]
        else:
            edge_cols, ecolor = {
                "🏃 자주 함께 노는 친구": ([("자주노는친구", 1)], "#26B5C9"),
                "🌟 더 친해지고 싶은 친구": ([("친해지고싶은친구", 1)], C["warn"]),
                "💔 어려운 친구": ([("멀리있는친구", 1)], C["danger"]),
            }[rel_type]
        responded = set(df["이름"])
        G = build_graph(df, roster_names, edge_cols)
        if G.number_of_nodes() == 0:
            st.info("표시할 데이터가 없습니다.")
        else:
            fig, in_w, out_d = plot_sociogram(G, gender_map, care_map, responded, ecolor)
            with st.container(border=True):
                st.plotly_chart(fig, use_container_width=True)
                st.markdown(
                    f'<span class="legend-chip"><span class="legend-dot" style="background:{C["boy"]}"></span>남학생</span>'
                    f'<span class="legend-chip"><span class="legend-dot" style="background:{C["girl"]}"></span>여학생</span>'
                    f'<span class="legend-chip"><span class="legend-dot" style="border:3px solid {C["danger"]}"></span>테두리: 관심 단계</span>'
                    f'<span class="legend-chip"><span class="legend-dot" style="background:{C["accent"]}"></span>주황 화살표: 서로 지목</span>'
                    '<span class="legend-chip">원 크기: 받은 지목</span>',
                    unsafe_allow_html=True,
                )

            sorted_in = sorted(in_w.items(), key=lambda x: x[1], reverse=True)
            max_in = sorted_in[0][1] if sorted_in else 0
            top_students = [f"**{k}**({v})" for k, v in sorted_in if v > 0 and v >= max(2, max_in - 2)][:5]
            mutual, seen = [], set()
            for u, v in G.edges():
                if G.has_edge(v, u):
                    p = tuple(sorted([u, v]))
                    if p not in seen:
                        seen.add(p)
                        mutual.append(f"**{p[0]} ↔ {p[1]}**")
            unselected = [x for x in G.nodes() if x in responded and in_w.get(x, 0) == 0 and out_d.get(x, 0) > 0]
            isolated = [x for x in G.nodes() if x in responded and in_w.get(x, 0) == 0 and out_d.get(x, 0) == 0]
            no_resp_no_pick = [x for x in G.nodes() if x not in responded and in_w.get(x, 0) == 0]
            clusters = sorted([list(c) for c in nx.connected_components(G.to_undirected()) if len(c) >= 2], key=len, reverse=True)

            st.write("")
            i1, i2 = st.columns(2)
            if "어려운" in rel_type:
                with i1:
                    st.error("🚨 **어려운 친구로 많이 지목된 학생**\n\n" + (", ".join(top_students) or "지목이 몰린 학생이 없습니다."))
                    st.warning("⚡ **서로를 어렵다고 지목한 쌍**\n\n" + (", ".join(mutual) or "없습니다."))
                with i2:
                    st.info("💡 **지도 팁**\n\n지목이 몰린 학생이 '가해자'라는 뜻은 아닙니다. 모둠·자리 배치에서 거리를 조절하고, "
                            "개별 면담으로 불편 요인을 먼저 파악해 주세요. 이 결과는 학생에게 공개하지 마세요.")
            else:
                with i1:
                    st.success("🌟 **지목이 모이는 학생**\n\n" + (", ".join(top_students) or "아직 뚜렷하지 않습니다."))
                    st.info("🤝 **서로 지목한 단짝**\n\n" + (", ".join(mutual) or "아직 없습니다."))
                with i2:
                    st.warning("🏝️ **관계 지원이 필요할 수 있는 학생**\n\n"
                               f"- 주고받은 지목 없음: {', '.join(isolated) or '없음'}\n"
                               f"- 친구를 골랐지만 지목받지 못함: {', '.join(unselected) or '없음'}\n"
                               f"- 미응답 & 지목 없음: {', '.join(no_resp_no_pick) or '없음'}")
                    if clusters:
                        st.markdown("🏘️ **주요 소그룹**\n" + "\n".join(
                            f"- 그룹 {i + 1} ({len(c)}명): {', '.join(c)}" for i, c in enumerate(clusters[:4])))
                st.markdown(
                    f"> **💡 학급 경영 제안:** 서로 지목한 단짝 **{len(mutual)}쌍**, 지목을 받지 못한 학생 "
                    f"**{len(isolated) + len(unselected)}명**. 모둠 활동 때 지목이 모이는 학생"
                    f"({', '.join(k for k, v in sorted_in[:2] if v > 0) or '-'})과 관계 지원이 필요한 학생을 자연스럽게 연결하고, "
                    "'더 친해지고 싶은 친구' 데이터를 자리 배치에 참고해 보세요."
                )

    with t3:
        view = df.sort_values(by="위기점수", ascending=False)
        filt = st.segmented_control("단계 필터", ["전체", "🔴 우선 상담", "🟡 관찰 필요", "🟢 안정"], default="전체") \
            if hasattr(st, "segmented_control") else st.radio("단계 필터", ["전체", "🔴 우선 상담", "🟡 관찰 필요", "🟢 안정"], horizontal=True)
        if filt and filt != "전체":
            view = view[view["관심단계"] == filt]
        st.dataframe(
            view[["관심단계", "이름", "위기점수", "관심요인", "마음날씨", "현재기분", "친한친구_가중피지목수", "주요고민", "하고싶은말"]],
            use_container_width=True, hide_index=True, height=520,
            column_config={
                "위기점수": st.column_config.ProgressColumn("관심 점수", min_value=0, max_value=MAX_RISK_SCORE, format="%d"),
                "친한친구_가중피지목수": st.column_config.NumberColumn("받은 친구 지목", format="%d"),
                "관심요인": st.column_config.TextColumn("관심 요인", width="large"),
                "하고싶은말": st.column_config.TextColumn("서술 응답", width="large"),
            },
        )
        with st.expander("ℹ️ 관심 점수는 어떻게 계산되나요?"):
            st.markdown(
                "- 마음 날씨(0~4) + 현재 기분(0~4) + 마음 문장(0~5)\n"
                "- 학교생활 점수: (5−즐거움) + (힘든 친구−1) + (5−상담 대상)\n"
                "- 친한 친구 지목 0회 +2, 어려운 친구 지목 3회 이상 +1, 고립·말하기 어려움 체크 +1\n"
                f"- **14점 이상 또는 고위험 정서 응답 → 우선 상담**, 7점 이상·상담 희망·지목 0회 → 관찰 필요 (최대 {MAX_RISK_SCORE}점)\n\n"
                "점수는 선생님의 관찰을 돕는 보조 지표이며 심리 진단 도구가 아닙니다."
            )

    with t4:
        names = df.sort_values("위기점수", ascending=False)["이름"].tolist()
        sel = st.selectbox("학생 선택", names, format_func=lambda x: f"{x}  {care_map.get(x, '')}")
        r = df[df["이름"] == sel].iloc[0]
        tone = {"🔴 우선 상담": "danger", "🟡 관찰 필요": "warn"}.get(r["관심단계"], "ok")
        st.markdown(f"### 🧒 {esc(sel)} " + badge(r["관심단계"], tone) + badge(f"관심 점수 {r['위기점수']}", "muted")
                    + (badge("상담 희망", "primary") if r["상담희망"] else ""), unsafe_allow_html=True)
        st.caption(f"관심 요인: {r['관심요인']}")
        c1, c2 = st.columns(2)
        with c1, st.container(border=True):
            st.markdown("#### 🌤️ 정서 상태")
            st.write(f"- **마음 날씨:** {r['마음날씨']} / **기분:** {r['현재기분']}")
            st.write(f"- **마음 문장:** {r['깊이있는마음']}")
            st.write(f"- **학교생활 점수:** 즐거움 {r.get('점수_즐거움', '-')} · 힘든 친구 {r.get('점수_힘든친구', '-')} · 상담 대상 {r.get('점수_상담대상', '-')}")
            st.write(f"- **고민:** {r['주요고민']}")
            st.write(f"- **짜증나는 순간:** {r['짜증순간']}")
            st.write(f"- **필요한 것:** {r['필요한것']}")
        with c2, st.container(border=True):
            st.markdown("#### 🤝 교우 관계")
            for i, medal in enumerate(["🥇", "🥈", "🥉"], 1):
                f = r.get(f"친한친구_{i}순위", "")
                if f:
                    st.write(f"- {medal} **{f}** — {r.get(f'친한친구_{i}순위_이유', '') or '이유 미기재'}")
            st.write(f"- **자주 노는 친구:** {r['자주노는친구'] or '-'}")
            st.write(f"- **친해지고 싶은 친구:** {r['친해지고싶은친구'] or '-'}")
            st.write(f"- **어려운 친구:** {r['멀리있는친구'] or '-'}")
            st.write(f"- **관계 어려움:** {r['관계어려움']}")
            picked_by = [f"{row['이름']}({i}순위)" for _, row in df.iterrows()
                         for i, (col, _) in enumerate(CLOSE_COLS, 1) if str(row.get(col, "")).strip() == sel]
            st.write(f"- **나를 친한 친구로 고른 학생:** {', '.join(picked_by) or '없음'}")
        with st.container(border=True):
            st.markdown("#### 💌 마음속 이야기")
            st.write(r["하고싶은말"] or "작성된 내용이 없습니다.")

    with t5:
        hist = []
        for rd in rounds:
            a = analyze_student_metrics(round_latest(all_df, rd), roster_names)
            if not a.empty:
                a["회차"] = rd
                hist.append(a)
        full = pd.concat(hist, ignore_index=True)
        c1, c2 = st.columns(2)
        with c1:
            avg = full.groupby("회차", sort=False)["위기점수"].mean().reindex(rounds).reset_index()
            fig = px.line(avg, x="회차", y="위기점수", markers=True, title="학급 평균 관심 점수",
                          color_discrete_sequence=[C["primary"]])
            st.plotly_chart(style_fig(fig, 340), use_container_width=True)
        with c2:
            dist = full.groupby(["회차", "관심단계"], sort=False).size().reset_index(name="학생수")
            fig = px.bar(dist, x="회차", y="학생수", color="관심단계", title="회차별 관심 단계 분포",
                         color_discrete_map=CARE_COLORS, category_orders={"회차": rounds})
            st.plotly_chart(style_fig(fig, 340), use_container_width=True)
        t_stu = st.selectbox("학생별 추이", sorted(full["이름"].unique().tolist()), key="h_stu")
        t_df = full[full["이름"] == t_stu]
        fig = px.line(t_df, x="회차", y="위기점수", markers=True, title=f"{t_stu} 관심 점수 변화",
                      color_discrete_sequence=[C["accent"]], category_orders={"회차": rounds},
                      hover_data=["마음날씨", "현재기분", "관심단계"])
        fig.update_yaxes(range=[0, MAX_RISK_SCORE])
        st.plotly_chart(style_fig(fig, 320), use_container_width=True)
    footer()


# =========================================================
# 13. 교사: 종이 설문 AI 스캔
# =========================================================
def page_ocr():
    hero("종이 설문지 AI 스캔", "스캔한 PDF나 사진을 올리면 AI가 읽고, 선생님이 검수 후 저장해요.", "📸")
    api_key = get_api_key(CODE)

    with st.container(border=True):
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            ocr_round = st.text_input("저장할 회차", value=ROUND)
        with c2:
            batch_size = st.slider("한 번에 보낼 장 수", 3, 15, 8, help="많을수록 빠르지만 인식 오류가 늘 수 있어요.")
        with c3:
            st.write("")
            st.markdown(badge("API 키 등록됨", "ok") if api_key else badge("API 키 없음", "danger"), unsafe_allow_html=True)
        if not api_key:
            api_key = st.text_input("Gemini API Key (이번 세션에만 사용)", type="password")
            st.caption("학급 설정 → AI 설정에서 저장해 두면 매번 입력하지 않아도 돼요.")
        st.caption("⚠️ 학생 응답 이미지가 Google Gemini API로 전송됩니다. 데이터가 학습에 쓰이지 않는 유료(결제 등록) 키 사용을 권장합니다.")
        files = st.file_uploader("설문지 파일 (PDF / PNG / JPG)", type=["pdf", "png", "jpg", "jpeg"], accept_multiple_files=True)

    if files:
        images = []
        for f in files:
            b = f.getvalue()
            if f.name.lower().endswith(".pdf"):
                try:
                    doc = fitz.open(stream=b, filetype="pdf")
                    for p in doc:
                        images.append(prep_image(p.get_pixmap(dpi=150).tobytes("jpeg")))
                except Exception as e:
                    st.error(f"PDF 변환 오류 ({f.name}): {e}")
            else:
                try:
                    images.append(prep_image(b))
                except Exception as e:
                    st.error(f"이미지 오류 ({f.name}): {e}")
        total = len(images)
        st.markdown(badge(f"총 {total}페이지 감지", "primary"), unsafe_allow_html=True)
        if total:
            rng = st.slider("판독할 페이지 범위", 1, max(1, total), (1, max(1, total))) if total > 1 else (1, 1)
            if st.button("🤖 AI 판독 시작", type="primary"):
                if not api_key.strip():
                    st.error("Gemini API 키가 필요합니다.")
                else:
                    run_ocr(images[rng[0] - 1: rng[1]], batch_size, api_key.strip(), ocr_round)

    if "ocr_df" in ss and not ss["ocr_df"].empty:
        st.write("")
        with st.container(border=True):
            st.markdown("#### 📝 판독 결과 검수")
            bad = int((ss["ocr_df"]["확인필요"] != "").sum())
            if bad:
                st.warning(f"⚠️ {bad}건은 이름이 명렬표와 일치하지 않거나 자동 보정되었습니다. '확인필요' 열을 확인하세요.")
            edited = st.data_editor(ss["ocr_df"], num_rows="dynamic", use_container_width=True, height=420,
                                    column_config={"확인필요": st.column_config.TextColumn("확인필요", disabled=True)})
            b1, b2 = st.columns(2)
            with b1:
                if st.button("💾 DB에 저장", type="primary", use_container_width=True):
                    recs = edited.drop(columns=["확인필요"], errors="ignore").fillna("")
                    for c in ["점수_즐거움", "점수_힘든친구", "점수_상담대상"]:
                        recs[c] = recs[c].apply(lambda v, c=c: to_int(v, 1 if c == "점수_힘든친구" else 4))
                    try:
                        insert_responses(recs.to_dict(orient="records"))
                        ss.pop("ocr_df", None)
                        st.success(f"✅ {len(recs)}건 저장 완료!")
                    except Exception as e:
                        st.error(f"저장 실패: {e}")
            with b2:
                st.download_button("📥 엑셀로 받기", to_excel_bytes(edited), file_name=f"AI스캔_{ocr_round}.xlsx",
                                   use_container_width=True)
    footer()


def run_ocr(target_images, batch_size, api_key, ocr_round):
    rows = []
    prog = st.progress(0.0)
    status = st.empty()
    client = genai.Client(api_key=api_key)
    batches = (len(target_images) - 1) // batch_size + 1
    roster_str = ", ".join(roster_names) or "(명단 없음)"
    for bi in range(batches):
        batch = target_images[bi * batch_size: (bi + 1) * batch_size]
        status.info(f"🔄 {bi + 1}/{batches} 묶음 분석 중...")
        payload = [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in batch]
        payload.append(
            f"제공된 {len(batch)}장의 초등학생 설문지 이미지를 순서대로 분석하세요. 이미지 1장 = 학생 1명입니다. "
            f"우리 반 명단[{roster_str}]에 있는 이름으로 학생 이름과 친구 이름을 표기하세요. "
            f"깊이있는마음은 다음 보기 중 하나를 그대로 쓰세요: {list(DEEP_SCORE.keys())}. "
            "체크 항목, 1~5점 점수, 서술형 답변을 빠짐없이 읽어 JSON으로 반환하세요. 읽을 수 없는 칸은 빈 문자열로 두세요."
        )
        for attempt in range(3):
            try:
                res = client.models.generate_content(
                    model=GEMINI_MODEL, contents=payload,
                    config=types.GenerateContentConfig(response_mime_type="application/json",
                                                       response_schema=BatchSurveyExtraction, temperature=0.1),
                )
                for d in json.loads(res.text).get("results", []):
                    name, ok = match_name(d.get("이름", ""), roster_names)
                    flags = []
                    if not ok:
                        flags.append("이름 불일치")
                    elif name != d.get("이름", ""):
                        flags.append(f"이름 보정({d.get('이름', '')}→{name})")
                    friends = []
                    for i in (1, 2, 3):
                        fn, fok = match_name(d.get(f"친한친구_{i}순위", ""), roster_names)
                        if fn and not fok:
                            flags.append(f"{i}순위 친구 불일치")
                        friends.append(fn)
                    rows.append({
                        "확인필요": ", ".join(flags),
                        "class_code": CODE, "survey_round": ocr_round, "이름": name or "미기재",
                        "마음날씨": d.get("마음날씨") or "구름 조금", "현재기분": d.get("현재기분") or "그냥 보통이에요",
                        "깊이있는마음": d.get("깊이있는마음", ""),
                        "주요고민": ", ".join(d.get("주요고민", [])) or "특별히 없음",
                        "짜증순간": ", ".join(d.get("짜증순간", [])) or "없음",
                        "필요한것": ", ".join(d.get("필요한것", [])) or "없음",
                        "친한친구_1순위": friends[0], "친한친구_1순위_이유": d.get("친한친구_1순위_이유", ""),
                        "친한친구_2순위": friends[1], "친한친구_2순위_이유": d.get("친한친구_2순위_이유", ""),
                        "친한친구_3순위": friends[2], "친한친구_3순위_이유": d.get("친한친구_3순위_이유", ""),
                        "자주노는친구": match_name_list(d.get("자주노는친구", ""), roster_names),
                        "친해지고싶은친구": match_name_list(d.get("친해지고싶은친구", ""), roster_names),
                        "멀리있는친구": match_name_list(d.get("멀리있는친구", ""), roster_names),
                        "관계어려움": ", ".join(d.get("관계어려움", [])) or "특별히 어려운 점은 없어요.",
                        "점수_즐거움": to_int(d.get("점수_즐거움"), 4), "점수_힘든친구": to_int(d.get("점수_힘든친구"), 1),
                        "점수_상담대상": to_int(d.get("점수_상담대상"), 4),
                        "비밀편지여부": d.get("비밀편지여부") or "괜찮아요", "하고싶은말": d.get("하고싶은말", ""),
                        "제출방식": "AI 스캔", "제출일시": datetime.now(KST).isoformat(),
                    })
                if len(json.loads(res.text).get("results", [])) != len(batch):
                    st.warning(f"⚠️ {bi + 1}번 묶음: 이미지 {len(batch)}장 중 일부만 인식되었습니다. 해당 묶음을 다시 확인하세요.")
                break
            except Exception as e:
                if any(code in str(e) for code in ("503", "429")) and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                st.warning(f"⚠️ {bi + 1}번 묶음 오류: {e}")
                break
        prog.progress((bi + 1) / batches)
    if rows:
        ss["ocr_df"] = pd.DataFrame(rows)
        status.success(f"✅ {len(rows)}명 판독 완료! 아래에서 검수 후 저장하세요.")
    else:
        status.error("판독된 결과가 없습니다.")


# =========================================================
# 14. 교사: 비밀 편지함
# =========================================================
def page_letters():
    hero("비밀 편지함", "우리 반 아이들이 선생님에게만 보낸 편지입니다.", "💌")
    letters = load_secret_letters(CODE)
    legacy = load_responses(CODE)
    if not legacy.empty and "제출방식" in legacy.columns:
        legacy = legacy[legacy["제출방식"] == "비밀편지함"]
    else:
        legacy = pd.DataFrame()

    if letters.empty and legacy.empty:
        with st.container(border=True):
            st.markdown("#### 📭 아직 도착한 편지가 없어요")
        footer()
        return

    if not letters.empty:
        only_unread = st.toggle("안 읽은 편지만 보기", value=False)
        view = letters[~letters["is_read"].astype(bool)] if only_unread else letters
        unread = int((~letters["is_read"].astype(bool)).sum())
        st.markdown(badge(f"전체 {len(letters)}통", "primary") + badge(f"안 읽음 {unread}통", "danger" if unread else "muted"),
                    unsafe_allow_html=True)
        st.write("")
        for _, row in view.iterrows():
            is_read = bool(row.get("is_read"))
            ts = str(row.get("created_at", ""))[:16].replace("T", " ")
            with st.container(border=True):
                h1, h2 = st.columns([4, 1])
                with h1:
                    st.markdown(f"**{'📨' if is_read else '✉️'} {esc(row['student_name'])}** "
                                + badge("읽음" if is_read else "새 편지", "muted" if is_read else "danger")
                                + f" <span style='color:{C['muted']};font-size:.82rem'>{esc(ts)}</span>",
                                unsafe_allow_html=True)
                with h2:
                    if st.button("읽지 않음으로" if is_read else "읽음 표시", key=f"rd_{row['id']}", use_container_width=True):
                        mark_letter_read(row["id"], not is_read)
                        st.rerun()
                st.markdown("> " + str(row["content"]).replace("\n", "\n> "))

    if not legacy.empty:
        with st.expander(f"📁 이전 버전에서 접수된 편지 ({len(legacy)}통)"):
            for _, row in legacy.iloc[::-1].iterrows():
                st.markdown(f"**{row['이름']}** · {str(row['제출일시'])[:16].replace('T', ' ')}")
                st.markdown(f"> {row['하고싶은말']}")
                st.divider()
    footer()


# =========================================================
# 15. 교사: 학급 설정
# =========================================================
def page_settings():
    hero("학급 설정", "학급 공유, 설문 회차, 명렬표, AI, 보안, 데이터 관리를 한곳에서.", "⚙️")
    t1, t2, t3, t4, t5, t6 = st.tabs(["🔗 학급 공유", "🗓️ 설문 회차", "👥 명렬표", "🤖 AI 설정", "🔐 보안", "🗑️ 데이터 관리"])

    with t1:
        with st.container(border=True):
            c1, c2 = st.columns([2, 1])
            with c1:
                st.markdown("#### 학생 입장 안내")
                st.markdown(f"학급 코드: <span style='font-size:2rem;font-weight:800;letter-spacing:.15em;color:{C['primary']}'>{esc(CODE)}</span>",
                            unsafe_allow_html=True)
                link = f"{APP_BASE_URL.rstrip('/')}/?class={CODE}" if APP_BASE_URL else ""
                if link:
                    st.code(link, language=None)
                    st.caption("이 링크로 들어오면 학급 코드 입력 없이 바로 입장해요.")
                else:
                    st.caption("APP_BASE_URL을 설정하면 바로 입장 링크와 QR 코드가 만들어져요.")
            with c2:
                if link and qrcode:
                    buf = BytesIO()
                    qrcode.make(link).save(buf)
                    st.image(buf.getvalue(), caption="교실 TV에 띄워 주세요", width=200)
                    st.download_button("QR 이미지 받기", buf.getvalue(), file_name=f"{CODE}_QR.png", use_container_width=True)
        with st.container(border=True):
            st.markdown("#### 학급 정보 수정")
            with st.form("class_info"):
                s = st.text_input("학교명", value=cls.get("school_name") or "")
                cn = st.text_input("학급명", value=cls.get("class_name") or "")
                tn = st.text_input("선생님 성함", value=cls.get("teacher_name") or "")
                if st.form_submit_button("저장", type="primary"):
                    update_class(CODE, {"school_name": s.strip(), "class_name": cn.strip(), "teacher_name": tn.strip()})
                    st.success("저장되었습니다.")
                    st.rerun()

    with t2:
        with st.container(border=True):
            st.markdown("#### 진행 중인 설문 회차")
            st.caption("여기서 바꾼 회차가 모든 학생 기기에 바로 적용됩니다. 새 회차를 시작하면 이전 응답은 그대로 보관돼요.")
            new_round = st.text_input("회차명", value=ROUND)
            c1, c2 = st.columns(2)
            with c1:
                if st.button("회차 저장", type="primary", use_container_width=True):
                    update_class(CODE, {"active_round": new_round.strip() or default_round_name()})
                    st.success("회차가 변경되었습니다.")
                    st.rerun()
            with c2:
                if st.button(f"이번 달로 새 회차 시작 ({default_round_name()})", use_container_width=True):
                    update_class(CODE, {"active_round": default_round_name()})
                    st.rerun()

    with t3:
        with st.container(border=True):
            st.markdown("#### 엑셀로 명렬표 등록")
            st.markdown("`student_no`(출석번호), `name`(이름), `gender`(남/여) 세 열이 필요합니다.")
            sample = pd.DataFrame({"student_no": [1, 2, 3], "name": ["김하늘", "이바다", "박나무"], "gender": ["남", "여", "남"]})
            st.download_button("📥 업로드 양식 받기", to_excel_bytes(sample, "명렬표"), file_name="명렬표_양식.xlsx")
            up = st.file_uploader("명렬표 엑셀 (.xlsx)", type=["xlsx"])
            if up is not None:
                try:
                    up_df = pd.read_excel(up)
                except Exception as e:
                    st.error(f"엑셀을 읽을 수 없습니다: {e}")
                    up_df = pd.DataFrame()
                if {"student_no", "name", "gender"}.issubset(up_df.columns):
                    dup = up_df["name"].astype(str).str.strip().duplicated().sum()
                    st.success(f"{len(up_df)}명 확인" + (f" · ⚠️ 동명이인/중복 {dup}건 (두 번째부터 제외됨. 이름 뒤에 A/B 등을 붙여 구분하세요)" if dup else ""))
                    st.dataframe(up_df, use_container_width=True, hide_index=True)
                    st.warning("등록하면 기존 명렬표가 이 파일로 교체됩니다.")
                    if st.button("💾 명렬표 등록", type="primary"):
                        try:
                            cnt = save_roster(CODE, up_df)
                            st.success(f"✅ {cnt}명 등록 완료!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"등록 실패: {e}")
                elif not up_df.empty:
                    st.error("'student_no', 'name', 'gender' 열 이름이 정확히 있어야 합니다.")
        with st.container(border=True):
            st.markdown(f"#### 현재 명렬표 ({len(roster_df)}명)")
            if roster_df.empty:
                st.info("등록된 학생이 없습니다.")
            else:
                st.dataframe(roster_df.rename(columns={"student_no": "번호", "name": "이름", "gender": "성별"}),
                             use_container_width=True, hide_index=True)

    with t4:
        with st.container(border=True):
            st.markdown("#### Gemini API 키")
            has_key = bool(get_api_key(CODE))
            st.markdown(badge("등록됨", "ok") if has_key else badge("미등록", "muted"), unsafe_allow_html=True)
            if _fernet is None:
                st.warning("서버에 암호화 키(APP_ENCRYPTION_KEY)가 설정되지 않아 API 키를 저장할 수 없습니다. "
                           "AI 스캔 화면에서 세션 동안만 입력해 사용하세요.")
            else:
                new_key = st.text_input("새 API 키", type="password", placeholder="저장된 키는 보안상 다시 표시되지 않아요")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("🔑 암호화하여 저장", type="primary", use_container_width=True, disabled=not new_key):
                        save_api_key(CODE, new_key)
                        st.success("✅ 저장되었습니다.")
                with c2:
                    if st.button("키 삭제", use_container_width=True, disabled=not has_key):
                        update_class(CODE, {"gemini_api_key": ""})
                        st.rerun()
            st.caption(f"사용 모델: `{GEMINI_MODEL}` (관리자가 GEMINI_MODEL 설정으로 변경 가능)")

    with t5:
        with st.container(border=True):
            st.markdown("#### 교사 비밀번호 변경")
            with st.form("pin_change", clear_on_submit=True):
                cur = st.text_input("현재 비밀번호", type="password")
                p1 = st.text_input("새 비밀번호 (숫자 6자리 권장)", type="password")
                p2 = st.text_input("새 비밀번호 확인", type="password")
                if st.form_submit_button("변경", type="primary"):
                    if not verify_teacher(CODE, cur):
                        st.error("현재 비밀번호가 올바르지 않습니다.")
                    elif len(p1) < 4 or not p1.isdigit():
                        st.error("새 비밀번호는 숫자 4자리 이상이어야 합니다.")
                    elif p1 != p2:
                        st.error("새 비밀번호가 서로 다릅니다.")
                    else:
                        update_class(CODE, {"pin_code": hash_pin(p1)})
                        st.success("✅ 변경되었습니다.")

    with t6:
        with st.container(border=True):
            st.markdown("#### 회차별 응답 삭제")
            rounds = ordered_rounds(load_responses(CODE))
            if rounds:
                r_del = st.selectbox("삭제할 회차", rounds)
                conf = st.text_input("확인을 위해 '삭제'라고 입력하세요", key="del_round_conf")
                if st.button("이 회차 삭제", disabled=conf != "삭제"):
                    delete_round(CODE, r_del)
                    st.success("삭제되었습니다.")
                    st.rerun()
            else:
                st.caption("삭제할 응답이 없습니다.")
        with st.container(border=True):
            st.markdown("#### 학년도 종료 · 전체 데이터 파기")
            st.caption("설문 응답과 비밀 편지를 모두 삭제합니다. 학년 말이나 학교 보유 기간이 끝나면 반드시 파기해 주세요. 되돌릴 수 없습니다.")
            conf2 = st.text_input(f"확인을 위해 학급 코드 '{CODE}'를 입력하세요", key="del_all_conf")
            if st.button("🗑️ 전체 데이터 파기", type="primary", disabled=conf2 != CODE):
                delete_all_data(CODE)
                st.success("모든 응답 데이터가 파기되었습니다.")
                st.rerun()
    footer()


# =========================================================
# 16. 라우팅
# =========================================================
ROUTES = {
    "📝 마음 설문 참여": page_student_survey,
    "💌 선생님께 비밀 편지": page_student_letter,
    "📊 분석 대시보드": page_dashboard,
    "📸 종이 설문 AI 스캔": page_ocr,
    "💌 비밀 편지함": page_letters,
    "⚙️ 학급 설정": page_settings,
    "👀 학생 설문 미리보기": lambda: page_student_survey(preview=True),
}
if page in TEACHER_PAGES and not ss.teacher_auth:
    page = STUDENT_PAGES[0]
ROUTES.get(page or STUDENT_PAGES[0], page_student_survey)()
