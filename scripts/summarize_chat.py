"""命令行：总结我和某人最近聊了什么（文字 + 语音转写）。

用法：
  python scripts\\summarize_chat.py "妈妈" --days 7
  python scripts\\summarize_chat.py "妈妈" --days 3 --no-voice
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="总结与某人的聊天（含语音转文字）")
    ap.add_argument("name", help="联系人备注/昵称，例如 妈妈")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=400, help="最多取多少条消息")
    ap.add_argument("--max-voices", type=int, default=40, help="最多转写多少条语音")
    ap.add_argument("--no-voice", action="store_true", help="不转写语音，只看文字")
    ap.add_argument("--no-save", action="store_true", help="只在终端输出，不落 md 文件")
    args = ap.parse_args(argv)

    from src.assistant.chat_summary import summarize_chat
    r = summarize_chat(args.name, days=args.days, limit=args.limit,
                       transcribe=not args.no_voice, max_voices=args.max_voices,
                       save=not args.no_save)
    if not r.get("ok"):
        print("失败：", r.get("error"))
        return 1
    print("对象：%s  消息 %d 条（语音 %d 条，未转写 %d 条）  耗时 %s 秒"
          % (r["chat"], r["rows"], r["voices"], r["skipped_voices"], r["seconds"]))
    print("=" * 60)
    print(r["summary"])
    if r.get("path"):
        print("=" * 60, "\n已存：", r["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
