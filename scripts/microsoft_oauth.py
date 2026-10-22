"""Classify Microsoft OAuth failures without reflecting credentials in errors."""

from __future__ import annotations

import re
from typing import Any


def _aadsts_codes(payload: dict[str, Any]) -> set[str]:
    codes = set(re.findall(r"AADSTS\d{5,8}\b", str(payload.get("error_description") or ""), re.I))
    codes = {code.upper() for code in codes}
    for code in payload.get("error_codes") or []:
        if re.fullmatch(r"\d{5,8}", str(code)):
            codes.add(f"AADSTS{code}")
    return codes


def is_scope_error(payload: dict[str, Any]) -> bool:
    """Distinguish permission failures from expired or revoked credentials."""
    code = str(payload.get("error") or "")
    description = str(payload.get("error_description") or "").lower()
    codes = _aadsts_codes(payload)
    return (
        code in {"invalid_scope", "consent_required"}
        or bool(codes & {"AADSTS65001", "AADSTS65004", "AADSTS70011", "AADSTS90023"})
        or (code == "invalid_grant" and "scope" in description
            and any(word in description for word in ("unauthorized", "not authorized", "consent")))
    )


def oauth_error_message(payload: dict[str, Any], status: int = 0) -> str:
    code = str(payload.get("error") or "")
    codes = _aadsts_codes(payload)
    suffix = f"（{', '.join(sorted(codes))}）" if codes else ""
    if code == "authorization_pending":
        return "pending"
    if code == "slow_down":
        return "slow_down"
    if is_scope_error(payload):
        return f"Microsoft 未授予 Graph Mail.Read 邮箱权限，请检查应用授权范围{suffix}。"
    if code in {"authorization_declined", "access_denied"}:
        return f"Microsoft 授权已被拒绝，请重新开始授权{suffix}。"
    if code in {"expired_token", "code_expired"}:
        return "Microsoft 设备授权已过期，请重新开始授权。"
    if codes & {"AADSTS700082", "AADSTS700084", "AADSTS70008", "AADSTS70043"}:
        return f"Outlook refresh_token 已过期，请重新完成 Microsoft 授权{suffix}。"
    if codes & {"AADSTS50173"}:
        return f"Microsoft 已撤销此 refresh_token，请重新完成授权{suffix}。"
    if code in {"invalid_client", "unauthorized_client"} or "AADSTS700016" in codes:
        return f"Client ID 无效，或应用未启用公共客户端登录{suffix}。"
    if "AADSTS50020" in codes:
        return f"该应用未允许当前 Microsoft 账号类型，请检查应用设置{suffix}。"
    if code in {"interaction_required", "login_required"}:
        return f"账号需要交互登录或补充授权，请完成 Microsoft 授权{suffix}。"
    if code == "invalid_grant":
        return f"Microsoft 拒绝刷新凭据，请核对 client_id、refresh_token 与授权范围{suffix}。"
    # OAuth providers may echo request values in error_description. Return only
    # known diagnostics, never the untrusted raw description.
    return f"Microsoft OAuth2 请求失败{suffix}" + (f"（HTTP {status}）。" if status else "。")
