"""Generate reproducible coupled 2x2 data with shuffled numbered headers."""
import csv
import json
from pathlib import Path

import numpy as np
from scipy.signal import lfilter


def main():
    root = Path(__file__).resolve().parents[1] / "examples"
    rng = np.random.default_rng(20260916)
    n, dt = 1000, .02
    # Independent inputs excite both columns of the transfer matrix.
    u = np.repeat(rng.choice([-1., 0., 1.], size=(n//10, 2)), 10, axis=0)
    y = np.column_stack((
        lfilter([0., .25], [1., -.75], u[:, 0]) + lfilter([0., -.10], [1., -.75], u[:, 1]) + .30,
        lfilter([0., .12], [1., -.55], u[:, 0]) + lfilter([0., .30], [1., -.55], u[:, 1]) - .10))
    y += rng.normal(0., .001, y.shape)
    with (root / "multichannel.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["time", "input2", "output2", "input1", "output1"])
        writer.writerows(zip(np.arange(n)*dt, u[:, 1], y[:, 1], u[:, 0], y[:, 0]))
    config = {"model": {"kind": "multichannel", "sample_time": dt, "intercept": True}, "algorithm": "oe",
              "truth": {"output1": {"a": [.75], "b": [[.25], [-.10]], "offset": .075},
                        "output2": {"a": [.55], "b": [[.12], [.30]], "offset": -.045}},
              "note": "所有输入作用于所有输出；表头有意打乱。这是仿真数据，不是真实设备实验。"}
    (root / "multichannel.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
