#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""텔레그램 chat_id 확인용 1회성 스크립트.
사용법: 봇에게 아무 메시지나 하나 보낸 뒤
  TELEGRAM_BOT_TOKEN=봇토큰 python3 get_chat_id.py
"""
import os, sys, requests

token = os.environ.get("TELEGRAM_BOT_TOKEN") or (sys.argv[1] if len(sys.argv) > 1 else "")
if not token:
    sys.exit("봇 토큰이 없습니다. TELEGRAM_BOT_TOKEN 환경변수나 첫 번째 인자로 넣어주세요.")

r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
if not r.get("ok"):
    sys.exit(f"토큰이 잘못된 것 같습니다: {r}")

found = {}
for u in r.get("result", []):
    msg = u.get("message") or u.get("channel_post") or {}
    chat = msg.get("chat") or {}
    if chat.get("id"):
        found[chat["id"]] = f"{chat.get('type')} · {chat.get('title') or chat.get('username') or chat.get('first_name','')}"

if not found:
    print("아직 받은 메시지가 없습니다. 텔레그램에서 봇에게 아무 말이나 하나 보낸 뒤 다시 실행해 주세요.")
else:
    print("찾은 chat_id:")
    for cid, desc in found.items():
        print(f"  {cid}   ({desc})")
