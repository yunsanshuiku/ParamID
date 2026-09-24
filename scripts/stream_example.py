"""A sample acquisition client for the real online API; CSV is a simulated sensor.

Create a live session in the UI, then pass its id. This client sends one sample
at a time, respecting sequence numbers. Replace the CSV iterator with a device
reader to integrate hardware. Scheduling here is soft real time.
"""

import argparse
import csv
import json
import time
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description="向本机在线会话逐点推送仿真采集数据")
    parser.add_argument("--session", required=True)
    parser.add_argument("--csv", default="examples/regression.csv")
    parser.add_argument("--period", type=float, default=.01)
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    if args.period <= 0:
        parser.error("period 必须为正。")
    base = f"http://127.0.0.1:{args.port}"
    with urlopen(f"{base}/api/session?id={args.session}", timeout=10) as response:
        snapshot = json.load(response)
    if snapshot["next_sequence"] != 0:
        parser.error("示例从文件第一行开始，请使用尚未接收数据的新会话。")
    deadline = time.monotonic()
    with open(args.csv, encoding="utf-8-sig", newline="") as source:
        for sequence, row in enumerate(csv.DictReader(source)):
            payload = {"session_id": args.session, "sequence": sequence, "samples": [row]}
            request = Request(f"{base}/api/session/push", json.dumps(payload).encode(), {"Content-Type": "application/json"})
            with urlopen(request, timeout=10) as response:
                snapshot = json.load(response)
            if sequence % 100 == 0:
                print(f"Received={snapshot['next_sequence']}, updates={snapshot['updates']}")
            deadline += args.period
            time.sleep(max(0, deadline - time.monotonic()))
    print(json.dumps(snapshot["parameters"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
