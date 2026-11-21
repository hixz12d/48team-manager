"""Run codex-rs logged-out recovery for one codex-rs account right now (ignores the switch).

Inside the container, from /app:
    python scripts/codex_rs_recovery_once.py --account <codex-rs 账号 ID> --dry-run
    python scripts/codex_rs_recovery_once.py --account <codex-rs 账号 ID>

--dry-run only reads: candidate check, whether a refresh would be tried, whether a proxy / HME
mailbox / team name is found. It never refreshes, opens a browser or writes anything.
Output never contains tokens, proxy URLs or callback URLs.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

LABELS = {
    "remote_id": "rs 账号 ID", "enabled": "自动重授权开关", "status": "rs 状态", "error_reason": "rs 错误原因",
    "enabled_remote": "rs 账号启用", "candidate": "满足处理条件", "legacy_binding": "由 48team 旧重授权功能负责",
    "would_refresh": "会先调用 rs 刷新", "has_mark": "备注里有标记行", "state": "本地记录",
    "proxy": "找到代理", "hme_account": "HME 邮箱", "team_name": "团队名", "error": "错误",
}


def _show(value) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    if value is None:
        return "无"
    if isinstance(value, dict):
        return "，".join(f"{key}={_show(item)}" for key, item in value.items() if key != "email")
    return str(value)


async def main(account: str, dry_run: bool) -> int:
    sys.path[:0] = [str(Path.cwd()), str(Path(__file__).resolve().parents[1])]
    from app.application import codex_rs_recovery
    from app.core.config import Settings
    from app.persistence.database import create_engine, create_session_factory

    engine = create_engine(Settings())
    try:
        async with create_session_factory(engine)() as db:
            if dry_run:
                report = await codex_rs_recovery.dry_run(db, account)
                for key, value in report.items():
                    print(f"{LABELS.get(key, key)}：{_show(value)}")
                print("干跑：没有刷新、没有打开浏览器、没有写任何东西。")
                return 0 if "error" not in report else 1
            result = await codex_rs_recovery.recover_account(db, account, force=True)
            print(f"结果：{'成功' if result['ok'] else '跳过' if result['skipped'] else '失败'}")
            print(f"错误码：{result['code'] or '无'}")
            print(f"说明：{result['message']}")
            if result.get("state"):
                print(f"本地状态：{result['state']}")
            return 0 if result["ok"] else 1
    finally:
        await engine.dispose()


def cli() -> None:
    parser = argparse.ArgumentParser(description="对指定 codex-rs 账号立即跑一次掉登录自动重授权（不看开关）")
    parser.add_argument("--account", required=True, metavar="RS_ACCOUNT_ID", help="codex-rs 账号 ID")
    parser.add_argument("--dry-run", action="store_true", help="只检查不执行：不刷新、不开浏览器、不写任何东西")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.account.strip(), args.dry_run)))


if __name__ == "__main__":
    cli()
