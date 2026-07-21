"""HONG STOCK 중앙 앱의 Supabase 로그인 도우미."""

import os
from typing import Any

import requests
import streamlit as st


def _setting(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, default) or "").strip()
    except Exception:
        return default


def supabase_url() -> str:
    return _setting("SUPABASE_URL").rstrip("/")


def supabase_anon_key() -> str:
    return _setting("SUPABASE_ANON_KEY") or _setting("SUPABASE_PUBLISHABLE_KEY")


def is_configured() -> bool:
    return bool(supabase_url() and supabase_anon_key())


def _headers(access_token: str | None = None) -> dict[str, str]:
    key = supabase_anon_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {access_token or key}",
        "Content-Type": "application/json",
    }


def current_session() -> dict[str, Any] | None:
    session = st.session_state.get("hongstock_auth_session")
    if not isinstance(session, dict) or not session.get("access_token"):
        return None
    return session


def current_user() -> dict[str, Any] | None:
    session = current_session()
    if not session:
        return None
    user = session.get("user")
    return user if isinstance(user, dict) and user.get("id") else None


def is_admin_user(user: dict[str, Any] | None = None) -> bool:
    """Return whether the signed-in user may open operator-only pages."""
    # Keep this list in Secrets/.env, never in editable user profile data.
    user = user or current_user()
    email = str((user or {}).get("email") or "").strip().lower()
    allowed = {
        item.strip().lower()
        for item in _setting("HONGSTOCK_ADMIN_EMAILS").split(",")
        if item.strip()
    }
    return bool(email and email in allowed)


def access_token() -> str | None:
    session = current_session()
    return str(session.get("access_token")) if session else None


def _auth_request(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not is_configured():
        raise RuntimeError("Supabase 연결 정보가 아직 설정되지 않았습니다.")
    response = requests.post(
        f"{supabase_url()}{path}",
        headers=_headers(),
        json=payload,
        timeout=20,
    )
    if not response.ok:
        try:
            message = response.json().get("msg") or response.json().get("message")
        except ValueError:
            message = ""
        raise ValueError(message or "로그인 요청을 처리하지 못했습니다.")
    return response.json()


def sign_in(email: str, password: str) -> None:
    data = _auth_request(
        "/auth/v1/token?grant_type=password",
        {"email": email.strip(), "password": password},
    )
    if not data.get("access_token") or not data.get("user"):
        raise ValueError("로그인 정보를 확인해 주세요.")
    st.session_state["hongstock_auth_session"] = data


def sign_up(email: str, password: str, nickname: str, analytics_consent: bool) -> bool:
    data = _auth_request(
        "/auth/v1/signup",
        {
            "email": email.strip(),
            "password": password,
            "data": {
                "nickname": nickname.strip(),
                "analytics_consent": bool(analytics_consent),
            },
        },
    )
    if data.get("access_token") and data.get("user"):
        st.session_state["hongstock_auth_session"] = data
        return True
    return False


def sign_out() -> None:
    token = access_token()
    if token and is_configured():
        try:
            requests.post(
                f"{supabase_url()}/auth/v1/logout",
                headers=_headers(token),
                timeout=10,
            )
        except requests.RequestException:
            pass
    st.session_state.pop("hongstock_auth_session", None)


def show_auth_sidebar() -> dict[str, Any] | None:
    """공용 분석 화면은 열어 두고, 개인 기능에만 로그인 상태를 제공한다."""
    if not is_configured():
        return None

    user = current_user()
    if user:
        metadata = user.get("user_metadata") or {}
        nickname = str(metadata.get("nickname") or user.get("email") or "회원")
        st.sidebar.caption(f"로그인: {nickname}")
        if st.sidebar.button("로그아웃", key="hongstock_logout"):
            sign_out()
            st.rerun()
        return user

    st.sidebar.markdown("## 👤 내 투자 기록")
    st.sidebar.caption("모의투자·투자 성향은 로그인한 본인에게만 저장됩니다.")
    login_tab, signup_tab = st.sidebar.tabs(["로그인", "회원가입"])

    with login_tab:
        with st.form("hongstock_login_form"):
            email = st.text_input("로그인 ID (이메일)", key="hongstock_login_email")
            password = st.text_input("비밀번호", type="password", key="hongstock_login_password")
            submitted = st.form_submit_button("로그인")
        if submitted:
            try:
                sign_in(email, password)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    with signup_tab:
        with st.form("hongstock_signup_form"):
            email = st.text_input("로그인 ID (이메일)", key="hongstock_signup_email")
            password = st.text_input("비밀번호 (8자 이상)", type="password", key="hongstock_signup_password")
            nickname = st.text_input("닉네임 (선택)", key="hongstock_signup_nickname")
            analytics_consent = st.checkbox(
                "식별정보를 제외한 모의투자 통계를 추천 로직 개선에 활용하는 데 동의합니다.",
                key="hongstock_analytics_consent",
            )
            submitted = st.form_submit_button("회원가입")
        if submitted:
            if len(password) < 8:
                st.error("비밀번호는 8자 이상으로 입력해 주세요.")
            else:
                try:
                    signed_in = sign_up(email, password, nickname, analytics_consent)
                    if signed_in:
                        st.rerun()
                    st.success("가입 확인 메일을 확인한 뒤 로그인해 주세요.")
                except Exception as exc:
                    st.error(str(exc))
    return None
