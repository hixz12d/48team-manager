"""API-key-only reverse handoff transport. Secrets exist only in the read result."""
from app.integrations.sub2api.client import sub2api_client


def handoff_failure(code="handoff_unavailable"):
    return {"ok": False, "error_code": code, "message": {
        "handoff_unsupported": "Sub2API 尚不支持安全交回，请先升级全部服务副本",
        "handoff_auth_required": "安全交回需要 Sub2API 管理员 API Key",
        "handoff_conflict": "交接身份或版本发生变化，已停止接收凭据",
    }.get(code, "交接结果尚未确认，保留原归属；请继续同一交接，不要重新消费刷新令牌")}


async def handoff_call(db, remote_id, request, action):
    http = None
    try:
        http, headers, _ = await sub2api_client._with_client(db)
        cap_response = await http.get("/api/v1/admin/integration/capabilities", headers=headers)
        if cap_response.status_code in {401, 403}:
            return handoff_failure("handoff_auth_required")
        cap_response.raise_for_status()
        cap = sub2api_client._unwrap_sync_payload(cap_response.json())
        contract = cap.get("oauth_sync", {})
        if cap.get("instance_id") != request["expected_instance_id"]:
            return handoff_failure("handoff_conflict")
        if contract.get("revision") != 5 or any(contract.get(k) is not True for k in ("refresh_fencing", "refresh_handoff")) or contract.get("refresh_fencing_scope") != "observed_rt_lineage":
            return handoff_failure("handoff_unsupported")
        response = await http.post(f"/api/v1/admin/accounts/{int(remote_id)}/credential-refresh-handoff", headers=headers, json={**request, "action": action})
        if response.status_code in {401, 403}:
            return handoff_failure("handoff_auth_required")
        if response.status_code == 409:
            return handoff_failure("handoff_conflict")
        response.raise_for_status()
        data = sub2api_client._unwrap_sync_payload(response.json())
        if data.get("instance_id") != request["expected_instance_id"] or data.get("operation_id") != request["operation_id"] or data.get("remote_account_id") != remote_id:
            return handoff_failure("handoff_conflict")
        if data.get("state") not in {"draining", "ready", "acknowledged"} or type(data.get("epoch")) is not int or data["epoch"] <= 0 or type(data.get("credential_version")) is not int:
            return handoff_failure()
        result = {"ok": True, "state": data["state"], "epoch": data["epoch"], "credential_version": data["credential_version"], "uncertain": data.get("uncertain") is True}
        if action == "read":
            credentials = data.get("credentials")
            if data["state"] != "ready" or data["credential_version"] <= 0 or not isinstance(credentials, dict) or any(not isinstance(credentials.get(k), str) or not credentials[k].strip() or len(credentials[k]) > 65536 for k in ("access_token", "refresh_token", "client_id")):
                return handoff_failure()
            result["credentials"] = {k: credentials[k] for k in ("access_token", "refresh_token", "client_id")}
        return result
    except Exception:
        return handoff_failure()
    finally:
        if http is not None:
            await http.aclose()
